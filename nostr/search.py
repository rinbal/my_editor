"""NIP-50 search client — global person lookup when local matches run dry.

Spec: https://github.com/nostr-protocol/nips/blob/master/50.md

Relays that implement NIP-50 accept a ``"search"`` field on their REQ
filters and return matching events. For our use case we search ``kind:0``
profile events on relays known to have the index built — currently just
``relay.nostr.band``, which can be extended later if other relays add the
capability.

Results stream into ``KnownPeople`` so they remain searchable offline on
the next query.
"""

from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .contacts import parse_metadata_event
from .known_people import KnownPeople, Person
from .relay import RelayPool, Subscription


# Curated set of relays that implement NIP-50 for kind 0.
DEFAULT_SEARCH_RELAYS: tuple[str, ...] = (
    "wss://relay.nostr.band",
)

# Time before we give up and emit whatever we collected so far.
_SEARCH_TIMEOUT_MS: int = 3_500

# Hard cap so a chatty relay can't flood the picker.
_SEARCH_LIMIT: int = 30


class Nip50SearchClient(QObject):
    """One-query-at-a-time NIP-50 search.

    The picker fires a new search on every keystroke (after debouncing on
    its side); each call cancels the previous one so stale results from
    an out-of-date query don't trickle in late.

    Signals:
      results(query, list[Person])  — terminal; fired once per call
      failed(query, str)            — terminal alternative on no relays / error
    """

    results = Signal(str, list)
    failed = Signal(str, str)

    def __init__(
        self,
        pool: RelayPool,
        people: KnownPeople,
        *,
        relays: tuple[str, ...] = DEFAULT_SEARCH_RELAYS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._pool = pool
        self._people = people
        self._relays = list(relays)
        self._sub: Optional[Subscription] = None
        self._timer: Optional[QTimer] = None
        self._current_query: str = ""
        self._batch: List[Person] = []
        self._seen: set[str] = set()

    def search(self, query: str) -> None:
        query = query.strip()
        self._cancel_active()
        self._current_query = query
        self._batch = []
        self._seen = set()
        if not query:
            self.results.emit(query, [])
            return
        if not self._relays:
            self.failed.emit(query, "no search relays configured")
            return

        self._sub = self._pool.subscribe(
            self._relays,
            filters=[{"kinds": [0], "search": query, "limit": _SEARCH_LIMIT}],
        )
        self._sub.event.connect(self._on_event)
        self._sub.eose.connect(self._finalize)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_SEARCH_TIMEOUT_MS)
        self._timer.timeout.connect(self._finalize)
        self._timer.start()

    def cancel(self) -> None:
        self._cancel_active()
        # No signal is emitted on explicit cancel — caller initiated it.

    # -- internals ---------------------------------------------------------

    def _cancel_active(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _on_event(self, event: dict) -> None:
        person = parse_metadata_event(event)
        if person is None or person.pubkey in self._seen:
            return
        self._seen.add(person.pubkey)
        person.source = "search"
        person.updated_at = person.updated_at or int(time.time())
        # Persist so the next local search hits it without round-tripping
        # the relay again. The cache merges if the same pubkey already
        # exists from the contact list.
        self._people.upsert(person)
        self._batch.append(person)

    def _finalize(self) -> None:
        if self._sub is None:
            return  # already finalized
        query = self._current_query
        batch = list(self._batch)
        self._cancel_active()
        self.results.emit(query, batch)
