# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Following the writer's submissions without a signer prompt.

:class:`SubmissionWatcher` asks ``POST /api/submissions/lookup`` with
the status keys from the writer's submissions list, at launch
(:meth:`~SubmissionWatcher.start`), when the Submissions view opens
(:meth:`~SubmissionWatcher.view_opened`) and every 15 minutes (plan
10.4). The lookup carries no signature: a NIP-98 poll would cost a
signature each time, which with a signer app is a request on the phone.
Each submission is asked only for what is new since the last ``seq``
seen, at most 50 per request, each on its own server.

Everything is per account: a watcher belongs to the account it was made
for, and :meth:`~SubmissionWatcher.stop` (sign-out, another account; the
window calls it from ``_release_identity_state``) stops the timer,
ignores every answer still on its way and forgets what it learned
(criterion 19 of the plan).

:func:`resolve_unconfirmed` is the rule of contract 12.4 for a request
that got no answer: the lookup decides whether it arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QTimer, Signal

from . import formats, views
from .client import StandupApi, StandupError
from .lists import SubmissionEntry

REFRESH_INTERVAL_MS = 15 * 60 * 1000


@dataclass(frozen=True)
class Followed:
    """What the server says about one submission: its view, the receipt
    of its latest revision, and every timeline item seen so far."""

    entry: SubmissionEntry
    item: views.LookupItem
    timeline: tuple

    @property
    def last_seq(self) -> int:
        return self.timeline[-1].seq if self.timeline else 0


class SubmissionWatcher(QObject):
    """Polls one account's submissions through their status keys.

    ``entries()`` gives the account's entries (the submissions list);
    only the writer's, which carry a status key, are looked up.
    ``client_for(server)`` gives the :class:`StandupApi` for a server
    (one per server and account). ``timer`` is a seam.

    Signals:
      updated(dict)   sid -> Followed, after each round that learned something.
      failed(object)  a server could not be asked (StandupError); the
                      next round tries again.
    """

    updated = Signal(object)
    failed = Signal(object)

    def __init__(self, account: str, entries: Callable[[], Sequence[SubmissionEntry]],
                 client_for: Callable[[str], StandupApi], *,
                 interval_ms: int = REFRESH_INTERVAL_MS, timer=None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.account = account
        self._entries = entries
        self._client_for = client_for
        self._interval_ms = interval_ms
        self._timer = timer if timer is not None else QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)
        self._generation = 0
        self._running = False
        self._busy = False
        self._again = False
        self._followed: Dict[str, Followed] = {}

    # -- triggers ------------------------------------------------------------

    def start(self) -> None:
        """At launch, once the list is read: look now, then every 15 minutes."""
        self._running = True
        self.refresh()

    def view_opened(self) -> None:
        """The Submissions view opened: look now."""
        if self._running:
            self.refresh()

    def stop(self) -> None:
        """Sign-out or another account: no timer, no answer, nothing kept."""
        self._generation += 1
        self._running = False
        self._busy = False
        self._again = False
        self._timer.stop()
        self._followed = {}

    @property
    def running(self) -> bool:
        return self._running

    def followed(self) -> Dict[str, Followed]:
        return dict(self._followed)

    def _on_timer(self) -> None:
        if self._running:
            self.refresh()

    # -- one round -------------------------------------------------------------

    def refresh(self) -> None:
        """Ask every server once for the account's submissions. A round
        asked for while one runs follows it."""
        if not self._running:
            return
        if self._busy:
            self._again = True
            return
        self._timer.stop()
        entries = [e for e in self._entries() if e.role == "writer" and e.status_key]
        batches: List[List[SubmissionEntry]] = []
        by_server: Dict[str, List[SubmissionEntry]] = {}
        for entry in entries:
            by_server.setdefault(entry.server, []).append(entry)
        for server, group in by_server.items():
            for start in range(0, len(group), formats.MAX_LOOKUP_ITEMS):
                batches.append(group[start:start + formats.MAX_LOOKUP_ITEMS])
        if not batches:
            self._round_done()
            return
        self._busy = True
        generation = self._generation
        waiting = {"n": len(batches)}
        learned: Dict[str, Followed] = {}

        def one_done() -> None:
            waiting["n"] -= 1
            if waiting["n"] == 0:
                self._busy = False
                if learned:
                    self._followed.update(learned)
                    self.updated.emit(dict(learned))
                self._round_done()

        for batch in batches:
            self._ask(batch, generation, learned, one_done)

    def _ask(self, batch: List[SubmissionEntry], generation: int,
             learned: Dict[str, Followed], one_done: Callable[[], None]) -> None:
        by_key = {entry.status_key: entry for entry in batch}

        def found(items: Dict[str, views.LookupItem]) -> None:
            if generation != self._generation:
                return
            for key, item in items.items():
                entry = by_key.get(key)
                if entry is None or item.submission.submission_id != entry.sid:
                    continue
                known = self._followed.get(entry.sid)
                timeline = {i.seq: i for i in (known.timeline if known else ())}
                timeline.update({i.seq: i for i in item.timeline})
                learned[entry.sid] = Followed(entry=entry, item=item,
                                              timeline=tuple(timeline[s] for s in sorted(timeline)))
            one_done()

        def failed(error: StandupError) -> None:
            if generation != self._generation:
                return
            self.failed.emit(error)
            one_done()

        try:
            client = self._client_for(batch[0].server)
        except ValueError as exc:
            failed(StandupError("unavailable", message=str(exc)))
            return
        client.lookup([(e.status_key, self._since(e)) for e in batch], found, failed)

    def _since(self, entry: SubmissionEntry) -> int:
        known = self._followed.get(entry.sid)
        return known.last_seq if known else 0

    def _round_done(self) -> None:
        if not self._running:
            return
        if self._again:
            self._again = False
            self.refresh()
            return
        self._timer.start(self._interval_ms)


