# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Submitting an article to a publication, in the protocol's order
(editorial contract 8.1, 12.4, 13; plan 10.3). Pure: every request goes
through :class:`Ports`, so the same table runs against the real client
and against the fakes of the tests.

The order is fixed:

1. the Redaktion's key, checked right before, and only for the server
   that will receive the submission (8.1);
2. each local picture encrypted (SMF1) and uploaded to the writer's
   own media servers;
3. the revision built, encrypted and wrapped (``drafting.seal``);
4. the entry recorded on the writer's list, and the list holding
   exactly it (13; 12.4 "No answer after sending");
5. the request signed (NIP-98, last: it covers the body's hash and holds
   for 60 seconds) and sent;
6. the receipt compared with what was sent: only a matching one, every
   file found, is "Eingereicht". No answer, or a receipt that does not
   match, is checked by the status key (no signer prompt) until the
   server says what it has. Files it misses are uploaded again and the
   same body is sent again.

A refusal for the round (409 ``conflict`` naming the server's round, or
``stale_revision`` for this revision 1: the server stores its sid
already) builds the revision once more, for the round the server names
or, after ``stale_revision``, the one chosen again from step 1 of
contract 2 (:mod:`.rounds`); never twice, and the refused body never
goes out again.

:class:`Submitting` is that order as a table (``_TABLE``) of what may
happen in each :class:`Step` and what follows. Anything else (an answer
for a call it no longer waits for, a cancelled one, a second answer) is
ignored, so a step never moves twice. The person can cancel until the
request is on its way (:attr:`Progress.cancellable`); after a failure
they can try again once, from where it failed (:attr:`Failure.retry`).
The screen only shows :class:`Progress` (:mod:`.progress`, the values it
reads; the requests, :class:`Ports`, are :mod:`.ports`'s; both offered
here too) and forwards :meth:`cancel`, :meth:`retry` and
:meth:`retry_message`; it never composes a step.

The module is one state machine: its table, the events the table names
and their handlers stay together (lesson 10), so it is longer than most.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import drafting, formats, views, words
from .drafting import ArticleDraft, Choice, EncryptedPicture, Sealed
from .errors import Code, StandupError
from .formats import FormatError, SecretKeyFound
from ..list_results import PublishStatus
from .entries import RecordResult, RecordStatus
from .ports import Ports
from .progress import (
    HELD_UNKNOWN_FIRST, HELD_WORDS, PHASE_OF, Failure, Held, HeldWords, MessageStatus, Phase,
    Progress, Step,
)
from .rounds import RoundAnswer
from .trust import KeyNotConfirmed, RedaktionKey
from .watcher import Different, NotStored, StoredOther, Superseded

# How long to wait before asking again by the status key, in turn; the
# last one repeats until the server answers.
LOOKUP_DELAYS_MS = (0, 2_000, 10_000, 30_000, 60_000, 300_000)
# Files a receipt misses are uploaded again this often before it fails.
MAX_REUPLOADS = 2


# The events that are not the domain's own values.
@dataclass(frozen=True)
class _Go:
    pass


@dataclass(frozen=True)
class _Uploaded:
    servers: Mapping[str, Tuple[str, ...]]


@dataclass(frozen=True)
class _UploadFailed:
    said: str


@dataclass(frozen=True)
class _Sending:
    pass


@dataclass(frozen=True)
class _MessageDone:
    sent: bool
    retry_after: Optional[int] = None       # a limit's wait, in seconds


@dataclass(frozen=True)
class _MessageSigned:
    """The signer sealed the first message; it goes to the server now."""


@dataclass(frozen=True)
class _MaybeStored:
    """A request that went out got no answer it could be judged by: the
    server may hold the body, so the lookup says."""

    error: StandupError


@dataclass(frozen=True)
class _OtherRound:
    """A 409 ``conflict`` naming the round the server stores for this work
    (contract 2), closed: nothing of this body was stored."""

    latest: int


@dataclass(frozen=True)
class _RoundOpen:
    """As :class:`_OtherRound`, the round still open: the work is handed
    in there already."""

    latest: int


