# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writer's two private lists of STANDUP submissions (editorial
contract 13), kept on the account's own relays as kind 30078 events
encrypted to itself, on the shared store (``nostr/encrypted_lists.py``):

- :class:`SubmissionsList` (``d`` = ``einundzwanzig:submissions``): one
  entry per submission with what following and revising it needs, the
  secrets included (``s_sub``, the submission key, and ``status_key``,
  which the lookup takes instead of a signature). Merged by ``sid``, the
  newest ``updated_at`` wins; a removal is a tombstone that stays.
- :class:`ReadState` (``d`` = SHA-256 of
  ``"einundzwanzig:read-state:v1:" + pubkey``, so relays cannot tell
  what it is): the last ``seq`` read per submission, merged by the
  highest. Written at most every 5 minutes, and when leaving the app.

Both are split into pages of at most 28,672 bytes, so every page fits
one call through a signer app over any connection (criterion 22 of the
plan; contract 12.5 "Pages fit every connection", 007ded2), bound into
one generation by page 0's ``parts`` (``nostr/paged_list.py``). Both
merges are unions, so a read that missed a page (``complete`` False)
still holds every entry it found, and the next write is complete again.
What the lists hold, and how it merges, is ``nostr/standup/entries.py``;
this module reads and writes it.

**Before sending.** A submission's entry is written before the
submission is sent (12.4, "No answer after sending"): only with
``s_sub`` and ``status_key`` stored can the app find out later whether a
request that got no answer arrived, and read what it sent. So
:meth:`SubmissionsList.record` answers a :class:`RecordResult`, and the
caller sends only when it is ``stored``: when the list now on the relays
holds exactly that entry (its ``sid``, revision, revision hash,
``s_sub`` and ``status_key``). Another device may hold the same ``sid``
with a newer ``updated_at``, or a tombstone, and win the merge; then the
entry is ``SUPERSEDED`` and must not be sent. Until it is stored, an
entry waits apart from what the list holds here (``pending``), so one
that was never stored is not written later by an unrelated record.

