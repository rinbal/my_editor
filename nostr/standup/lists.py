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
``s_sub`` and ``status_key``). Another device may hold a newer record
of the same keys, a tombstone for them, or other keys for the same
``sid`` (one ``sid`` has one set of secrets); then the entry is
``SUPERSEDED`` and must not be sent. Until it is stored, an
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

from .. import failures
from ..decrypted_pages import DecryptedPages
from ..encrypted_lists import EncryptedList
from ..later import answer_later
from ..list_results import ListRead, ListWords, PublishResult, PublishStatus, ReadStatus
from ..paged_list import Paging
from . import formats
from .entries import (
    ForeignItem, RecordResult, RecordStatus, SubmissionEntry, SubmissionItems,
    Tombstone, decode_items, decode_seen, encode_items, entry_problem, higher_seen,
    holds_exactly, is_tombstone, kept_of, merge_items, merge_seen, record_key, record_of,
    records_of, same_keys, shown_for, tombstone_for,
)

# What the person is told when a list cannot be read: one sentence each,
# its cause and what to do (lessons 36), whole in a failure's place.
SUBMISSIONS_UNREADABLE = N_("Your list of submissions couldn’t be read: try again in a moment.")
SUBMISSIONS_OLD_ENCRYPTION = N_(
    "Your list of submissions was saved with an older kind of encryption: open it once in the "
    "app that saved it, then try again here.")
SUBMISSIONS_MISSING_PAGE = N_(
    "Part of your list of submissions hasn’t arrived yet: what did is shown, and the list is "
    "made whole the next time it is saved.")
# A page longer than the connection to the signer app carries: written
# before every page fitted every connection (contract 12.5). A connection
# made with the code goes over relays that carry it.
SUBMISSIONS_TOO_LONG_FOR_CONNECTION = N_(
    "Your list of submissions was saved in pieces too long for the connection to your signer "
    "app: connect your signer app again to read it.")
# What was read is written again on its own (ReadState.flush), so it is
# read again then too: nothing to do.
READ_STATE_UNREADABLE = N_("What you have read couldn’t be loaded: MyEditor tries again on its "
                           "own.")
READ_STATE_SIGNER_SILENT = N_(
    "Your signer app didn’t answer, so what you have read couldn’t be loaded: MyEditor tries "
    "again on its own.")
READ_STATE_OLD_ENCRYPTION = N_(
    "What you have read was saved with an older kind of encryption and is left as it is.")
READ_STATE_TOO_LONG_FOR_CONNECTION = N_(
    "What you have read was saved in pieces too long for the connection to your signer app: "
    "connect your signer app again to load it.")

# 13: written at most this often, when leaving the app, and before signing
# out, which waits for that write at most this long.
READ_STATE_INTERVAL_SECONDS = 5 * 60
SIGN_OUT_WAIT_MS = 5_000

def _submissions_words() -> ListWords:
    return ListWords(unreadable=_(SUBMISSIONS_UNREADABLE),
                     signer_silent=_(failures.SIGNER_SILENT),
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
        # The pages opened already, so a quiet re-read asks the signer
        # only for the ones it has not seen; dropped with the account.
        self._opened = DecryptedPages(profile.user_pubkey)
        self._list = EncryptedList(
            d_tag=d_tag, decode=decode, encode=encode, merge=merge, words=words,
            session_pool=session_pool, relay_directory=relay_directory, query=query,
            publisher=publisher, clock=lambda: int(clock()), entitled_relays=entitled_relays,
            paging=paging, untagged="nip44", on_status=self.status.emit, name=d_tag,
            opened=self._opened, parent=self)
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
        self._opened.clear()
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

        def with_relays(relays) -> None:
            # Let go while its relays were being found: the list is not
            # read, so the signer of an account that was left is not asked.
            if generation == self._generation:
                self._list.read(self._profile, relays, read)

        self._list.relays(self._profile, with_relays, reading=True)

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
                           on_done=done, on_merged=merged,
                           live=lambda: generation == self._generation)


def names_other_secrets(items: SubmissionItems, entry: SubmissionEntry) -> bool:
    """Whether writing ``entry`` would give its ``sid`` secrets the list
    does not name for it (contract 2: one ``sid``, one set of secrets):
    its ``sid`` has records that are not removed, none of its keys; the
    record of its status key holds another ``s_sub``; or that record is
    one this app cannot read (it may be a newer app's revision of it)."""
    live = [item for item in records_of(items, entry.sid) if not is_tombstone(item)]
    mine = record_of(items, entry.sid, record_key(entry))
    if isinstance(mine, ForeignItem) or (isinstance(mine, SubmissionEntry)
                                         and not same_keys(mine, entry)):
        return True
    return bool(live) and all(record_key(item) != record_key(entry) for item in live)


