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

:class:`Submitting` is that order as a table (``_TABLE``) of what may
happen in each :class:`Step` and what follows. Anything else (an answer
for a call it no longer waits for, a cancelled one, a second answer) is
ignored, so a step never moves twice. The person can cancel until the
request is on its way (:attr:`Progress.cancellable`); after a failure
they can try again once, from where it failed (:attr:`Failure.retry`).
The screen only shows :class:`Progress` and forwards :meth:`cancel`,
:meth:`retry` and :meth:`retry_message`; it never composes a step.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import drafting, views, words
from .drafting import ArticleDraft, Choice, EncryptedPicture, Sealed
from .errors import Code, StandupError
from .formats import FormatError, SecretKeyFound
from ..list_results import PublishStatus
from .lists import RecordResult, RecordStatus
from .trust import KeyNotConfirmed, RedaktionKey
from .watcher import Different, NotStored, Superseded

# How long to wait before asking again by the status key, in turn; the
# last one repeats until the server answers.
LOOKUP_DELAYS_MS = (0, 2_000, 10_000, 30_000, 60_000, 300_000)
# Files a receipt misses are uploaded again this often before it fails.
MAX_REUPLOADS = 2


class Step(Enum):
    KEY = "key"                     # the Redaktion's key (8.1)
    ENCRYPTING = "encrypting"       # SMF1, each picture
    UPLOADING = "uploading"         # to the writer's media servers
    RECORDING = "recording"         # the entry on the list (13)
    SIGNING = "signing"             # the NIP-98 request
    SENDING = "sending"             # on its way: no cancelling now
    CHECKING = "checking"           # no answer: asked by the status key
    REUPLOADING = "reuploading"     # files the receipt missed
    SUBMITTED = "submitted"         # a matching receipt, every file found
    FAILED = "failed"
    CANCELLED = "cancelled"


class Phase(Enum):
    """What the handoff shows (plan 10.3)."""

    ENCRYPT = "encrypt"
    UPLOAD = "upload"
    SIGNER = "signer"
    SEND = "send"
    UNCONFIRMED = "unconfirmed"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


PHASE_OF = {
    Step.KEY: Phase.ENCRYPT, Step.ENCRYPTING: Phase.ENCRYPT, Step.UPLOADING: Phase.UPLOAD,
    Step.REUPLOADING: Phase.UPLOAD, Step.RECORDING: Phase.SIGNER, Step.SIGNING: Phase.SIGNER,
    Step.SENDING: Phase.SEND, Step.CHECKING: Phase.UNCONFIRMED, Step.SUBMITTED: Phase.DONE,
    Step.FAILED: Phase.FAILED, Step.CANCELLED: Phase.CANCELLED,
}
_CANCELLABLE = frozenset({Step.KEY, Step.ENCRYPTING, Step.UPLOADING, Step.RECORDING,
                          Step.SIGNING})


class MessageStatus(Enum):
    NONE = "none"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True)
class Failure:
    """What did not happen: ``headline`` (untranslated words, with
    ``{publication}`` filled in where they name it), ``detail`` (words,
    or the list's own, already translated, in ``said``), and where
    trying again goes on (``retry``; None when it would not help).
    The second line shown is ``said`` when there is one, else ``detail``."""

    headline: str
    retry: Optional[Step]
    detail: str = words.NOTHING_SUBMITTED
    said: str = ""


@dataclass(frozen=True)
class Progress:
    step: Step
    pictures: int = 0
    uploaded: float = 0.0           # 0 to 1 while uploading
    failure: Optional[Failure] = None
    receipt: Optional[views.Receipt] = field(default=None, repr=False)
    message: MessageStatus = MessageStatus.NONE

    @property
    def phase(self) -> Phase:
        return PHASE_OF[self.step]

    @property
    def cancellable(self) -> bool:
        return self.step in _CANCELLABLE


UploadDone = Callable[[Mapping[str, Tuple[str, ...]]], None]


@dataclass(frozen=True)
class Ports:
    """Every request, as the window wires it (tests: fakes).

    - ``confirm_key(on_key, on_refused)``: trust.confirm_key for the
      receiving server.
    - ``upload(pictures, on_progress(fraction), on_done({sha256: servers}),
      on_failure(words))`` returns a callable that stops it.
    - ``record(entry, on_done(RecordResult))``: SubmissionsList.record.
    - ``forget(entry)``: SubmissionsList.forget, for an entry recorded for
      a request that was cancelled before it was sent.
    - ``submit(body, on_sending(), on_success(receipt check),
      on_failure(StandupError))`` returns a callable that drops it.
    - ``lookup(entry, on_done(status), on_failure(StandupError))``:
      watcher.resolve_unconfirmed.
    - ``send_message(sealed, text, on_sent(), on_failure())``.
    - ``later(ms, callback)``; ``clock()``.
    """

    confirm_key: Callable[[Callable[[RedaktionKey], None],
                           Callable[[KeyNotConfirmed], None]], None]
    upload: Callable[[Sequence[EncryptedPicture], Callable[[float], None], UploadDone,
                      Callable[[str], None]], Callable[[], None]]
    record: Callable[[Any, Callable[[RecordResult], None]], None]
    forget: Callable[[Any], None]
    submit: Callable[[Mapping[str, Any], Callable[[], None], Callable[[Any], None],
                      Callable[[StandupError], None]], Callable[[], None]]
    lookup: Callable[[Any, Callable[[Any], None], Callable[[StandupError], None]], None]
    send_message: Callable[[Sealed, str, Callable[[], None], Callable[[], None]], None]
    later: Callable[[int, Callable[[], None]], None]
    clock: Callable[[], float]


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