**Where the secrets live.** In memory, for the account that is signed
in, and in the encrypted list; never in a plain file. There is no local
cache: at launch the list is read from the relays (one decryption per
page, as the drafts and feed lists already do), and :meth:`stop` (on
sign-out or another account) drops it from memory. A cache would have to
be encrypted with the account's key, which on a signer app means a
decryption per launch anyway, so it would save nothing. Entries never
print their secrets (``repr``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import N_, _

from ..encrypted_lists import EncryptedList
from ..later import answer_later
from ..list_results import ListRead, ListWords, PublishResult, PublishStatus, ReadStatus
from ..paged_list import Paging
from . import formats
from .entries import (
    ListItem, RecordResult, RecordStatus, SubmissionEntry, SubmissionItems,
    Tombstone, decode_items, decode_seen, encode_items, entry_problem, higher_seen,
    holds_exactly, item_for, merge_items, merge_seen, takes_place_of,
)

SUBMISSIONS_UNREADABLE = N_("Your list of submissions couldn't be read.")
SUBMISSIONS_SIGNER_SILENT = N_(
    "Your signer app didn't answer, so your list of submissions couldn't be read.")
SUBMISSIONS_OLD_ENCRYPTION = N_(
    "Your list of submissions was saved with an older kind of encryption. Open it once "
    "in the app that saved it; until then, nothing new can be submitted here.")
SUBMISSIONS_MISSING_PAGE = N_(
    "Part of your list of submissions hasn't arrived yet. What did is shown, and the list "
    "is made whole again the next time it is saved.")
# A page longer than the connection to the signer app carries: written
# before every page fitted every connection (contract 12.5). A connection
# made with the code goes over relays that carry it.
SUBMISSIONS_TOO_LONG_FOR_CONNECTION = N_(
    "The connection to your signer app carries only short messages, and your list of "
    "submissions was saved in longer pieces. Connect your signer app again to read it; until "
    "then, nothing new can be submitted here.")
READ_STATE_UNREADABLE = N_("What you have read couldn't be loaded.")
READ_STATE_SIGNER_SILENT = N_(
    "Your signer app didn't answer, so what you have read couldn't be loaded.")
READ_STATE_OLD_ENCRYPTION = N_(
    "What you have read was saved with an older kind of encryption and is left as it is.")
READ_STATE_TOO_LONG_FOR_CONNECTION = N_(
    "The connection to your signer app carries only short messages, and what you have read "
    "was saved in longer pieces. Connect your signer app again to load it.")

# 13: written at most this often, when leaving the app, and before signing
# out, which waits for that write at most this long.
READ_STATE_INTERVAL_SECONDS = 5 * 60
SIGN_OUT_WAIT_MS = 5_000

def _submissions_words() -> ListWords:
    return ListWords(unreadable=_(SUBMISSIONS_UNREADABLE),
                     signer_silent=_(SUBMISSIONS_SIGNER_SILENT),
                     old_encryption=_(SUBMISSIONS_OLD_ENCRYPTION),
                     missing_page=_(SUBMISSIONS_MISSING_PAGE),
                     too_long_for_connection=_(SUBMISSIONS_TOO_LONG_FOR_CONNECTION))


def _read_state_words() -> ListWords:
    return ListWords(unreadable=_(READ_STATE_UNREADABLE),
                     signer_silent=_(READ_STATE_SIGNER_SILENT),
                     old_encryption=_(READ_STATE_OLD_ENCRYPTION),
                     too_long_for_connection=_(READ_STATE_TOO_LONG_FOR_CONNECTION))


# --------------------------------------------------------------------------- #
# The lists                                                                    #
# --------------------------------------------------------------------------- #

class _AccountList(QObject):
    """One account's list on the shared store: the version last seen on
    the relays (base), the one here (local), and a generation, so an
    answer for an account that was left changes nothing."""

    status = Signal(str)

    def __init__(self, profile, *, d_tag: str, decode, encode, merge, words: ListWords,
                 paging: Paging, empty, session_pool, relay_directory, query, publisher,
                 clock: Callable[[], float], entitled_relays=None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._profile = profile
        self._clock = clock
        self._empty = empty
        self._list = EncryptedList(
            d_tag=d_tag, decode=decode, encode=encode, merge=merge, words=words,
            session_pool=session_pool, relay_directory=relay_directory, query=query,
            publisher=publisher, clock=lambda: int(clock()), entitled_relays=entitled_relays,
            paging=paging, untagged="nip44", on_status=self.status.emit, name=d_tag,
            parent=self)
        self._base: Any = None
        self._base_mark: Optional[Tuple[int, str]] = None
        self._local: Any = empty()
        self._generation = 0
        # Stopped with its account: never read or written again, so a late
        # caller never asks the account's signer (lesson 31).
        self._stopped = False
        self.loaded = False
        # False after a read that missed a page or found one of another
        # generation, until a write made the list whole again.
        self.complete = True

    @property
    def account(self) -> str:
        return self._profile.user_pubkey

    def stop(self) -> None:
        """Sign-out or another account: forget everything, answer nothing
        still on its way, and never read or write again: a later read or
        write is refused on a later turn, so a late caller asks no signer
        for an account that was let go (RG-M2r 3, lesson 31)."""
        self._stopped = True
        self._generation += 1
        self._base = None
        self._base_mark = None
        self._local = self._empty()
        self.loaded = False
        self.complete = True

    def load(self, on_done: Callable[[ListRead], None]) -> None:
        """Read the list: ``on_done(read)``, its ``value`` what is here
        now (the list read merged with what was here), or ``read.status``
        NO_ANSWER, BLOCKED (a page could not be opened now: offer to try
        again) or UNREADABLE, with ``read.words`` (also said through
        :attr:`status`). Nothing is written while it is not readable."""
        if self._stopped:
            answer_later(on_done, ListRead(ReadStatus.NO_ANSWER))
            return
        generation = self._generation

        def read(result: ListRead) -> None:
            if generation != self._generation:
                return
            if not result.readable:
                on_done(result)
                return
            remote = result.value if result.value is not None else self._empty()
            self._base = remote
            event = result.event
            self._base_mark = (int(event["created_at"]), event["id"]) if event else None
            self._local = self._merge_local(remote)
            self.loaded = True
            self.complete = result.status is not ReadStatus.INCOMPLETE
            on_done(replace(result, value=self._local))

        self._list.relays(self._profile, lambda relays: self._list.read(
            self._profile, relays, read), reading=True)

    def _merge_local(self, remote):
        raise NotImplementedError

    def _outgoing(self):
        """What a write puts on the relays from here: what the list holds
        here (a subclass adds what waits to be stored)."""
        return self._local

    def _publish(self, on_done: Callable[[PublishResult], None]) -> None:
        if self._stopped:
            answer_later(on_done, PublishResult(PublishStatus.FAILED,
                                                reason="the account was let go"))
            return
        generation = self._generation

        def merged(value, remote, event) -> None:
            if generation != self._generation or remote is None:
                return
            # What the relays had joins what is here; what this write
            # adds joins it only once the write has stored it.
            self._local = self._merge_local(remote)

        def done(result: PublishResult) -> None:
            if generation != self._generation:
                return
            if result.status is PublishStatus.SENT:
                self._base = result.value
                self._base_mark = (int(result.signed["created_at"]), result.signed["id"])
                self.complete = True
            elif result.status is PublishStatus.UP_TO_DATE:
                self._base = result.remote
            on_done(result)

        self._list.publish(self._profile,
                           state=lambda: (self._base, self._outgoing(), self._base_mark),
                           on_done=done, on_merged=merged)


class SubmissionsList(_AccountList):
    """The writer's submissions (13) for one account."""

    def __init__(self, profile, **boundaries) -> None:
        super().__init__(
            profile, d_tag=formats.SUBMISSIONS_LIST_D, decode=decode_items,
            encode=encode_items, merge=lambda base, local, remote: self._guarded(
                remote or SubmissionItems(), local),
            words=_submissions_words(), paging=Paging(field="items"), empty=SubmissionItems,
            **boundaries)
        # Entries (and tombstones) recorded whose write has not ended yet,
        # by sid: written with every write, held here only once stored.
        self._pending: Dict[str, Union[SubmissionEntry, Tombstone]] = {}
        # Per pending sid, the entry whose keys it may take the place of:
        # its own (a later revision), one proven never stored (replaces),
        # or the one a tombstone takes back.
        self._may_overwrite: Dict[str, SubmissionEntry] = {}

    def stop(self) -> None:
        super().stop()
        self._pending = {}
        self._may_overwrite = {}

    def _known(self, sid: str) -> Optional[ListItem]:
        """The newest item of ``sid`` known here: held, last read, waiting."""
        found = [item for source in (self._local, self._base or SubmissionItems())
                 for item in source.items if item.sid == sid]
        waiting = self._pending.get(sid)
        if waiting is not None:
            found.append(waiting)
        return max(found, key=lambda item: item.updated_at, default=None)

    def _overwrites_other_keys(self, item: Optional[ListItem], sid: str) -> bool:
        """Whether writing the pending item of ``sid`` would replace
        ``item`` with other secrets (:func:`takes_place_of`)."""
        mine = self._may_overwrite.get(sid)
        return mine is not None and takes_place_of(item, mine)

    def _guarded(self, remote: SubmissionItems, local: SubmissionItems) -> SubmissionItems:
        """:func:`merge_items`, except that what waits here never takes
        the place of another run's entry on the relays (another device
        submitted the same text meanwhile): that one stays, and the write
        here comes back SUPERSEDED."""
        merged = merge_items(remote, local)
        kept = {sid for sid in self._pending
                if self._overwrites_other_keys(item_for(remote, sid), sid)}
        if not kept:
            return merged
        theirs = {item.sid: item for item in remote.items if item.sid in kept}
        return SubmissionItems(items=tuple(theirs.get(item.sid, item) if item.sid in kept
                                           else item for item in merged.items))

    def _merge_local(self, remote: SubmissionItems) -> SubmissionItems:
        return merge_items(remote, self._local)

    def _outgoing(self) -> SubmissionItems:
        return merge_items(self._local, SubmissionItems(tuple(self._pending.values())))

    def entries(self) -> List[SubmissionEntry]:
        return list(self._local.entries)

    def entry(self, sid: str) -> Optional[SubmissionEntry]:
        return next((e for e in self._local.entries if e.sid == sid), None)

    def record(self, entry: SubmissionEntry, on_done: Callable[[RecordResult], None]) -> None:
        """Add or update ``entry`` and write the list; ``on_done`` gets a
        :class:`RecordResult`. Send the submission only when it is
        ``stored``. ``updated_at`` becomes at least now, and later than
        every item of its ``sid`` known here (an entry, a tombstone, an
        item this app cannot read), so this edit wins the merge unless
        another device wrote a newer one meanwhile (13: the newest
        ``updated_at`` wins); then it is SUPERSEDED.

        An item of the same ``sid`` with other secrets is never replaced
        (:func:`takes_place_of`): NOT_STORED when it is known here,
        SUPERSEDED when it is found on the relays. A new entry for a
        ``sid`` held keeps that entry's keys (drafting.seal)."""
        self.record_many([entry], lambda results: on_done(results[0]))

    def record_many(self, entries: Sequence[SubmissionEntry],
                    on_done: Callable[[List[RecordResult]], None]) -> None:
        """:meth:`record` for several entries, written at once (one
        signature per page changed, not per entry); ``on_done`` gets a
        result per entry, in order."""
        now = int(self._clock())
        fresh: List[SubmissionEntry] = []
        for entry in entries:
            problem = entry_problem(entry) or (
                "other secrets are on the list under this sid"
                if takes_place_of(self._known(entry.sid), entry) else None)
            if problem:
                failed = PublishResult(PublishStatus.FAILED, reason=problem)
                answer_later(on_done, [RecordResult(RecordStatus.NOT_STORED, e, failed)
                                       for e in entries])
                return
            fresh.append(replace(entry, updated_at=max(entry.updated_at, now,
                                                       self._latest(entry.sid) + 1)))
        for entry in fresh:
            self._pending[entry.sid] = entry
            self._may_overwrite[entry.sid] = entry
        self._write(fresh, on_done)

    def forget(self, entry: SubmissionEntry, on_done: Callable[[RecordResult], None]) -> None:
        """Take back ``entry``, recorded for a request that was never sent
        (12.4): a tombstone for its ``sid``, so no device takes the keys
        of a submission that does not exist for one. Only while the list
        holds exactly this entry: another run's keys are never removed
        (NOT_STORED here, SUPERSEDED when the relays hold them)."""
        if takes_place_of(self._known(entry.sid), entry):
            failed = PublishResult(PublishStatus.FAILED, reason="another run's entry")
            answer_later(on_done, RecordResult(RecordStatus.NOT_STORED, entry, failed))
            return
        tombstone = Tombstone(sid=entry.sid, updated_at=max(int(self._clock()),
                                                            self._latest(entry.sid) + 1))
        self._pending[entry.sid] = tombstone
        self._may_overwrite[entry.sid] = entry
        self._write([tombstone], lambda results: on_done(replace(results[0], entry=entry)))

    def _write(self, fresh: Sequence[Union[SubmissionEntry, Tombstone]],
               on_done: Callable[[List[RecordResult]], None]) -> None:
        generation = self._generation

        def written(result: PublishResult) -> None:
            if generation != self._generation:
                return
            on_list = (result.value if result.status is PublishStatus.SENT
                       else result.remote if result.status is PublishStatus.UP_TO_DATE
                       else None)
            outcomes = [self._settle(item, result, on_list) for item in fresh]
            on_done(outcomes)

        self._publish(written)

    def _latest(self, sid: str) -> int:
        """The newest ``updated_at`` of any item of ``sid`` known here
        (held, last seen on the relays, waiting), or -1."""
        known = [item.updated_at for source in (self._local, self._base or SubmissionItems())
                 for item in source.items if item.sid == sid]
        waiting = self._pending.get(sid)
        if waiting is not None:
            known.append(waiting.updated_at)
        return max(known, default=-1)

    def _settle(self, entry: SubmissionEntry, result: PublishResult,
                on_list: Optional[SubmissionItems]) -> RecordResult:
        """The outcome of ``entry``'s write: stored only when the list now
        on the relays holds exactly it; then, and only then, it is held
        here. Either way it no longer waits (unless a newer record of the
        same sid took its place)."""
        if self._pending.get(entry.sid) is entry:
            del self._pending[entry.sid]
            self._may_overwrite.pop(entry.sid, None)
        if on_list is None:
            return RecordResult(RecordStatus.NOT_STORED, entry, result)
        if (item_for(on_list, entry.sid) == entry if isinstance(entry, Tombstone)
                else holds_exactly(on_list, entry)):
            self._local = merge_items(self._local, SubmissionItems((entry,)))
            return RecordResult(RecordStatus.STORED, entry, result)
        return RecordResult(RecordStatus.SUPERSEDED, entry, result,
                            on_list=item_for(on_list, entry.sid))


class ReadState(_AccountList):
    """What the account has read: the last ``seq`` per submission (13)."""

    def __init__(self, profile, **boundaries) -> None:
        super().__init__(
            profile, d_tag=formats.read_state_d(profile.user_pubkey),
            decode=decode_seen, encode=lambda value, now: {"seen": dict(value)},
            merge=lambda base, local, remote: merge_seen(remote or {}, local),
            words=_read_state_words(), paging=Paging(field="seen", combine=higher_seen),
            empty=dict, **boundaries)
        self._dirty = False
        self._last_write: Optional[float] = None

    def _merge_local(self, remote):
        return merge_seen(remote, self._local)

    def seen(self, sid: str) -> int:
        value = self._local.get(sid, 0)
        return value if formats.is_int(value) else 0

    def mark(self, sid: str, seq: int) -> None:
        """``sid`` read up to ``seq`` (kept here until :meth:`flush`); a
        ``seq`` not beyond the one known changes nothing."""
        if not (formats.is_hex64(sid) and formats.is_int(seq)):
            raise ValueError("a submission id and a seq")
        if seq > self.seen(sid):
            self._local = dict(self._local, **{sid: seq})
            self._dirty = True

    @property
    def dirty(self) -> bool:
        """Something to write: a seq read here, or a list read incomplete,
        which the next write makes whole."""
        return self._dirty or not self.complete

    def flush(self, on_done: Optional[Callable[[Optional[PublishResult]], None]] = None, *,
              leaving: bool = False) -> bool:
        """Write what was read, at most every 5 minutes, or now when
        ``leaving``. False (and ``on_done(None)``) when nothing is written
        yet; True when a write started."""
        done = on_done or (lambda _result: None)
        now = self._clock()
        if not self.dirty or (not leaving and self._last_write is not None
                              and now - self._last_write < READ_STATE_INTERVAL_SECONDS):
            answer_later(done, None)
            return False
        self._last_write = now
        self._dirty = False

        def written(result: PublishResult) -> None:
            if not result.on_relays:
                self._dirty = True
            done(result)

        self._publish(written)
        return True

    def before_sign_out(self, on_done: Callable[[Optional[PublishResult]], None], *,
                        wait_ms: int = SIGN_OUT_WAIT_MS) -> None:
        """Write what was read before the account signs out (13), and
        answer ``on_done`` once: with the write's result when it ends
        within ``wait_ms``, else (or with nothing to write) with None.
        Sign out, and :meth:`stop`, after that answer; a write that comes
        later is not heard (the signer goes with the account)."""
        answered = []

        def answer(result: Optional[PublishResult]) -> None:
            if not answered:
                answered.append(result)
                on_done(result)

        if self.flush(answer, leaving=True):
            # Not bound to this list: deleted meanwhile, it must not keep
            # the sign-out waiting.
            QTimer.singleShot(max(0, int(wait_ms)), lambda: answer(None))

    def stop(self) -> None:
        super().stop()
        self._dirty = False
        self._last_write = None


__all__ = [
    "READ_STATE_INTERVAL_SECONDS", "READ_STATE_OLD_ENCRYPTION", "READ_STATE_SIGNER_SILENT",
    "READ_STATE_UNREADABLE", "SIGN_OUT_WAIT_MS", "SUBMISSIONS_MISSING_PAGE",
    "SUBMISSIONS_OLD_ENCRYPTION", "SUBMISSIONS_SIGNER_SILENT", "SUBMISSIONS_UNREADABLE",
    "ReadState", "SubmissionsList",
]
