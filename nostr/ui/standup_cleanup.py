# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writer's app removes a published work's encrypted files from the
media servers (plan 9.6; M-4 M14; :mod:`nostr.standup.cleanup` says
what goes and keeps the record), as the web app's ``blobCleanup.js``.

Nothing is deleted on the word of a View: the work counts as published
as the newest ``published`` report whose own event proves the address
says (``Reading.published``), and every revision is verified before its
signed blob list is read; when one after the published revision does
not verify, what it names is unknown and nothing is deleted. A file
another submission of this account names stays (only its last user
removes it).

When: with the key on this computer, whenever the detail of a published
submission of the writer's was read and the cleanup is due (no one is
asked). Through a signer app never at launch, and calmly (the lead's fix
to the M4 open list, RG-M4-c 5): right after the person published with
it here, that submission's own, and only while the line that says so is
on screen (RG-M4-b 12; a sheet closed meanwhile, or a report that
arrived later, leaves it to the ride); any other due submission read in
this run only as the account's one ride-along
(:class:`~.standup_unlisted.RideAlong`: after a signer action the person
took, at most one such request per run). Each request through a signer
app is one authorization of at most 32 files (RG-M4-a 10); the rest
wait for the next. A signature refused ends the cleanup for the run,
and nothing is asked once the account was let go.

It is quiet either way (lessons 36): housekeeping the person never asked
for. A server that failed is asked again after a pause, for seven days.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject

from ..blossom.store import delete_batches
from ..standup import cleanup
from .standup_account import live


class BlobCleanup(QObject):
    """One per account's Following. ``store`` is the media store (None:
    nothing is deleted); ``cleanups`` the record (:class:`cleanup.Cleanups`)."""

    def __init__(self, following, account, profile, *, store,
                 cleanups: Optional[cleanup.Cleanups] = None,
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._following, self._account, self._profile = following, account, profile
        self._store, self._clock = store, clock
        self._cleanups = cleanups or cleanup.Cleanups(account.writer)
        self._running: set = set()
        self._unknown: set = set()        # a revision did not verify: not again this run
        self._waiting: set = set()         # published here, its report being read
        self._resting = False              # the signer app refused: not again this run
        # sid -> what says, right before the signer app is asked, that it
        # asks once more and why (RG-M4-b 12): False when that line is not
        # on screen any more, so nothing is asked.
        self._telling: Dict[str, Callable[[], bool]] = {}
        following.detail_changed.connect(self._read)
        following.ride_along.join(self._ride)

    @property
    def _here(self) -> bool:
        return bool(getattr(self._profile, "is_local", False))

    def _read(self, sid: str) -> None:
        """A detail was read (also one this cleanup asked for)."""
        if self._here:
            self._clean([sid])
        elif sid in self._waiting and self._due(sid) and not self._resting:
            self._waiting.discard(sid)
            self._clean([sid], one_request=True)     # said right before it asks

    def _ride(self) -> bool:
        """The ride-along through a signer app: one due submission read in
        this run is cleaned now (with the key here none is needed)."""
        if self._here or self._resting or self._account.stopped:
            return False
        due = next((sid for sid in self._following.reading.read_sids() if self._due(sid)), None)
        if due is None:
            return False
        self._clean([due], one_request=True, riding=True)
        return True

    def after_publishing(self, sid: str,
                         on_asking: Optional[Callable[[], bool]] = None) -> None:
        """The person just published ``sid`` with their signer here: its
        report is read with the submission, then the files go.
        ``on_asking()`` comes right before the signer app is asked for
        them, and says whether its line is on screen (never with the key on
        this computer: no one is asked); without it nothing is asked."""
        self._waiting.add(sid)
        if on_asking is not None and not self._here:
            self._telling[sid] = on_asking
        self._following.open(sid)

    # -- what goes ---------------------------------------------------------------------------

    def _due(self, sid: str) -> bool:
        row = self._following.row(sid)
        view = row.view if row is not None else None
        return (self._store is not None and sid not in self._running | self._unknown
                and view is not None
                and view.writer == self._account.writer and view.state == "published"
                and cleanup.due(self._cleanups.entry(sid), self._clock())
                and self._following.reading.published(sid) is not None)

    def _clean(self, sids: Sequence[str], *, one_request: bool = False,
               riding: bool = False) -> None:
        """``one_request``: one authorization at most; ``riding``: the run's
        ride-along, asked unannounced."""
        sids = [sid for sid in dict.fromkeys(sids) if self._due(sid)]
        if not sids:
            return
        self._running.update(sids)
        plan: Dict[str, Tuple[Optional[dict], List[cleanup.Pair]]] = {}

        def next_one(rest: List[str]) -> None:
            if not rest:
                self._delete(plan, one_request=one_request, riding=riding)
                return
            sid = rest[0]

            def with_records(records) -> None:
                if records is not None:
                    published = self._following.reading.published(sid)
                    pairs = cleanup.to_delete(
                        records, published.revision or 0, self._store.target_servers(),
                        self._account.media_keys.named_besides(sid))
                    plan[sid] = (self._cleanups.entry(sid), pairs)
                else:
                    self._running.discard(sid)
                    self._unknown.add(sid)
                next_one(rest[1:])

            self._records(sid, with_records)

        next_one(sids)

    def _records(self, sid: str, done: Callable) -> None:
        """``done([(revision, blobs)])`` of revisions 1 to the current one,
        verified here; ``done(None)`` when one after the published one
        does not verify (what it names is unknown) or it is not known."""
        row, published = self._following.row(sid), self._following.reading.published(sid)
        if row is None or row.view is None or published is None or not published.revision:
            done(None)
            return
        last, records = row.view.revision, []

        def read(n: int) -> None:
            if n > last:
                done(records)
                return

            def got(version) -> None:
                if version is not None and version.opened is not None:
                    records.append((n, version.opened.verified.blobs))
                elif n > published.revision:
                    done(None)
                    return
                read(n + 1)

            self._following.reading.version(sid, n, live(self._account, got))

        read(1)

    # -- deleting ----------------------------------------------------------------------------

    def _delete(self, plan: Dict[str, Tuple[Optional[dict], List[cleanup.Pair]]], *,
                one_request: bool = False, riding: bool = False) -> None:
        """One authorization per submission (RG-M4-a 10; the store splits
        one naming more files than a server reads): one that fails fails
        only its own submission, which is asked again after its pause."""
        now = self._clock()
        rest = list(plan.items())

        def next_one() -> None:
            if not rest:
                return
            sid, (entry, pairs) = rest.pop(0)

            def deleted(gone: List[cleanup.Pair]) -> None:
                self._cleanups.keep(sid, cleanup.after_try(entry, pairs, gone, now))
                self._running.discard(sid)
                next_one()

            asked = cleanup.left(entry, pairs)
            if asked and one_request:
                asked = delete_batches(asked)[0]      # the rest wait for the next
            tell = self._telling.pop(sid, None)
            if asked and not self._here and not riding and not (tell is not None and tell()):
                self._running.discard(sid)            # not said: left to the ride
                next_one()
            elif asked:
                self._store.delete_on_servers(asked, live(self._account, deleted),
                                              going=lambda: not self._account.stopped,
                                              on_not_signed=self._rest)
            else:
                deleted([])

        next_one()

    def _rest(self) -> None:
        """The signer app refused (or did not answer): no cleanup asks it
        again in this run."""
        self._resting = True


__all__ = ["BlobCleanup"]
