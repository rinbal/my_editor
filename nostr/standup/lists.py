# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writer's two private lists of STANDUP submissions (editorial
contract 13), kept on the account's own relays as kind 30078 events
encrypted to itself, on the shared store (``nostr/encrypted_lists.py``):

- :class:`SubmissionsList` (``d`` = ``einundzwanzig:submissions``): one
  entry per submission with what following and revising it needs, the
  secrets included (``s_sub``, the submission key, and ``status_key``,
  which the lookup takes instead of a signature). Merged by ``sid``, the
  newest ``updated_at`` wins.
- :class:`ReadState` (``d`` = SHA-256 of
  ``"einundzwanzig:read-state:v1:" + pubkey``, so relays cannot tell
  what it is): the last ``seq`` read per submission, merged by the
  highest. Written at most every 5 minutes, and when leaving the app.

Both are split into pages of at most 32,000 bytes, so every page fits
one call through a signer app (criterion 22 of the plan).

**Before sending.** A submission's entry is written before the
submission is sent (12.4, "No answer after sending"): only with
``s_sub`` and ``status_key`` stored can the app find out later whether a
request that got no answer arrived, and read what it sent. So
:meth:`SubmissionsList.record` answers whether the entry is on the
relays (:func:`stored`), and the caller sends only then.

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

from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, Signal

from i18n import N_, _

from ..encrypted_lists import (
    FAILED,
    NO_ANSWER,
    SENT,
    UNREADABLE,
    UP_TO_DATE,
    EncryptedList,
    ListWords,
    Paging,
    PublishResult,
)
from . import formats
from .crypto import StandupCryptoError, secret_from_hex
from .formats import FormatError

SUBMISSIONS_UNREADABLE = N_("Your list of submissions couldn't be read.")
SUBMISSIONS_SIGNER_SILENT = N_(
    "Your signer app didn't answer, so your list of submissions couldn't be read.")
SUBMISSIONS_OLD_ENCRYPTION = N_(
    "Your list of submissions was saved with an older kind of encryption. Open it once "
    "in the app that saved it; until then, nothing new can be submitted here.")
SUBMISSIONS_MISSING_PAGE = N_(
    "Part of your list of submissions hasn't arrived yet. It is kept as it is.")
READ_STATE_UNREADABLE = N_("What you have read couldn't be loaded.")
READ_STATE_SIGNER_SILENT = N_(
    "Your signer app didn't answer, so what you have read couldn't be loaded.")
READ_STATE_OLD_ENCRYPTION = N_(
    "What you have read was saved with an older kind of encryption and is left as it is.")

# 13: written at most this often, and when leaving the app.
READ_STATE_INTERVAL_SECONDS = 5 * 60

_MAX_TEXT = 1_000


def _submissions_words() -> ListWords:
    return ListWords(unreadable=_(SUBMISSIONS_UNREADABLE),
                     signer_silent=_(SUBMISSIONS_SIGNER_SILENT),
                     old_encryption=_(SUBMISSIONS_OLD_ENCRYPTION),
                     missing_page=_(SUBMISSIONS_MISSING_PAGE))


def _read_state_words() -> ListWords:
    return ListWords(unreadable=_(READ_STATE_UNREADABLE),
                     signer_silent=_(READ_STATE_SIGNER_SILENT),
                     old_encryption=_(READ_STATE_OLD_ENCRYPTION))


# --------------------------------------------------------------------------- #
# An entry                                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SubmissionEntry:
    """One submission in the writer's list (13). ``s_sub`` and
    ``status_key`` are hex; a contributor's entry has no status key, and
    no ``s_sub`` until they opened their wrap."""

    sid: str
    publication: str
    server: str
    role: str
    s_sub: Optional[str] = field(repr=False)
    status_key: Optional[str] = field(repr=False)
    title: str
    kind: str
    d: str
    round: int
    revision: int
    revision_hash: str
    updated_at: int

    def to_json(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in _ENTRY_FIELDS}


_ENTRY_FIELDS = tuple(f.name for f in fields(SubmissionEntry))


def entry_problem(entry: SubmissionEntry) -> Optional[str]:
    """Why ``entry`` is not one the list may hold, or None."""
    try:
        formats.need(formats.is_hex64(entry.sid), "the submission id")
        formats.parse_address(entry.publication)
        formats.need(isinstance(entry.server, str) and 0 < len(entry.server) <= _MAX_TEXT,
                     "the server")
        formats.need(entry.role in formats.ROLES, "the role")
        if entry.s_sub is not None:
            formats.need(formats.is_hex64(entry.s_sub), "the submission key")
            secret_from_hex(entry.s_sub)
        formats.need(entry.status_key is None or formats.is_hex64(entry.status_key),
                     "the status key")
        if entry.role == "writer":
            formats.need(entry.s_sub is not None and entry.status_key is not None,
                         "a writer keeps the submission key and the status key")
        formats.need(isinstance(entry.title, str) and len(entry.title) <= formats.MAX_TITLE_CHARS,
                     "the title")
        formats.need(entry.kind in formats.KINDS, "the kind")
        formats.need(isinstance(entry.d, str) and len(entry.d) <= _MAX_TEXT, "the d tag")
        formats.need(formats.is_int(entry.round, low=1), "the round")
        formats.need(formats.is_int(entry.revision, low=1), "the revision")
        formats.need(formats.is_hex64(entry.revision_hash), "the revision hash")
        formats.need(formats.is_int(entry.updated_at), "updated_at")
    except (FormatError, StandupCryptoError) as exc:
        return str(exc)
    return None


