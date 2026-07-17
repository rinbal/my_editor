# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-02 contact-list fetcher + batched metadata refresh.

Two steps, run sequentially per profile:

  1. Fetch the user's most recent ``kind:3`` event. The ``["p", pubkey,
     relay_hint, petname]`` tags become the seed entries in KnownPeople —
     enough for the picker to function offline immediately after.

  2. Fan out ``kind:0`` REQs in batches of ~200 authors at a time to fill
     in real display names, pictures, and nip05 identifiers. Events stream
     into KnownPeople as they arrive.

The fetcher is intentionally fire-and-forget: callers don't await the
result, and a missing kind 3 or partial metadata coverage is fine — the
picker can show whatever's in the cache and search relays cover the rest.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

from . import DEFAULT_RELAYS
from .known_people import KnownPeople, Person
from .queries import fetch_latest_event
from .relay import RelayPool, Subscription


# How many authors per kind-0 batch REQ. Most major relays accept up to
# ~500; 200 keeps a wider compatibility margin.
_METADATA_BATCH_SIZE: int = 200

# Stop waiting for metadata after this many seconds even if EOSE hasn't
# fired on every relay. The cache picks up whatever did land.
_METADATA_TIMEOUT_MS: int = 12_000


# --------------------------------------------------------------------------- #
# Pure parsers                                                                #
# --------------------------------------------------------------------------- #

def parse_contact_list(event: dict) -> List[Person]:
    """Extract follow entries from a ``kind:3`` event.

    Per NIP-02 each follow is ``["p", <pubkey-hex>, <relay-hint>?, <petname>?]``.
    Returns Person records seeded with the petname as display_name (a
    sensible offline fallback until kind 0 fills the real value in).
    """
    out: List[Person] = []
    seen: set[str] = set()
    for tag in event.get("tags", []):
        if not isinstance(tag, list) or len(tag) < 2 or tag[0] != "p":
            continue
        pk = str(tag[1]).strip().lower()
        if len(pk) != 64 or pk in seen:
            continue
        seen.add(pk)
        relay_hint = str(tag[2]).strip() if len(tag) >= 3 and isinstance(tag[2], str) else ""
        petname = str(tag[3]).strip() if len(tag) >= 4 and isinstance(tag[3], str) else ""
        out.append(Person(
            pubkey=pk,
            display_name=petname,
            relay_hint=relay_hint,
            source="contact",
        ))
    return out


def parse_metadata_event(event: dict) -> Optional[Person]:
    """Extract Person fields from a single ``kind:0`` event.

    Returns ``None`` if the event has no pubkey or its content isn't a
    JSON object — both treated as "no data, move on".
    """
    pk = event.get("pubkey")
    if not isinstance(pk, str) or len(pk) != 64:
        return None
    try:
        fields = json.loads(event.get("content", "") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(fields, dict):
        return None
    return Person(
        pubkey=pk,
        display_name=(fields.get("display_name") or fields.get("name") or "").strip(),
        picture=(fields.get("picture") or "").strip(),
        nip05=(fields.get("nip05") or "").strip(),
        source="contact",
        updated_at=int(event.get("created_at") or time.time()),
    )


# --------------------------------------------------------------------------- #
# Fetcher                                                                     #
# --------------------------------------------------------------------------- #

class ContactListFetcher(QObject):
    """Populate KnownPeople for a user's follow list, in the background.

    Signals:
      seeded(int)             — count of contacts pulled from kind 3 (petname only)
      metadata_progress(int)  — incremental count of kind 0 events absorbed
      completed(int)          — total contacts now known after metadata pass
      failed(str)             — terminal error before seeding completes
    """

    seeded = Signal(int)
    metadata_progress = Signal(int)
    person_updated = Signal(object)   # Person — fired per kind 0 absorbed
    completed = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        pool: RelayPool,
        people: KnownPeople,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._pool = pool
        self._people = people
        self._sub: Optional[Subscription] = None
        self._timer = None
        self._metadata_count = 0
        self._followed: List[str] = []
        self._extra_relays: List[str] = []

    def fetch(self, pubkey_hex: str, extra_relays: Optional[List[str]] = None) -> None:
        """Kick off the two-step fetch. Cancel any in-flight pass first."""
        self.cancel()
        self._extra_relays = list(extra_relays or [])
        self._metadata_count = 0

        relays = list(dict.fromkeys(list(DEFAULT_RELAYS) + self._extra_relays))

        def _on_contact_list(event: Optional[dict]) -> None:
            if event is None:
                self.failed.emit("no contact list (kind 3) found")
                return
            contacts = parse_contact_list(event)
            # Seed petnames/relay hints immediately so the picker is usable
            # without waiting on the metadata pass.
            self._people.upsert_many(contacts)
            self._followed = [c.pubkey for c in contacts]
            self.seeded.emit(len(contacts))
            if self._followed:
                self._begin_metadata_pass(relays)
            else:
                self.completed.emit(0)

        fetch_latest_event(
            self._pool,
            relays,
            filters=[{"kinds": [3], "authors": [pubkey_hex], "limit": 1}],
            on_done=_on_contact_list,
            timeout_ms=8_000,
            parent=self,
        )

    def cancel(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # -- metadata pass -----------------------------------------------------

    def _begin_metadata_pass(self, relays: List[str]) -> None:
        # All authors go in a single REQ if they fit in one batch, else
        # we split the filter list. Most relays accept multiple filters in
        # one REQ, but to stay friendly to stricter ones we chunk authors.
        filters: List[dict] = []
        for i in range(0, len(self._followed), _METADATA_BATCH_SIZE):
            chunk = self._followed[i : i + _METADATA_BATCH_SIZE]
            filters.append({"kinds": [0], "authors": chunk})

        self._sub = self._pool.subscribe(relays, filters)
        self._sub.event.connect(self._on_metadata_event)

        # Lazy import to avoid a top-level Qt dependency on QTimer at module
        # load time (this module is imported by non-Qt tests too).
        from PySide6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_METADATA_TIMEOUT_MS)
        self._timer.timeout.connect(self._finalize)
        self._timer.start()

        # Also finalize once every relay sends EOSE — usually quicker than
        # the safety timeout.
        self._sub.eose.connect(self._finalize)

    def _on_metadata_event(self, event: dict) -> None:
        person = parse_metadata_event(event)
        if person is None:
            return
        existing = self._people.get(person.pubkey)
        if existing is not None and existing.updated_at >= person.updated_at:
            return  # we already have a newer kind 0 cached
        # Merge: preserve relay_hint from the kind-3 seed if the new record
        # doesn't carry one. upsert handles empty-field preservation.
        merged = self._people.upsert(person)
        self._metadata_count += 1
        self.metadata_progress.emit(self._metadata_count)
        self.person_updated.emit(merged)

    def _finalize(self) -> None:
        if self._sub is None:
            return  # already finalized
        self._sub.close()
        self._sub = None
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.completed.emit(len(self._followed))