# Refusals that trying again could get past, and from where.
_RETRY_AT = {
    Code.OFFLINE: Step.SIGNING, Code.TIMEOUT: Step.SIGNING, Code.SERVER: Step.SIGNING,
    Code.SIGNER_DECLINED: Step.SIGNING, Code.SIGNER_UNREACHABLE: Step.SIGNING,
    Code.RATE_LIMITED: Step.SIGNING, Code.UNAVAILABLE: Step.SIGNING,
    Code.EPOCH: Step.KEY, Code.KEY_EVENT_INVALID: Step.KEY,
}


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
        if self._progress.step is not Step.FAILED or failure is None or failure.retry is None:
            return
        self._reuploads = 0
        {Step.KEY: self._ask_key, Step.UPLOADING: self._upload, Step.REUPLOADING: self._reupload,
         Step.RECORDING: self._record, Step.SIGNING: self._send}[failure.retry]()

    def retry_message(self) -> None:
        if self._progress.step is Step.SUBMITTED and \
                self._progress.message is MessageStatus.FAILED:
            self._send_message()

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
        (Step.SENDING, views.Confirmed): "_confirmed",
        (Step.SENDING, views.BlobsMissing): "_blobs_missing",
        (Step.SENDING, views.Mismatch): "_check",
        (Step.SENDING, StandupError): "_refused",
        (Step.CHECKING, views.Confirmed): "_confirmed",
        (Step.CHECKING, views.BlobsMissing): "_blobs_missing",
        (Step.CHECKING, NotStored): "_not_stored",
        (Step.CHECKING, Superseded): "_elsewhere",
        (Step.CHECKING, Different): "_elsewhere",
        (Step.CHECKING, StandupError): "_lookup_failed",
        (Step.REUPLOADING, _Uploaded): "_reuploaded",
        (Step.REUPLOADING, _UploadFailed): "_upload_failed",
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
              detail: str = words.NOTHING_SUBMITTED) -> None:
        self._stop = None
        self._show(Step.FAILED, failure=Failure(headline, retry, detail=detail, said=said))

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
        retry = Step.REUPLOADING if self._progress.step is Step.REUPLOADING else Step.UPLOADING
        self._fail(words.UPLOAD_FAILED, retry, said=event.said)

    # -- 3. and 4. the revision, and the entry on the list -------------------

    def _seal_and_record(self) -> None:
        try:
            self._sealed = drafting.seal(self._draft, self._choice, self._key, self._pictures,
                                         self._servers, writer=self._writer,
                                         round_=self._round, now=int(self._ports.clock()))
        except SecretKeyFound as refusal:
            self._fail(str(refusal), None)
            return
        except drafting.TooLarge:
            self._fail(words.TOO_LARGE, None)
            return
        except FormatError:
            self._fail(words.NOT_TAKEN, None)
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
                                        answer, answer)

    def _sending(self, _event: _Sending) -> None:
        self._show(Step.SENDING)

    def _refused(self, error: StandupError) -> None:
        self._stop = None
        if error.maybe_stored:
            self._check(error)
            return
        self._fail(words.refusal(error.code), _RETRY_AT.get(error.code))

    # -- 6. the receipt, or the lookup ---------------------------------------

    def _confirmed(self, confirmed: views.Confirmed) -> None:
        """The one way to "Eingereicht": a matching receipt (the server's
        answer, or the lookup by the status key), every file found."""
        self._show(Step.SUBMITTED, receipt=confirmed.receipt, failure=None)
        if self._choice.message.strip():
            self._send_message()

    def _blobs_missing(self, missing: views.BlobsMissing) -> None:
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
        self._lookups = 0
        self._show(Step.CHECKING)
        self._look_up()

    def _look_up(self) -> None:
        delay = LOOKUP_DELAYS_MS[min(self._lookups, len(LOOKUP_DELAYS_MS) - 1)]
        self._lookups += 1
        answer = self._answer()
        token = self._token

        def ask() -> None:
            if token == self._token and not self._stopped:
                self._ports.lookup(self._sealed.entry, answer, answer)

        self._ports.later(delay, ask)

    def _lookup_failed(self, _error: StandupError) -> None:
        self._look_up()             # the entry stays unconfirmed: ask again later

    def _not_stored(self, _found: NotStored) -> None:
        self._fail(words.NOT_SENT, Step.SIGNING)

    def _elsewhere(self, _found: Any) -> None:
        self._fail(words.ANOTHER_VERSION, None)

    # -- the first message ---------------------------------------------------

    def _send_message(self) -> None:
        self._show(Step.SUBMITTED, message=MessageStatus.SENDING)
        answer = self._answer()
        self._ports.send_message(self._sealed, self._choice.message.strip(),
                                 lambda: answer(_MessageDone(True)),
                                 lambda: answer(_MessageDone(False)))

    def _message_done(self, event: _MessageDone) -> None:
        self._show(Step.SUBMITTED,
                   message=MessageStatus.SENT if event.sent else MessageStatus.FAILED)


__all__ = [
    "LOOKUP_DELAYS_MS", "MAX_REUPLOADS", "Failure", "MessageStatus", "PHASE_OF", "Phase",
    "Ports", "Progress", "Step", "Submitting",
]