def entry_from_json(value: Any) -> Optional[SubmissionEntry]:
    """An entry read from the list, or None when this app cannot read it
    (a broken one, or a newer app's)."""
    if not isinstance(value, dict):
        return None
    entry = SubmissionEntry(**{name: value.get(name) for name in _ENTRY_FIELDS})
    return None if entry_problem(entry) else entry


@dataclass(frozen=True)
class SubmissionItems:
    """What the list holds: the entries this app reads, and the items it
    cannot (``unread``: a broken one, or a newer app's), kept as they
    came and written back unchanged, so this app never deletes what
    another wrote."""

    entries: Tuple[SubmissionEntry, ...] = ()
    unread: Tuple[Mapping[str, Any], ...] = ()


def merge_items(remote: SubmissionItems, local: SubmissionItems) -> SubmissionItems:
    """The entries by :func:`merge_entries`; every unread item of either,
    once."""
    unread: Dict[str, Mapping[str, Any]] = {}
    for item in remote.unread + local.unread:
        unread.setdefault(formats.canonical_json(item), item)
    return SubmissionItems(entries=tuple(merge_entries(remote.entries, local.entries)),
                           unread=tuple(unread.values()))


def merge_entries(remote: Sequence[SubmissionEntry],
                  local: Sequence[SubmissionEntry]) -> List[SubmissionEntry]:
    """By ``sid``, the newest ``updated_at`` wins (13); on a tie, the
    higher revision, then the remote one. The order of ``remote`` is
    kept and new entries go at the end, so recording one rewrites only
    the last page and page 0, not every page: each page written costs a
    signature, through a signer app each one a request."""
    merged: Dict[str, SubmissionEntry] = {}
    for entry in list(remote) + list(local):
        known = merged.get(entry.sid)
        if known is None or (entry.updated_at, entry.revision) > (known.updated_at,
                                                                   known.revision):
            merged[entry.sid] = entry
    return list(merged.values())


def _decode_items(payload: dict) -> SubmissionItems:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("the submissions list has no items")
    entries, unread = [], []
    for item in items:
        entry = entry_from_json(item)
        if entry is not None:
            entries.append(entry)
        elif _canonical(item):
            unread.append(item)
    return merge_items(SubmissionItems(entries=tuple(entries), unread=tuple(unread)),
                       SubmissionItems())


def _canonical(item: Any) -> bool:
    """An item canonical JSON can hold (so it can be written back)."""
    try:
        formats.canonical_json(item)
    except FormatError:
        return False
    return True


def _encode_items(value: SubmissionItems, now: int) -> dict:
    return {"items": [entry.to_json() for entry in value.entries] + list(value.unread)}


def stored(result: PublishResult) -> bool:
    """True when the entry recorded is on the relays: the submission may
    be sent."""
    return result.status in (SENT, UP_TO_DATE)


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
            paging=paging, on_status=self.status.emit, name=d_tag, parent=self)
        self._base: Any = None
        self._base_mark: Optional[Tuple[int, str]] = None
        self._local: Any = empty()
        self._generation = 0
        self.loaded = False
        self.missing: Tuple[int, ...] = ()

    @property
    def account(self) -> str:
        return self._profile.user_pubkey

    def stop(self) -> None:
        """Sign-out or another account: forget everything, answer nothing."""
        self._generation += 1
        self._base = None
        self._base_mark = None
        self._local = self._empty()
        self.loaded = False

    def load(self, on_done: Callable[[Any], None]) -> None:
        """Read the list: ``on_done(value)``, or NO_ANSWER / UNREADABLE
        (the reason already said through :attr:`status`)."""
        generation = self._generation

        def read(result, event) -> None:
            if generation != self._generation:
                return
            if result in (NO_ANSWER, UNREADABLE):
                on_done(result)
                return
            remote = result if result is not None else self._empty()
            self._base = remote
            self._base_mark = (int(event["created_at"]), event["id"]) if event else None
            self._local = self._merge_local(remote)
            self.loaded = True
            on_done(self._local)

        def missing(numbers) -> None:
            self.missing = tuple(numbers)

        self._list.relays(self._profile, lambda relays: self._list.read(
            self._profile, relays, read, on_missing=missing), reading=True)

    def _merge_local(self, remote):
        raise NotImplementedError

    def _publish(self, on_done: Callable[[PublishResult], None]) -> None:
        generation = self._generation

        def merged(value, remote, event) -> None:
            if generation != self._generation:
                return
            self._local = value

        def done(result: PublishResult) -> None:
            if generation != self._generation:
                return
            if result.status == SENT:
                self._base = result.value
                if result.signed:
                    self._base_mark = (int(result.signed["created_at"]), result.signed["id"])
                self.missing = result.missing
            elif result.status == UP_TO_DATE:
                self._base = result.remote
            on_done(result)

        self._list.publish(self._profile,
                           state=lambda: (self._base, self._local, self._base_mark),
                           on_done=done, on_merged=merged)


