# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a private list (``nostr/encrypted_lists.py``) talks to relays:
asking for its events, publishing one, and telling the account's own
list from anything else a relay answers."""

from __future__ import annotations

import logging
from typing import Callable, Collection, Optional, Tuple

from PySide6.QtCore import QObject

from .events import verify_event
from .outbox.policy import normalize_relay_url
from .queries import fetch_events
from .later import answer_later

__all__ = ["LIST_KIND", "ListQuery", "event_mark", "is_list_of", "publish_once", "relay_key"]

_log = logging.getLogger(__name__)

LIST_KIND = 30078               # NIP-78 application data


class ListQuery(QObject):
    """Asks relays for events and says which relays answered."""

    def __init__(self, pool, parent: Optional[QObject] = None, *,
                 timeout_ms: int = 8_000) -> None:
        super().__init__(parent)
        self._pool = pool
        self._timeout_ms = timeout_ms

    def events(self, relays, filters, on_done) -> None:
        """``on_done(events, answered)``: every validly signed event, and
        the relays that answered (sent all their stored events); one that
        refused or could not be reached is not among them."""
        try:
            fetch_events(self._pool, list(relays), list(filters),
                         lambda fetched: on_done(list(fetched.events), set(fetched.answered)),
                         timeout_ms=self._timeout_ms, parent=self)
        except Exception:  # noqa: BLE001, settle the callback contract
            on_done([], set())


def publish_once(relay_pool, relays, signed, *, on_done: Callable[[Collection[str]], None],
                 every_relay: bool = False) -> None:
    """Publish ``signed`` and answer once with the relays that took it:
    as soon as one did (a relay that never answers must not hold up
    quitting for the publish timeout), or, with ``every_relay``, when
    every relay has answered or timed out. An event the pool refuses
    (not signed: ValueError) or a pool already gone at shutdown
    (RuntimeError) is taken by no relay, answered on the next turn;
    anything else is a bug and raises."""
    relays = list(relays)
    try:
        job = relay_pool.publish(relays, signed)
    except (ValueError, RuntimeError) as exc:
        _log.warning("list publish failed: %s", exc)
        answer_later(on_done, frozenset())
        return
    answered = []

    def accepted(url: str) -> None:
        if not answered and not every_relay:
            answered.append(True)
            on_done(frozenset({url}))

    def all_done(results) -> None:
        if not answered:
            answered.append(True)
            on_done(frozenset(url for url, ok, _ in results if ok))

    job.first_accept.connect(accepted)
    job.all_done.connect(all_done)


def event_mark(event: Optional[dict]) -> Optional[Tuple[int, str]]:
    """Which event a list came from: its time and id."""
    if not isinstance(event, dict):
        return None
    try:
        return int(event.get("created_at", 0)), str(event.get("id") or "")
    except (TypeError, ValueError):
        return None


def is_list_of(event, pubkey: str, d_tag: str, kind: int = LIST_KIND) -> bool:
    """Whether ``event`` is ``pubkey``'s own list under ``d_tag``: the
    kind, the ``d`` tag, the author and a valid signature."""
    if not isinstance(event, dict) or event.get("kind") != kind:
        return False
    if str(event.get("pubkey", "")).lower() != pubkey.lower():
        return False
    tag = next((t[1] for t in event.get("tags", [])
                if isinstance(t, list) and len(t) >= 2 and t[0] == "d"), None)
    if tag != d_tag:
        return False
    try:
        int(event.get("created_at"))
    except (TypeError, ValueError):
        return False
    return verify_event(event)


def relay_key(url: str) -> str:
    """A relay URL as two spellings of one relay compare."""
    return normalize_relay_url(url) or url