@dataclass(frozen=True)
class _StaleRound:
    """A 409 ``stale_revision``: the server holds another revision at
    this sid (``detail.revision``, contract 2, bde1274)."""

    error: StandupError


# Refusals that trying again could get past, and from where.
_RETRY_AT = {
    Code.OFFLINE: Step.SIGNING, Code.TIMEOUT: Step.SIGNING, Code.SERVER: Step.SIGNING,
    Code.SIGNER_DECLINED: Step.SIGNING, Code.SIGNER_UNREACHABLE: Step.SIGNING,
    Code.RATE_LIMITED: Step.SIGNING, Code.UNAVAILABLE: Step.SIGNING,
    Code.EPOCH: Step.KEY, Code.KEY_EVENT_INVALID: Step.KEY,
}


def _refusal(error: StandupError) -> Any:
    """What a failed submit is for the table (lesson 10): one that may be
    stored (:class:`_MaybeStored`), a conflict naming the round the server
    stores for this work, closed or open (one without both is not taken
    as one), or the refusal itself."""
    if error.maybe_stored:
        return _MaybeStored(error)
    if error.code == Code.STALE_REVISION:
        return _StaleRound(error)
    latest, still_open = error.detail.get("round"), error.detail.get("open")
    if error.code != Code.CONFLICT or not formats.is_int(latest) \
            or not isinstance(still_open, bool):
        return error
    return _RoundOpen(latest) if still_open else _OtherRound(latest)