class SubmissionsList(_AccountList):
    """The writer's submissions (13) for one account."""

    def __init__(self, profile, **boundaries) -> None:
        super().__init__(
            profile, d_tag=formats.SUBMISSIONS_LIST_D, decode=_decode_items,
            encode=_encode_items, merge=lambda base, local, remote: merge_items(
                remote or SubmissionItems(), local),
            words=_submissions_words(), paging=Paging(field="items"), empty=SubmissionItems,
            **boundaries)

    def _merge_local(self, remote: SubmissionItems) -> SubmissionItems:
        return merge_items(remote, self._local)

    def entries(self) -> List[SubmissionEntry]:
        return list(self._local.entries)

    def entry(self, sid: str) -> Optional[SubmissionEntry]:
        return next((e for e in self._local.entries if e.sid == sid), None)

    def record(self, entry: SubmissionEntry, on_done: Callable[[PublishResult], None]) -> None:
        """Add or update ``entry`` and write the list. Send the submission
        only when :func:`stored` says so. ``updated_at`` becomes at least
        now, and later than the entry it replaces, so this edit wins the
        merge (13: the newest ``updated_at`` wins)."""
        self.record_many([entry], on_done)

    def record_many(self, entries: Sequence[SubmissionEntry],
                    on_done: Callable[[PublishResult], None]) -> None:
        """:meth:`record` for several entries, written at once (one
        signature per page changed, not per entry)."""
        now = int(self._clock())
        fresh = []
        for entry in entries:
            problem = entry_problem(entry)
            if problem:
                on_done(PublishResult(FAILED, reason=problem))
                return
            # Later than the entry it replaces, even within one second.
            known = self.entry(entry.sid)
            floor = max(now, known.updated_at + 1 if known else 0)
            fresh.append(replace(entry, updated_at=max(entry.updated_at, floor)))
        self._local = merge_items(self._local, SubmissionItems(entries=tuple(fresh)))
        self._publish(on_done)


class ReadState(_AccountList):
    """What the account has read: the last ``seq`` per submission (13)."""

    def __init__(self, profile, **boundaries) -> None:
        super().__init__(
            profile, d_tag=formats.read_state_d(profile.user_pubkey),
            decode=_decode_seen, encode=lambda value, now: {"seen": dict(value)},
            merge=lambda base, local, remote: _max_merge(remote or {}, local),
            words=_read_state_words(), paging=Paging(field="seen", combine=_higher),
            empty=dict, **boundaries)
        self._dirty = False
        self._last_write: Optional[float] = None

    def _merge_local(self, remote):
        return _max_merge(remote, self._local)

    def seen(self, sid: str) -> int:
        return int(self._local.get(sid, 0))

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
        return self._dirty

    def flush(self, on_done: Optional[Callable[[Optional[PublishResult]], None]] = None, *,
              leaving: bool = False) -> bool:
        """Write what was read, at most every 5 minutes, or now when
        ``leaving``. False (and ``on_done(None)``) when nothing is written
        yet; True when a write started."""
        done = on_done or (lambda _result: None)
        now = self._clock()
        if not self._dirty or (not leaving and self._last_write is not None
                               and now - self._last_write < READ_STATE_INTERVAL_SECONDS):
            done(None)
            return False
        self._last_write = now
        self._dirty = False

        def written(result: PublishResult) -> None:
            if result.status not in (SENT, UP_TO_DATE):
                self._dirty = True
            done(result)

        self._publish(written)
        return True

    def stop(self) -> None:
        super().stop()
        self._dirty = False
        self._last_write = None


def _higher(earlier: Any, later: Any) -> Any:
    if formats.is_int(earlier) and formats.is_int(later):
        return max(earlier, later)
    return later if formats.is_int(later) else earlier


def _decode_seen(payload: dict) -> Dict[str, int]:
    seen = payload.get("seen")
    if not isinstance(seen, dict):
        raise ValueError("the read state has no map")
    return {sid: seq for sid, seq in seen.items()
            if formats.is_hex64(sid) and formats.is_int(seq)}


def _max_merge(remote: Dict[str, int], local: Dict[str, int]) -> Dict[str, int]:
    merged = dict(remote)
    for sid, seq in local.items():
        merged[sid] = max(seq, merged.get(sid, seq))
    return merged


__all__ = [
    "READ_STATE_INTERVAL_SECONDS", "ReadState", "SubmissionEntry", "SubmissionItems",
    "SubmissionsList", "entry_from_json", "entry_problem", "merge_entries", "merge_items",
    "stored",
]
