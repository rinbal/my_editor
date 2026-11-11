# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A work the writer published that its publication does not list yet
(RG-M4-b 3; editorial contract 12.4 ``/published``). Pure: the record,
and when to report it again.

Once a relay took the writer's signed article it is public, whatever the
server answers to the report. A report that did not arrive is made again
quietly after a growing pause, for at most seven days from the first
(the server also records the work from the relays); one the server
refused because its publication lists another work at the link name is
kept as that, and never tried again.

The record (per account, in the app's settings, like
:mod:`.cleanup`'s) holds, by sid, the signed article, its naddr and the
revision it publishes: public already, nothing secret, and never a key.
So the work stays known as public across a restart, and it is kept
until the View lists the work (or no longer waits for this report):
the seven days end only the quiet tries (RG-M4-c 3), and after them the
person sends it again by hand.

A work the writer published on another device has no record here. When
the server lists another work at its link name, the relays' read of its
address (12.4) tells whether it is public all the same
(:func:`found_public`), and it is kept as that.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import events as nostr_events
from . import formats, publishing

# The pauses between tries, in seconds; the last one repeats.
PAUSES = (30, 2 * 60, 10 * 60, 3600, 6 * 3600, 24 * 3600)
# How long a report is tried again, from its first try.
GIVES_UP = 7 * 24 * 3600
SETTINGS_KEY = "standup_unlisted"
# At most this many works are kept per account (the oldest go first).
MOST_KEPT = 32


def due(entry: Optional[Mapping[str, Any]], now: float) -> bool:
    """Whether ``entry`` is to be reported again at ``now``."""
    return entry is not None and not entry.get("taken") and not over(entry, now) \
        and now >= entry.get("next", 0)


def over(entry: Mapping[str, Any], now: float) -> bool:
    """Whether ``entry`` has been tried for long enough."""
    return now > entry.get("started", now) + GIVES_UP


def first(*, event: Mapping, naddr: str, revision: int, now: float,
          taken: bool = False) -> Dict[str, Any]:
    """The record of a report that did not arrive at ``now`` (``taken``:
    refused because the publication lists another work at the link name)."""
    return {"event": dict(event), "naddr": naddr, "revision": int(revision), "started": now,
            "tries": 1, "next": now + PAUSES[0], "taken": bool(taken)}


def found_public(revision: Mapping, *, writer: str, address: str,
                 relays: Mapping[str, Tuple[bool, Sequence[Any]]]
                 ) -> Optional[Tuple[dict, List[str]]]:
    """The newest event at ``address`` in the relays' read (12.4) that
    credits ``writer`` and shows ``revision``, with the relays that hold
    it; None when none does. It shows the revision when nothing tells the
    two apart: :func:`publishing.correction_changes` finds no change and
    :func:`publishing.correction_texts` reads both texts alike."""
    shown = [(url, event) for url, (_complete, events) in relays.items() for event in events
             if publishing.held_credit(event, address) == writer
             and not publishing.correction_changes(revision, event)
             and len(set(publishing.correction_texts(revision, event))) == 1]
    if not shown:
        return None
    newest = max((event for _url, event in shown), key=lambda event: event["created_at"])
    return dict(newest), [url for url, event in shown if event["id"] == newest["id"]]


def after_try(entry: Mapping[str, Any], now: float, *, taken: bool = False) -> Dict[str, Any]:
    """The record after one more try at ``now`` that did not arrive."""
    tries = int(entry.get("tries", 0)) + 1
    pause = PAUSES[min(tries, len(PAUSES)) - 1]
    return dict(entry, tries=tries, next=now + pause, taken=bool(taken or entry.get("taken")))


class Unlisted:
    """The unlisted works of one account, by sid, in the app's settings
    (key :data:`SETTINGS_KEY`, then the account's pubkey). ``load`` and
    ``save`` are seams; :meth:`in_memory` keeps them for one run only."""

    def __init__(self, pubkey: str, *, load: Optional[Callable[[], dict]] = None,
                 save: Optional[Callable[[str, Any], None]] = None) -> None:
        if load is None or save is None:
            import settings as app_settings
            load = load or app_settings.load_settings
            save = save or app_settings.save_setting
        self._pubkey, self._load, self._save = pubkey, load, save
        self._verified: Dict[str, bool] = {}          # event id -> it verifies
        # This account's records, read once (the rows ask often) and
        # written through: only this app writes them.
        self._kept: Optional[Dict[str, Any]] = None

    @classmethod
    def in_memory(cls, pubkey: str) -> "Unlisted":
        kept: Dict[str, Any] = {}
        return cls(pubkey, load=lambda: kept, save=kept.__setitem__)

    def _all(self) -> Dict[str, Any]:
        stored = (self._load() or {}).get(SETTINGS_KEY)
        return dict(stored) if isinstance(stored, dict) else {}

    def _mine(self) -> Dict[str, Any]:
        if self._kept is None:
            mine = self._all().get(self._pubkey)
            self._kept = dict(mine) if isinstance(mine, dict) else {}
        return dict(self._kept)

    def _write(self, mine: Mapping[str, Any]) -> None:
        self._kept = dict(mine)
        stored = self._all()
        stored[self._pubkey] = dict(mine)
        self._save(SETTINGS_KEY, stored)

    def entry(self, sid: str) -> Optional[Dict[str, Any]]:
        """The record of ``sid``: its event the writer's own valid article
        at the naddr's address, else None (nothing else is believed)."""
        entry = self._mine().get(sid)
        if not isinstance(entry, dict) or not formats.is_int(entry.get("tries")) \
                or not formats.is_int(entry.get("revision")) \
                or not isinstance(entry.get("naddr"), str):
            return None
        event = entry.get("event")
        if not isinstance(event, dict) or event.get("pubkey") != self._pubkey:
            return None
        known = self._verified.get(str(event.get("id")))
        if known is None:
            known = self._verified[str(event.get("id"))] = nostr_events.is_verified_event(
                event, kind=formats.ARTICLE_KIND)
        return entry if known else None

    def sids(self) -> List[str]:
        return [sid for sid in self._mine() if self.entry(sid) is not None]

    def keep(self, sid: str, entry: Mapping[str, Any]) -> None:
        mine = self._mine()
        mine.pop(sid, None)
        mine[sid] = dict(entry)
        while len(mine) > MOST_KEPT:
            mine.pop(next(iter(mine)))
        self._write(mine)

    def forget(self, sid: str) -> None:
        mine = self._mine()
        if mine.pop(sid, None) is not None:
            self._write(mine)


__all__ = ["GIVES_UP", "MOST_KEPT", "PAUSES", "SETTINGS_KEY", "Unlisted", "after_try", "due",
           "first", "found_public", "over"]
