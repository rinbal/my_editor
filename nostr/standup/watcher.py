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

:func:`classify` says what the server's latest receipt means for the
revision an entry records: confirmed, stored with files missing, not
stored, superseded or different. Every :class:`Followed` carries it as
its ``status``, and :func:`resolve_unconfirmed`, the rule of contract
12.4 for a request that got no answer, asks it too: an entry recorded
for a revision that never arrived never reads as the revision before.

A lookup answers at most a byte budget of timeline items, shared by
every submission it answers (contract 15; the backend's TimelineBudget,
fde04a3): a submission behind others may come back with nothing and
``"more": true``. A round asks again, with ``since``, for every
submission that says more, those that got nothing first, until the
server says there is no more. A submission that got nothing although
none ahead of it took any of the budget makes the round fail for it,
and so do :data:`MAX_CONTINUATIONS` answers in a row that say more: the
one rule for a round and for :func:`read_submission`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from PySide6.QtCore import QObject, QTimer, Signal

from . import answers, formats, views
from .answers import LookupAnswer
from .client import StandupApi
from .errors import Code, StandupError
from .entries import SubmissionEntry, same_sent

REFRESH_INTERVAL_MS = 15 * 60 * 1000
# Answers in a row that may say "more" before a round or a reading fails
# (a server must not hold the reader in a loop).
MAX_CONTINUATIONS = 20


def _nothing_new() -> StandupError:
    return StandupError(Code.BAD_RESPONSE, message="more was promised and nothing came")


def _too_long() -> StandupError:
    return StandupError(Code.BAD_RESPONSE,
                        message=f"more was still promised after {MAX_CONTINUATIONS} answers")


# --------------------------------------------------------------------------- #
# What a receipt says about an entry (12.4)                                    #
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


@dataclass(frozen=True)
class StoredOther:
    """This revision, its number and hash, is stored, but with another
    envelope or other wraps than this app sent (12.4 "No answer after
    sending"): never "Eingereicht"."""

    receipt: views.Receipt


EntryStatus = Union[views.Confirmed, views.BlobsMissing, NotStored, Superseded, Different,
                    StoredOther]


def classify(entry: SubmissionEntry, receipt: views.Receipt) -> EntryStatus:
    """What ``receipt``, the server's receipt of the submission's latest
    revision, says about the revision ``entry`` records: confirmed only
    when its number, its hash, the envelope and every wrap are the ones
    the entry says were sent (also after a restart, with no body to
    compare). Pure; the one rule for the watcher and for a request that
    got no answer."""
    if views.same_revision(receipt, submission_id=entry.sid, revision=entry.revision,
                           revision_hash=entry.revision_hash):
        if not same_sent(entry, receipt):
            return StoredOther(receipt)
        if receipt.blobs_missing:
            return views.BlobsMissing(receipt=receipt, missing=receipt.blobs_missing)
        return views.Confirmed(receipt=receipt, repeat=True)
    if receipt.revision < entry.revision:
        return NotStored(receipt)
    if receipt.revision > entry.revision:
        return Superseded(receipt)
    return Different(receipt)


