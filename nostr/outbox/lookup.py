# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find someone's newest replaceable event, and say how sure that is.

Looking up a relay list (or a profile) has three outcomes, and confusing
two of them is how a client overwrites somebody's real list:

    FOUND    a validly signed event of the right kind by the right author
    ABSENT   enough relays answered "nothing stored" (ABSENT_QUORUM, one of
             them an indexer): there really is none, as far as can be told
    UNKNOWN  relays timed out, refused, or too few answered. This is not
             evidence of absence and must never be treated as such.

Only events that pass verification compete for "newest", so a forged
event with a large timestamp cannot hide the real one.

Each relay asked ends in one of three ways: it ends its stored events
(EOSE, an answer), it closes the request (CLOSED, a refusal: auth
required, rate limited, blocked) or it cannot be reached. Only an answer
counts toward ABSENT. A lookup is over as soon as every relay has ended
one way or the other, and at the latest when the time is up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QTimer

from .. import events
from . import defaults
from .policy import LookupState, dedupe_relays, is_newer

# Candidates whose signature is checked per author and lookup, so a relay
# flooding forged events costs a bounded amount of work. An author with
# more candidates than that is UNKNOWN: one that was never checked might
# have been the real, newer event.
VERIFY_CAP = 25


@dataclass(frozen=True)
class Lookup:
    """What one lookup established."""

    state: LookupState
    event: Optional[dict] = None
    answered: tuple = ()     # relays that ended their stored events (EOSE)
    refused: tuple = ()      # relays that closed the request or could not be reached
    unchecked: bool = False  # candidates went unchecked (VERIFY_CAP): UNKNOWN, whoever answered


def classify(event: Optional[dict], answered: Sequence[str]) -> LookupState:
    """FOUND, ABSENT or UNKNOWN from a verified event and who answered."""
    if event is not None:
        return LookupState.FOUND
    indexers = set(dedupe_relays(defaults.INDEXER_RELAYS))
    if len(answered) >= defaults.ABSENT_QUORUM and any(a in indexers for a in answered):
        return LookupState.ABSENT
    return LookupState.UNKNOWN


def fetch_replaceable(pool, relays: Sequence[str], *, kind: int, author: str,
                      on_done: Callable[[Lookup], None], timeout_ms: int = 6_000,
                      parent: Optional[QObject] = None) -> "_ReplaceableQuery":
    """Ask ``relays`` for ``author``'s newest ``kind``; ``on_done`` once."""
    key = (author or "").lower()
    return _ReplaceableQuery(pool, dedupe_relays(relays), kind, [key],
                             lambda results: on_done(results[key]), timeout_ms, parent)


def fetch_replaceable_many(pool, relays: Sequence[str], *, kind: int,
                           authors: Sequence[str],
                           on_done: Callable[[Dict[str, Lookup]], None],
                           timeout_ms: int = 6_000,
                           parent: Optional[QObject] = None) -> "_ReplaceableQuery":
    """Ask ``relays`` for the newest ``kind`` of every one of ``authors`` in
    one request (one filter naming them all); ``on_done`` once, with a
    Lookup per author. A relay's end of stored events answers for all of
    them, so each is classified from the same answers."""
    keys = list(dict.fromkeys((a or "").lower() for a in authors if a))
    return _ReplaceableQuery(pool, dedupe_relays(relays), kind, keys, on_done,
                             timeout_ms, parent)


class _ReplaceableQuery(QObject):
    """One REQ for the newest ``kind`` of one or more authors."""

    def __init__(self, pool, relays: List[str], kind: int, authors: List[str], on_done,
                 timeout_ms: int, parent) -> None:
        super().__init__(parent)
        self._relays = relays
        self._kind = kind
        self._authors = authors
        self._on_done = on_done
        self._best: Dict[str, Optional[dict]] = {a: None for a in authors}
        self._checked: Dict[str, int] = {a: 0 for a in authors}
        self._overflow: set = set()
        self._rejected: set = set()     # fingerprints of copies that failed the check
        self._answered: List[str] = []
        self._refused: List[str] = []
        self._finished = False
        self._sub = None
        self._timer: Optional[QTimer] = None
        if not relays or not authors:
            QTimer.singleShot(0, self._finish)
            return
        self._sub = pool.subscribe(relays, [{"kinds": [kind], "authors": list(authors),
                                             "limit": 2 * len(authors)}])
        self._sub.event.connect(self._on_event)
        self._sub.relay_eose.connect(self._on_relay_eose)
        self._sub.relay_closed.connect(self._on_relay_refused)
        self._sub.relay_failed.connect(self._on_relay_refused)
        self._sub.eose.connect(self._finish)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(timeout_ms)
        self._timer.timeout.connect(self._finish)
        self._timer.start()

    def _on_event(self, event: dict) -> None:
        if self._finished or not isinstance(event, dict) or event.get("kind") != self._kind:
            return
        author = str(event.get("pubkey", "")).lower()
        if author not in self._best:
            return
        if not is_newer(event, self._best[author]):
            return          # older, or the same event again from another relay
        fingerprint = _fingerprint(event)
        if fingerprint in self._rejected:
            return          # the same forgery again: once is enough
        if self._checked[author] >= VERIFY_CAP:
            self._overflow.add(author)
            return
        self._checked[author] += 1
        if events.verify_event(event):
            self._best[author] = event
        else:
            self._rejected.add(fingerprint)

    def _on_relay_eose(self, url: str) -> None:
        if url not in self._answered and url not in self._refused:
            self._answered.append(url)
        self._finish_when_settled()

    def _on_relay_refused(self, url: str, _reason: str = "") -> None:
        if url not in self._answered and url not in self._refused:
            self._refused.append(url)
        self._finish_when_settled()

    def _finish_when_settled(self) -> None:
        if set(self._relays) <= set(self._answered) | set(self._refused):
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._timer is not None:
            self._timer.stop()
        if self._sub is not None:
            self._sub.close()
            self._sub.deleteLater()
        answered = tuple(self._answered)
        results = {}
        for author, event in self._best.items():
            if author in self._overflow:
                results[author] = Lookup(LookupState.UNKNOWN, answered=answered,
                                         refused=tuple(self._refused), unchecked=True)
                continue
            results[author] = Lookup(state=classify(event, answered), event=event,
                                     answered=answered, refused=tuple(self._refused))
        try:
            self._on_done(results)
        finally:
            # A finished query belongs to nobody: let go of the parent
            # first, so the deferred delete is the only way it goes. Left
            # as a child, a parent torn down before the event loop runs
            # again (a directory freed by the garbage collector) deleted
            # it a second time.
            self.setParent(None)
            self.deleteLater()


def _fingerprint(event: dict) -> str:
    """The whole event, as text: a copy differing in any field, even one
    sharing the real event's id and signature, is a different copy."""
    try:
        return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(event)