# --------------------------------------------------------------------------- #
# No answer after sending (12.4)                                               #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class NotStored:
    """The server has no such revision: nothing was stored, and the
    submission may be sent again (also as a newly built revision)."""

    receipt: Optional[views.Receipt] = None


@dataclass(frozen=True)
class Superseded:
    """A later revision is stored, so this one arrived before it (from
    this or another device): read the submission again."""

    receipt: views.Receipt


@dataclass(frozen=True)
class Different:
    """A revision with this number but another hash is stored (another
    device sent one): sending this one again would be refused as stale."""

    receipt: views.Receipt


def resolve_unconfirmed(api: StandupApi, entry: SubmissionEntry,
                        on_done: Callable[[object], None],
                        on_failure: Callable[[StandupError], None]) -> None:
    """Did the revision in ``entry`` arrive, although its request got no
    answer? Asked with the entry's status key, so no signature.

    ``on_done`` gets :class:`~.views.Confirmed` (same revision and hash,
    every file found), :class:`~.views.BlobsMissing` (stored, files
    missing: upload and send the same body again), :class:`NotStored`,
    :class:`Superseded` or :class:`Different`. ``on_failure`` when the
    server could not be asked: the entry stays unconfirmed, ask later."""
    if not entry.status_key:
        on_failure(StandupError("refused", message="the entry has no status key"))
        return

    def found(items: Dict[str, views.LookupItem]) -> None:
        item = items.get(entry.status_key)
        if item is None or item.receipt.submission_id != entry.sid:
            on_done(NotStored())
            return
        receipt = item.receipt
        if views.lookup_confirms(receipt, submission_id=entry.sid, revision=entry.revision,
                                 revision_hash=entry.revision_hash):
            if receipt.blobs_missing:
                on_done(views.BlobsMissing(receipt=receipt, missing=receipt.blobs_missing))
            else:
                on_done(views.Confirmed(receipt=receipt, repeat=True))
        elif receipt.revision < entry.revision:
            on_done(NotStored(receipt))
        elif receipt.revision > entry.revision:
            on_done(Superseded(receipt))
        else:
            on_done(Different(receipt))

    api.lookup([(entry.status_key, 0)], found, on_failure)


__all__ = [
    "Different", "Followed", "NotStored", "REFRESH_INTERVAL_MS", "SubmissionWatcher",
    "Superseded", "resolve_unconfirmed",
]