@dataclass(frozen=True)
class Followed:
    """What the server says about one submission: its view, the receipt
    of its latest revision, every timeline item seen so far, and what
    that receipt says about the revision the entry records
    (:func:`classify`): only :class:`~.views.Confirmed` is "Eingereicht"."""

    entry: SubmissionEntry
    item: views.LookupItem
    timeline: tuple
    status: EntryStatus

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
      failed(object)  a server could not be asked, or its answer for a
                      submission could not be read (StandupError); what
                      was known stays, and the next round tries again.
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
             learned: Dict[str, Followed], one_done: Callable[[], None],
             continuation: int = 0) -> None:
        by_key = {entry.status_key: entry for entry in batch}

        def found(answer: LookupAnswer) -> None:
            if generation != self._generation:
                return
            waited: List[SubmissionEntry] = []      # got nothing: the budget was spent
            grew: List[SubmissionEntry] = []
            spent = False                           # an item ahead took some of the budget
            for key, item in answer.found.items():
                entry = by_key[key]
                if item.submission.submission_id != entry.sid:
                    self.failed.emit(StandupError(
                        Code.BAD_RESPONSE, message="the answer is for another submission"))
                    continue
                before = self._since(entry, learned)
                learned[entry.sid] = self._followed_with(entry, item, learned)
                if item.more and learned[entry.sid].last_seq > before:
                    grew.append(entry)
                elif item.more and spent:
                    waited.append(entry)
                elif item.more:
                    self.failed.emit(_nothing_new())
                spent = spent or bool(item.timeline)
            if answer.unreadable:
                self.failed.emit(StandupError(
                    Code.BAD_RESPONSE, message=f"{len(answer.unreadable)} answers unreadable"))
            more = waited + grew                    # those that waited are served first
            if more and continuation >= MAX_CONTINUATIONS:
                self.failed.emit(_too_long())
            elif more:
                self._ask(more, generation, learned, one_done, continuation + 1)
                return
            one_done()

        def failed(error: StandupError) -> None:
            if generation != self._generation:
                return
            self.failed.emit(error)
            one_done()

        self._client_for(batch[0].server).lookup(
            [(e.status_key, self._since(e, learned)) for e in batch], found, failed)

    def _known(self, sid: str, learned: Dict[str, Followed]) -> Optional[Followed]:
        return learned.get(sid) or self._followed.get(sid)

    def _followed_with(self, entry: SubmissionEntry, item: views.LookupItem,
                       learned: Dict[str, Followed]) -> Followed:
        """What is known of ``entry`` with ``item`` added: its timeline
        grows by what is new, each ``seq`` once."""
        known = self._known(entry.sid, learned)
        timeline = {i.seq: i for i in (known.timeline if known else ())}
        timeline.update({i.seq: i for i in item.timeline})
        return Followed(entry=entry, item=item,
                        timeline=tuple(timeline[seq] for seq in sorted(timeline)),
                        status=classify(entry, item.receipt))

    def _since(self, entry: SubmissionEntry, learned: Dict[str, Followed]) -> int:
        known = self._known(entry.sid, learned)
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
# Reading one submission whole                                                 #
# --------------------------------------------------------------------------- #

def read_submission(api: StandupApi, submission_id: str,
                    on_done: Callable[[answers.SubmissionDetail], None],
                    on_failure: Callable[[StandupError], None], *, since: int = 0,
                    since_internal: int = 0, revision: Optional[int] = None) -> None:
    """``GET /api/submissions/<sid>`` until the server says no more
    timeline items wait (``more``, contract 15): ``on_done`` once, with
    the last answer's view, revisions and members and every item of each
    channel after its own cursor (``since`` for the conversation,
    ``since_internal`` for a member's internal notes, feat/redaktion
    ceac858), each (channel, ``seq``) once, in order. An answer that
    promises more and brings nothing new in either channel is a failure,
    and so are :data:`MAX_CONTINUATIONS` answers in a row that promise
    more (a server must not hold the reader in a loop), as in a round."""
    gathered: Dict[str, Dict[int, views.TimelineItem]] = {"conversation": {}, "internal": {}}

    def ask(after: int, after_internal: int, continuation: int) -> None:
        def answered(detail: answers.SubmissionDetail) -> None:
            gathered["conversation"].update({item.seq: item for item in detail.timeline})
            gathered["internal"].update({item.seq: item for item in detail.internal})
            last = max(gathered["conversation"], default=after)
            last_internal = max(gathered["internal"], default=after_internal)
            if detail.more and (last, last_internal) == (after, after_internal):
                on_failure(_nothing_new())
            elif detail.more and continuation >= MAX_CONTINUATIONS:
                on_failure(_too_long())
            elif detail.more:
                ask(last, last_internal, continuation + 1)
            else:
                on_done(replace(detail, more=False, timeline=_in_order(gathered["conversation"]),
                                internal=_in_order(gathered["internal"])))

        api.submission(submission_id, answered, on_failure, since=after,
                       since_internal=after_internal or None, revision=revision)

    ask(since, since_internal, 0)