class SubmissionsList(_AccountList):
    """The writer's submissions (13) for one account: records, merged by
    identity (``entries.merge_items``, 13 at 0456312)."""

    def __init__(self, profile, **boundaries) -> None:
        super().__init__(
            profile, d_tag=formats.SUBMISSIONS_LIST_D, decode=decode_items,
            encode=encode_items, merge=lambda base, local, remote: self._merged(
                remote or SubmissionItems(), local),
            words=_submissions_words(), paging=Paging(field="items"), empty=SubmissionItems,
            **boundaries)
        # Records (entries and tombstones) whose write has not ended yet,
        # by identity: written with every write, held here only once
        # stored.
        self._pending: Dict[Tuple[str, str], Union[SubmissionEntry, Tombstone]] = {}

    def stop(self) -> None:
        super().stop()
        self._pending = {}

    def load(self, on_done: Callable[[ListRead], None]) -> None:
        """Read the list (``_AccountList.load``); after a complete read,
        the records :meth:`left_behind` get their tombstones."""
        def read(result: ListRead) -> None:
            on_done(result)
            if result.readable and self.complete:
                self._tidy()

        super().load(read)

    def _tidy(self) -> None:
        """A tombstone for every record :meth:`left_behind`, in one write
        (13 at 0456312, Removing); one already on its way is not written
        twice."""
        behind = [entry for entry in self.left_behind()
                  if (entry.sid, record_key(entry)) not in self._pending]
        if not behind:
            return
        now = int(self._clock())
        stones = [tombstone_for(entry, max(now, self._latest(entry.sid) + 1))
                  for entry in behind]
        for stone in stones:
            self._pending[(stone.sid, record_key(stone))] = stone
        self._write(stones, lambda _results: None)

    def _known(self, sid: str) -> SubmissionItems:
        """Every record of ``sid`` known here, merged: held, last read,
        waiting."""
        found = [item for source in (self._local, self._base or SubmissionItems())
                 for item in records_of(source, sid)]
        found += [item for item in self._pending.values() if item.sid == sid]
        return merge_items(SubmissionItems(tuple(found)), SubmissionItems())

    def _merged(self, remote: SubmissionItems, local: SubmissionItems) -> SubmissionItems:
        """:func:`merge_items`, except that an entry waiting here whose
        keys the relays' list does not name for its ``sid``, which names
        others, is not written (another device submitted the same text
        meanwhile): one ``sid`` has one set of secrets (contract 2), and
        the write here comes back SUPERSEDED."""
        waiting = {id(item) for item in self._pending.values()}
        kept = tuple(item for item in local.items
                     if id(item) not in waiting or not isinstance(item, SubmissionEntry)
                     or not names_other_secrets(remote, item))
        return merge_items(remote, SubmissionItems(kept))

    def _merge_local(self, remote: SubmissionItems) -> SubmissionItems:
        return merge_items(remote, self._local)

    def _outgoing(self) -> SubmissionItems:
        return merge_items(self._local, SubmissionItems(tuple(self._pending.values())))

    def entries(self) -> List[SubmissionEntry]:
        """The entry each submission shows (``entries.shown_for``)."""
        return list(self._local.entries)

    def entry(self, sid: str) -> Optional[SubmissionEntry]:
        """What ``sid`` shows, when it is an entry this app reads."""
        shown = shown_for(self._local, sid)
        return shown if isinstance(shown, SubmissionEntry) else None

    def record(self, entry: SubmissionEntry, on_done: Callable[[RecordResult], None]) -> None:
        """Add or update ``entry`` and write the list; ``on_done`` gets a
        :class:`RecordResult`. Send the submission only when it is
        ``stored``. ``updated_at`` becomes at least now, and later than
        every record of its ``sid`` known here (an entry, a tombstone, an
        item this app cannot read), so this edit is the greater record of
        its identity unless another device wrote a newer one meanwhile;
        then it is SUPERSEDED.

        An entry whose keys no record of its ``sid`` names, while the
        ``sid`` has records, is never written: one ``sid`` has one set of
        secrets (contract 2), a new entry for a ``sid`` held keeps that
        entry's keys (drafting.seal). NOT_STORED then."""
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
                if names_other_secrets(self._known(entry.sid), entry) else None)
            if problem:
                failed = PublishResult(PublishStatus.FAILED, reason=problem)
                answer_later(on_done, [RecordResult(RecordStatus.NOT_STORED, e, failed)
                                       for e in entries])
                return
            fresh.append(replace(entry, updated_at=max(entry.updated_at, now,
                                                       self._latest(entry.sid) + 1)))
        for entry in fresh:
            self._pending[(entry.sid, record_key(entry))] = entry
        self._write(fresh, on_done)

    def mark_stored(self, entries: Sequence[SubmissionEntry],
                    on_done: Callable[[List[RecordResult]], None] = lambda _results: None
                    ) -> None:
        """``entries`` have a receipt held (the run's own, or a lookup's)
        for their sid with their status key: each is marked ``stored`` (13,
        72d5ddc) and the list written. Held here at once, so the next write
        carries the mark even when this one does not end on the relays."""
        marked: List[SubmissionEntry] = []
        for entry in entries:
            # The mark is the status key's (any revision of it): the record
            # held here with that key, a later revision maybe, is marked.
            held = record_of(self._local, entry.sid, record_key(entry))
            base = held if isinstance(held, SubmissionEntry) and same_keys(held, entry) \
                else entry
            if base.role == "writer" and not base.stored:
                marked.append(replace(base, stored=True))
        if not marked:
            answer_later(on_done, [])
            return
        self._local = merge_items(self._local, SubmissionItems(tuple(marked)))
        self.record_many(marked, on_done)

    def forget(self, entry: SubmissionEntry, on_done: Callable[[RecordResult], None]) -> None:
        """Take back ``entry``, recorded for a request that was never sent
        (12.4), discarded (Verwerfen), or left behind once another record
        of its ``sid`` was stored (13 at 0456312): a tombstone keyed by its
        status key, a record of its identity only, so no other entry of
        the ``sid`` is touched. Only where the merge keeps the tombstone:
        never over a stored entry (NOT_STORED, nothing written)."""
        tombstone = tombstone_for(entry, max(int(self._clock()), self._latest(entry.sid) + 1))
        known = record_of(self._known(entry.sid), entry.sid, record_key(entry))
        if known is None or kept_of(kept_of(known, entry), tombstone) is not tombstone:
            failed = PublishResult(PublishStatus.FAILED, reason="no record of these keys is held, "
                                   "or the list keeps it (the server stored it)")
            answer_later(on_done, RecordResult(RecordStatus.NOT_STORED, entry, failed))
            return
        self._pending[(entry.sid, record_key(tombstone))] = tombstone
        self._write([tombstone], lambda results: on_done(replace(results[0], entry=entry)))

    def left_behind(self) -> List[SubmissionEntry]:
        """This account's writer entries that are not stored, of a ``sid``
        whose shown entry is stored with other keys: their secrets were
        never used, and each gets a tombstone (13 at 0456312, Removing)."""
        out: List[SubmissionEntry] = []
        for shown in self._local.entries:
            if not shown.stored:
                continue
            out += [item for item in records_of(self._local, shown.sid)
                    if isinstance(item, SubmissionEntry) and item.role == "writer"
                    and not item.stored]
        return out

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
        """The newest ``updated_at`` of any record of ``sid`` known here
        (held, last seen on the relays, waiting), or -1."""
        return max((item.updated_at for item in self._known(sid).items), default=-1)

    def _settle(self, item: Union[SubmissionEntry, Tombstone], result: PublishResult,
                on_list: Optional[SubmissionItems]) -> RecordResult:
        """The outcome of ``item``'s write: stored only when the list now
        on the relays holds exactly it as its identity's record; then, and
        only then, it is held here. Either way it no longer waits (unless
        a newer record of the same identity took its place)."""
        identity = (item.sid, record_key(item))
        if self._pending.get(identity) is item:
            del self._pending[identity]
        if on_list is None:
            return RecordResult(RecordStatus.NOT_STORED, item, result)
        on = record_of(on_list, item.sid, record_key(item))
        if on == item if isinstance(item, Tombstone) else holds_exactly(on_list, item):
            self._local = merge_items(self._local, SubmissionItems((item,)))
            return RecordResult(RecordStatus.STORED, item, result)
        return RecordResult(RecordStatus.SUPERSEDED, item, result,
                            on_list=on if on is not None else shown_for(on_list, item.sid))


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
    "SUBMISSIONS_OLD_ENCRYPTION", "SUBMISSIONS_UNREADABLE",
    "ReadState", "SubmissionsList",
]