class Submitting:
    """One article on its way to one publication (see the module
    docstring). ``on_change(progress)`` after every step; the first
    comes from :meth:`start`."""

    def __init__(self, draft: ArticleDraft, choice: Choice, *, writer: str, round_: int,
                 ports: Ports, on_change: Callable[[Progress], None]) -> None:
        self._draft = draft
        self._choice = choice
        self._writer = writer
        self._round = round_
        self._ports = ports
        self._on_change = on_change
        self._progress = Progress(Step.KEY, pictures=len(draft.pictures))
        self._key: Optional[RedaktionKey] = None
        self._pictures: Tuple[EncryptedPicture, ...] = ()
        self._servers: Dict[str, Tuple[str, ...]] = {}
        self._sealed: Optional[Sealed] = None
        self._missing: Tuple[str, ...] = ()
        self._reuploads = 0
        self._lookups = 0
        # Only the answers of the newest request count (``_token``); one
        # event is handled at a time, the rest wait (``_queue``).
        self._token = 0
        self._queue: List[Tuple[int, Any]] = []
        self._busy = False
        self._stop: Optional[Callable[[], None]] = None
        self._stopped = False
        # Built again once for the round the server expects (409
        # ``conflict``, contract 2) or for the round chosen again after
        # ``stale_revision``: never twice, never the same body.
        self._rebuilt = False
        # The round a revision 1 was refused in as stale_revision.
        self._stale_round = 0

    # -- what the screen asks ------------------------------------------------

    @property
    def progress(self) -> Progress:
        return self._progress

    @property
    def sealed(self) -> Optional[Sealed]:
        return self._sealed

    @property
    def choice(self) -> Choice:
        return self._choice

    def start(self) -> None:
        self._show(Step.KEY)
        self._ask_key()

    def cancel(self) -> None:
        """Until the request is on its way; then nothing changes. An
        entry recorded (or being recorded) for it is taken back from the
        list: no device keeps the keys of a submission never sent."""
        if not self._progress.cancellable:
            return
        recorded = self._progress.step in (Step.RECORDING, Step.SIGNING)
        self._halt()
        if recorded and self._sealed is not None:
            self._ports.forget(self._sealed.entry)
        self._show(Step.CANCELLED)

    def retry(self) -> None:
        failure = self._progress.failure
        if self._progress.step is not Step.FAILED or failure is None or failure.retry is None \
                or self._waits(failure.retry_at):
            return
        self._reuploads = 0
        {Step.KEY: self._ask_key, Step.UPLOADING: self._upload, Step.REUPLOADING: self._reupload,
         Step.RECORDING: self._record, Step.SIGNING: self._send,
         Step.ROUND: self._choose_round}[failure.retry]()

    def retry_message(self) -> None:
        if self._progress.step is Step.SUBMITTED and \
                self._progress.message is MessageStatus.FAILED and \
                not self._waits(self._progress.message_retry_at):
            self._send_message()

    def _waits(self, until: Optional[float]) -> bool:
        """Whether a limit's wait is not over: another request now would
        be another signer prompt meeting the same limit (RG-M2r 7)."""
        return until is not None and self._ports.clock() < until

    def _after(self, seconds: Optional[int]) -> Optional[float]:
        return None if not seconds else self._ports.clock() + seconds

    def stop(self) -> None:
        """Sign-out: nothing more is asked, nothing more is said."""
        self._halt()
        self._stopped = True

    # -- the table -----------------------------------------------------------

    _TABLE = {
        (Step.KEY, RedaktionKey): "_with_key",
        (Step.KEY, KeyNotConfirmed): "_key_refused",
        (Step.ENCRYPTING, _Go): "_encrypt",
        (Step.UPLOADING, _Uploaded): "_uploaded",
        (Step.UPLOADING, _UploadFailed): "_upload_failed",
        (Step.RECORDING, RecordResult): "_recorded",
        (Step.SIGNING, _Sending): "_sending",
        (Step.SIGNING, StandupError): "_refused",
        (Step.SIGNING, _MaybeStored): "_check",
        (Step.SIGNING, _OtherRound): "_other_round",
        (Step.SIGNING, _RoundOpen): "_round_open",
        (Step.SIGNING, _StaleRound): "_stale",
        (Step.SENDING, views.Confirmed): "_confirmed",
        (Step.SENDING, views.BlobsMissing): "_blobs_missing",
        (Step.SENDING, views.Mismatch): "_check",
        (Step.SENDING, StandupError): "_refused",
        (Step.SENDING, _MaybeStored): "_check",
        (Step.SENDING, _OtherRound): "_other_round",
        (Step.SENDING, _RoundOpen): "_round_open",
        (Step.SENDING, _StaleRound): "_stale",
        (Step.ROUND, RoundAnswer): "_round_chosen",
        (Step.CHECKING, views.Confirmed): "_confirmed",
        (Step.CHECKING, views.BlobsMissing): "_blobs_missing",
        (Step.CHECKING, NotStored): "_not_stored",
        (Step.CHECKING, Superseded): "_elsewhere",
        (Step.CHECKING, Different): "_elsewhere",
        (Step.CHECKING, StoredOther): "_stored_other",
        (Step.CHECKING, StandupError): "_lookup_failed",
        (Step.REUPLOADING, _Uploaded): "_reuploaded",
        (Step.REUPLOADING, _UploadFailed): "_reupload_failed",
        (Step.SUBMITTED, _MessageSigned): "_message_signed",
        (Step.SUBMITTED, _MessageDone): "_message_done",
    }

    def _answer(self) -> Callable[[Any], None]:
        """A callback for the request about to be made; the answers of any
        earlier one are ignored from now on."""
        self._token += 1
        token = self._token
        return lambda event: self._post(token, event)

    def _post(self, token: int, event: Any) -> None:
        self._queue.append((token, event))
        if self._busy:
            return
        self._busy = True
        try:
            while self._queue:
                token, event = self._queue.pop(0)
                if token != self._token or self._stopped:
                    continue
                name = self._TABLE.get((self._progress.step, type(event)))
                if name is not None:
                    getattr(self, name)(event)
        finally:
            self._busy = False

    def _show(self, step: Step, **changes) -> None:
        self._progress = replace(self._progress, step=step, **changes)
        if not self._stopped:
            self._on_change(self._progress)

    def _halt(self) -> None:
        self._token += 1
        stop, self._stop = self._stop, None
        if stop is not None:
            stop()

    def _fail(self, headline: str, retry: Optional[Step], *, said: str = "",
              held: Optional[Held] = None, retry_at: Optional[float] = None) -> None:
        """Failed, with what the server holds now (``held``, when this
        answer told; else as it was): the second line comes from it."""
        self._stop = None
        held = held or self._progress.held
        self._show(Step.FAILED, held=held, failure=Failure(
            headline, retry, detail=HELD_WORDS[held].failed, said=said, retry_at=retry_at))

    def _refused_held(self, error: Optional[StandupError] = None) -> Held:
        """What the server holds after a refusal of the request just made:
        nothing of it (as before it, when a receipt already said the
        revision is stored), or another version (``stale_revision``)."""
        if error is not None and error.code == Code.STALE_REVISION:
            return Held.OTHER
        return Held.STORED if self._progress.held is Held.STORED else Held.NOTHING

    # -- 1. the key ----------------------------------------------------------

    def _ask_key(self) -> None:
        self._show(Step.KEY, failure=None)
        answer = self._answer()
        self._ports.confirm_key(answer, answer)

    def _with_key(self, key: RedaktionKey) -> None:
        self._key = key
        self._show(Step.ENCRYPTING)
        # On a later turn, so "Wird verschlüsselt …" is on screen while
        # the pictures are encrypted.
        answer = self._answer()
        self._ports.later(0, lambda: answer(_Go()))

    def _key_refused(self, _refusal: KeyNotConfirmed) -> None:
        self._fail(words.refusal(Code.KEY_EVENT_INVALID), Step.KEY)

    # -- 2. the pictures -----------------------------------------------------

    def _encrypt(self, _go: _Go) -> None:
        self._pictures = drafting.encrypt_pictures(self._draft.pictures)
        if self._pictures:
            self._upload()
        else:
            self._seal_and_record()

    def _upload(self) -> None:
        self._show(Step.UPLOADING, failure=None, uploaded=0.0)
        self._start_upload(self._pictures)

    def _start_upload(self, pictures: Sequence[EncryptedPicture]) -> None:
        answer = self._answer()
        token = self._token
        self._stop = self._ports.upload(
            pictures, lambda fraction: self._uploading(token, fraction),
            lambda servers: answer(_Uploaded(dict(servers))),
            lambda said: answer(_UploadFailed(said)))

    def _uploading(self, token: int, fraction: float) -> None:
        if token == self._token and self._progress.step in (Step.UPLOADING, Step.REUPLOADING):
            self._show(self._progress.step, uploaded=max(0.0, min(1.0, fraction)))

    def _uploaded(self, event: _Uploaded) -> None:
        self._stop = None
        self._servers.update(event.servers)
        if any(not self._servers.get(p.sha256) for p in self._pictures):
            self._fail(words.UPLOAD_FAILED, Step.UPLOADING)
            return
        self._seal_and_record()

    def _upload_failed(self, event: _UploadFailed) -> None:
        self._fail(words.UPLOAD_FAILED, Step.UPLOADING, said=event.said)

    def _reupload_failed(self, event: _UploadFailed) -> None:
        self._fail(words.UPLOAD_FAILED, Step.REUPLOADING, said=event.said)

    # -- 3. and 4. the revision, and the entry on the list -------------------

    def _seal_and_record(self) -> None:
        sid = drafting.submission_id_of(self._draft, self._choice, self._writer, self._round)
        try:
            self._sealed = drafting.seal(self._draft, self._choice, self._key, self._pictures,
                                         self._servers, writer=self._writer,
                                         round_=self._round, now=int(self._ports.clock()),
                                         held=self._ports.held(sid))
        except SecretKeyFound as refusal:
            self._fail(str(refusal), None)
            return
        except drafting.TooLarge:
            self._fail(words.TOO_LARGE, None)
            return
        except FormatError:
            # Built here and refused here: nothing went out, so it is
            # never the server's refusal (RG-M2b 11).
            self._fail(words.NOT_BUILT, None)
            return
        except drafting.HeldNotUsable:
            self._fail(words.LIST_ENTRY_UNUSABLE, None)
            return
        self._record()

    def _record(self) -> None:
        self._show(Step.RECORDING, failure=None)
        self._ports.record(self._sealed.entry, self._answer())

    def _recorded(self, result: RecordResult) -> None:
        if result.status is RecordStatus.STORED:
            self._sealed = replace(self._sealed, entry=result.entry)
            self._send()
        elif result.status is RecordStatus.SUPERSEDED:
            self._fail(words.LIST_CHANGED_ELSEWHERE, None)
        else:
            # A list that could not be read now says why in its own words
            # (BLOCKED as UNREADABLE), with one retry.
            said = result.reason if result.publish.status in (
                PublishStatus.BLOCKED, PublishStatus.UNREADABLE) else ""
            self._fail(words.LIST_NOT_SAVED, Step.RECORDING, said=said)

    # -- 5. the request ------------------------------------------------------

    def _send(self) -> None:
        self._show(Step.SIGNING, failure=None)
        answer = self._answer()
        self._stop = self._ports.submit(self._sealed.body, lambda: answer(_Sending()),
                                        answer, lambda error: answer(_refusal(error)))

    def _sending(self, _event: _Sending) -> None:
        held = Held.STORED if self._progress.held is Held.STORED else Held.MAYBE
        self._show(Step.SENDING, sent=True, held=held)

    def _refused(self, error: StandupError) -> None:
        self._stop = None
        if error.code == Code.RATE_LIMITED:
            # The limit's wait says which one it is and when trying again helps.
            self._fail(words.rate_limited(error.retry_after), _RETRY_AT[error.code],
                       held=self._refused_held(error), retry_at=self._after(error.retry_after))
            return
        self._fail(words.refusal(error.code), _RETRY_AT.get(error.code),
                   held=self._refused_held(error))

    def _round_open(self, _conflict: _RoundOpen) -> None:
        """The work is handed in in the server's latest round, still open
        (contract 2): nothing of this body was stored, so its entry goes;
        the server holds the work in that round."""
        self._stop = None
        self._ports.forget(self._sealed.entry)
        self._fail(drafting.ALREADY_SUBMITTED, None, held=Held.OTHER)

    def _other_round(self, conflict: _OtherRound) -> None:
        """The server expects another round (contract 2): its latest for
        this work is closed. Nothing of this body was stored, so its entry
        goes; the run builds round ``latest + 1`` once (a server that names
        the same round again must not hold it in a loop)."""
        self._stop = None
        self._ports.forget(self._sealed.entry)
        if self._rebuilt or conflict.latest + 1 == self._round:
            self._fail(words.NOT_TAKEN, None, held=self._refused_held())
            return
        self._rebuilt = True
        self._round = conflict.latest + 1
        self._progress = replace(self._progress, held=self._refused_held())
        self._seal_and_record()

    def _stale(self, stale: _StaleRound) -> None:
        """409 ``stale_revision``. For this revision 1, never stored (no
        receipt said so), the server stores its sid already under secrets
        the list did not hold for it: another device handed the work in
        meanwhile, or the list lost the entry. The round is chosen again
        from step 1 of contract 2, once (bde1274); the entry stays, as it
        may hold the secrets the server has. Anything else is a refusal."""
        self._stop = None
        if self._rebuilt or self._sealed.body.get("revision") != 1 \
                or self._progress.held is Held.STORED:
            self._refused(stale.error)
            return
        self._rebuilt = True
        self._stale_round = self._round
        self._choose_round()

    def _choose_round(self) -> None:
        self._show(Step.ROUND, failure=None, held=Held.OTHER)
        self._ports.choose_round(self._sealed.entry, self._answer())

    def _round_chosen(self, answer: RoundAnswer) -> None:
        """What step 1 of contract 2, asked again, says: a later round,
        built once; the work still open there; or, when the round cannot
        be told, why (Try Again asks again unless a bound was met). A
        round not after the refused one would be the refused sid again."""
        self._stop = None
        if answer.words:
            self._fail(words.ANOTHER_VERSION, None if answer.final else Step.ROUND,
                       said=answer.words, held=Held.OTHER)
        elif answer.round is None:
            self._fail(drafting.ALREADY_SUBMITTED, None, held=Held.OTHER)
        elif answer.round <= self._stale_round:
            self._fail(words.ANOTHER_VERSION, None, held=Held.OTHER)
        else:
            self._round = answer.round
            self._progress = replace(self._progress, held=Held.NOTHING)
            self._seal_and_record()

    # -- 6. the receipt, or the lookup ---------------------------------------

    def _confirmed(self, confirmed: views.Confirmed) -> None:
        """The one way to "Eingereicht": a matching receipt (the server's
        answer, or the lookup by the status key), every file found."""
        self._show(Step.SUBMITTED, receipt=confirmed.receipt, failure=None, held=Held.STORED)
        if self._choice.message.strip():
            self._send_message()

    def _blobs_missing(self, missing: views.BlobsMissing) -> None:
        # A receipt: the server stores this revision, files missing.
        self._progress = replace(self._progress, held=Held.STORED)
        if self._reuploads >= MAX_REUPLOADS:
            self._fail(words.PICTURE_MISSING, Step.REUPLOADING)
            return
        self._missing = tuple(missing.missing)
        self._reupload()

    def _reupload(self) -> None:
        self._reuploads += 1
        missing = set(self._missing)
        self._show(Step.REUPLOADING, failure=None, uploaded=0.0)
        self._start_upload([p for p in self._pictures if p.sha256 in missing]
                           or list(self._pictures))

    def _reuploaded(self, event: _Uploaded) -> None:
        self._stop = None
        self._send()                # the same body, a new signature (12.4)

    def _check(self, _why: Any) -> None:
        self._stop = None
        self._lookups = 0
        self._show(Step.CHECKING)
        self._look_up()

    def _look_up(self, *, wait_s: Optional[int] = None) -> None:
        delay = max(LOOKUP_DELAYS_MS[min(self._lookups, len(LOOKUP_DELAYS_MS) - 1)],
                    (wait_s or 0) * 1000)
        self._lookups += 1
        answer = self._answer()
        token = self._token

        def ask() -> None:
            if token == self._token and not self._stopped:
                self._ports.lookup(self._sealed, answer, answer)

        self._ports.later(delay, ask)

    def _lookup_failed(self, error: StandupError) -> None:
        # The entry stays unconfirmed: ask again later, never before a
        # limit's wait is over (contract 15: 30 lookups a minute).
        self._look_up(wait_s=error.retry_after if error.code == Code.RATE_LIMITED else None)

    def _not_stored(self, _found: NotStored) -> None:
        # The server answered the lookup: there was a connection, it has
        # nothing of this submission (RG-M2b 11).
        self._fail(words.NOT_RECEIVED, Step.SIGNING, held=Held.NOTHING)

    def _elsewhere(self, _found: Any) -> None:
        self._fail(words.ANOTHER_VERSION, None, held=Held.OTHER)

    def _stored_other(self, _found: StoredOther) -> None:
        # This revision is stored, but not as it was sent (12.4).
        self._fail(words.STORED_OTHER, None, held=Held.OTHER)

    # -- the first message ---------------------------------------------------

    def _send_message(self) -> None:
        # The signer app is asked first: the words say so while it waits
        # (RG-M2b 11), as they do for the submission itself.
        self._show(Step.SUBMITTED, message=MessageStatus.SIGNING)
        answer = self._answer()
        self._ports.send_message(self._sealed, self._choice.message.strip(),
                                 lambda: answer(_MessageSigned()),
                                 lambda: answer(_MessageDone(True)),
                                 lambda wait=None: answer(_MessageDone(False, wait)))

    def _message_signed(self, _event: _MessageSigned) -> None:
        self._show(Step.SUBMITTED, message=MessageStatus.SENDING)

    def _message_done(self, event: _MessageDone) -> None:
        self._show(Step.SUBMITTED,
                   message=MessageStatus.SENT if event.sent else MessageStatus.FAILED,
                   message_retry_at=None if event.sent else self._after(event.retry_after))


__all__ = [
    "HELD_UNKNOWN_FIRST", "HELD_WORDS", "Held", "HeldWords", "LOOKUP_DELAYS_MS", "MAX_REUPLOADS",
    "Failure", "MessageStatus", "PHASE_OF", "Phase", "Ports", "Progress", "Step", "Submitting",
]