def _in_order(items: Dict[int, views.TimelineItem]) -> tuple:
    return tuple(items[seq] for seq in sorted(items))


# --------------------------------------------------------------------------- #
# No answer after sending (12.4)                                               #
# --------------------------------------------------------------------------- #

def look_up_all(api: StandupApi, entries: Sequence[SubmissionEntry],
                on_found: Callable[[Dict[str, Optional[views.LookupItem]]], None],
                on_failure: Callable[[StandupError], None]) -> None:
    """What the server has for each of the writer's ``entries`` (1 to
    ``MAX_LOOKUP_ITEMS``), asked with their status keys in one lookup (no
    signature): ``on_found({sid: item})``, item None where it stored
    nothing. ``on_failure`` when the server could not be asked, when it
    has something whose answer could not be read (never "not stored",
    12.4), or when an answer is for another submission. Every lookup of
    entries goes through here (RG-M2b 2). ValueError for an entry
    without a status key."""
    if not entries or any(not entry.status_key for entry in entries):
        raise ValueError("only a writer's entries have a status key")

    def found(answer: LookupAnswer) -> None:
        if any(entry.status_key in answer.unreadable for entry in entries):
            on_failure(StandupError(Code.BAD_RESPONSE, message="an answer is unreadable",
                                    maybe_stored=True))
            return
        items = {entry.sid: answer.found.get(entry.status_key) for entry in entries}
        if any(item is not None and item.receipt.submission_id != sid
               for sid, item in items.items()):
            on_failure(StandupError(Code.BAD_RESPONSE,
                                    message="an answer is for another submission",
                                    maybe_stored=True))
            return
        on_found(items)

    api.lookup([(entry.status_key, 0) for entry in entries], found, on_failure)


def look_up(api: StandupApi, entry: SubmissionEntry,
            on_found: Callable[[Optional[views.LookupItem]], None],
            on_failure: Callable[[StandupError], None]) -> None:
    """:func:`look_up_all` for one entry: ``on_found(item)``, or
    ``on_found(None)`` when the server stored nothing for it."""
    look_up_all(api, [entry], lambda items: on_found(items[entry.sid]), on_failure)


def resolve_unconfirmed(api: StandupApi, entry: SubmissionEntry,
                        on_done: Callable[[object], None],
                        on_failure: Callable[[StandupError], None], *,
                        body: Optional[Mapping[str, Any]] = None,
                        clock: Callable[[], float] = time.time) -> None:
    """Did the revision in ``entry`` arrive, although its request got no
    answer? Asked with the entry's status key (:func:`look_up`), so no
    signature.

    ``on_done`` gets :class:`~.views.Confirmed` (same revision and hash,
    every file found), :class:`~.views.BlobsMissing` (stored, files
    missing: upload and send the same body again), :class:`NotStored`,
    :class:`Superseded`, :class:`Different` or :class:`StoredOther`
    (:func:`classify`, by the entry alone, as after a restart). With
    ``body``, the one that was sent, the receipt found must match it in
    every field as well, as the answer to the request itself must
    (views.check_receipt): anything else of this revision is
    :class:`StoredOther`, never "Eingereicht" (RG-M2b 11). ``on_failure``
    when the server could not be asked or its answer not read: the entry
    stays unconfirmed, ask later."""
    def found(item: Optional[views.LookupItem]) -> None:
        if item is None:
            on_done(NotStored())
            return
        status = classify(entry, item.receipt)
        if body is not None and isinstance(status, (views.Confirmed, views.BlobsMissing)):
            checked = views.check_receipt(body, item.receipt.raw, now=clock(), repeat=True)
            status = StoredOther(item.receipt) if isinstance(checked, views.Mismatch) else checked
        on_done(status)

    look_up(api, entry, found, on_failure)


__all__ = [
    "Different", "EntryStatus", "Followed", "MAX_CONTINUATIONS", "NotStored",
    "REFRESH_INTERVAL_MS", "StoredOther", "SubmissionWatcher", "Superseded", "classify",
    "read_submission",
    "look_up", "look_up_all", "resolve_unconfirmed",
]
