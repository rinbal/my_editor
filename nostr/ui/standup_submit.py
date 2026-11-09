# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The window's side of submitting to a STANDUP publication (plan 10.2,
10.3): the real requests behind the ports of
:mod:`nostr.standup.submitting`, and what belongs to one account.

:class:`StandupWriter` keeps, per account, the client for the server
(``MYEDITOR_STANDUP_SERVER`` until the association names its address;
without one nothing is sent and the sheet says "kommt vom Verein"), the
writer's submissions list and read state, the media keys, and the
submissions on their way. A submission whose sheet was closed goes on
in the background, and :attr:`StandupWriter.status` says how it ends
("Wird eingereicht …", then "Eingereicht. Im Eingang von …" only after
the receipt). :meth:`StandupWriter.stop` is the sign-out (13): the
submissions on their way stop answering, the read state is written,
then the lists stop, and the media keys are forgotten.

:class:`BlossomUploads` sends the encrypted pictures to the writer's own
media servers through the Media Library's store (the first server, then
the mirrors), and :func:`thumbnail` makes the revision's small cover
(about 128 px, JPEG, at most 24 KB).
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter

from i18n import _, ngettext

from ..einundzwanzig_api.client import session_signer
from ..list_relays import ListQuery, publish_once
from ..later import answer_later
from ..standup import conversation, formats, trust, words
from ..standup.client import StandupApi
from ..standup import drafting
from ..standup.drafting import ArticleDraft, Choice, EncryptedPicture, Sealed, choose_round
from ..standup.entries import SubmissionEntry
from ..standup.lists import (
    SUBMISSIONS_MISSING_PAGE, SUBMISSIONS_UNREADABLE, ReadState, SubmissionsList,
)
from ..standup.opening import MediaKeys
from ..standup.servers import configured_server
from ..standup.submitting import MessageStatus, Phase, Ports, Progress, Submitting
from ..standup.trust import Destination
from ..standup.watcher import look_up_all, resolve_unconfirmed

THUMB_SIDE = 128


def thumbnail(data: bytes) -> Optional[dict]:
    """The revision's ``thumb`` (5) of a picture: at most 128 px, JPEG on
    white, at most 24 KB; None when it cannot be read or made that small."""
    image = QImage.fromData(QByteArray(data))
    if image.isNull():
        return None
    image = image.scaled(min(THUMB_SIDE, image.width()), min(THUMB_SIDE, image.height()),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(QColor("white"))
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    for quality in (82, 68, 52, 36):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        flat.save(buffer, "JPEG", quality)
        raw = bytes(buffer.data())
        if len(raw) <= formats.MAX_THUMB_BYTES:
            return {"mime": "image/jpeg", "data": formats.b64encode(raw)}
    return None


@dataclass
class _Upload:
    on_progress: Callable[[float], None]
    on_done: Callable[[Dict[str, Tuple[str, ...]]], None]
    on_failure: Callable[[str], None]
    expected: Dict[str, str]                        # name -> cipher sha256
    sizes: Dict[str, int]
    sent: Dict[str, int] = field(default_factory=dict)
    servers: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    left: Set[str] = field(default_factory=set)


class BlossomUploads(QObject):
    """The ``upload`` port: each SMF1 file as a plain Blossom upload
    through ``store`` (MediaStore), the first server, then the mirrors.
    Answers the servers that hold each file, by its hash."""

    def __init__(self, store, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._jobs: Dict[str, _Upload] = {}
        store.upload_progress.connect(self._progress)
        store.upload_finished.connect(self._finished)
        store.upload_failed.connect(self._failed)

    def __call__(self, pictures: Sequence[EncryptedPicture], on_progress, on_done,
                 on_failure) -> Callable[[], None]:
        names = {f"{p.sha256[:16]}.smf1": p for p in pictures}
        job = _Upload(on_progress, on_done, on_failure,
                      expected={n: p.sha256 for n, p in names.items()},
                      sizes={n: p.encryptor.cipher_size for n, p in names.items()},
                      left=set(names))
        for name in names:
            self._jobs[name] = job
        for name, picture in names.items():
            if self._jobs.get(name) is job:
                # Contract 7: never the plaintext's type. Sent whatever the
                # library says: a server said it misses a file (12.4).
                self._store.upload_bytes(picture.cipher, name=name,
                                         mime_type="application/octet-stream", force=True,
                                         submission=True)
        return lambda: self._stop(job)

    def _end(self, job: _Upload) -> None:
        for name in [n for n, j in self._jobs.items() if j is job]:
            del self._jobs[name]

    def _stop(self, job: _Upload) -> None:
        names = [n for n, j in self._jobs.items() if j is job]
        self._end(job)
        for name in names:
            self._store.cancel_upload(name)

    def _progress(self, name: str, sent: int, _total: int) -> None:
        job = self._jobs.get(name)
        if job is not None:
            job.sent[name] = sent
            job.on_progress(sum(job.sent.values()) / max(1, sum(job.sizes.values())))

    def _finished(self, name: str, media) -> None:
        job = self._jobs.get(name)
        if job is None:
            return
        if media.hash != job.expected[name]:
            self._failed(name, "")
            return
        job.servers[media.hash] = tuple(u.get("server", "") for u in media.urls
                                        if u.get("server"))
        job.left.discard(name)
        if not job.left:
            self._end(job)
            job.on_done(dict(job.servers))

    def _failed(self, name: str, reason: str) -> None:
        job = self._jobs.get(name)
        if job is not None:
            self._stop(job)
            job.on_failure(reason)


class _PoolSigner:
    """The account's signer for sealing a message (conversation.py)."""

    def __init__(self, session_pool, profile) -> None:
        self._sign = session_signer(session_pool, profile)

    def sign_event(self, unsigned: dict, *, on_success, on_failure) -> None:
        self._sign(unsigned, on_success, on_failure)


@dataclass(frozen=True)
class RoundAnswer:
    """The round a text goes to a publication in (drafting.choose_round):
    ``round``, or None while it is still open there or cannot be told
    (``words`` say why then). ``unsent`` is the list's entry of an
    earlier try the server has no receipt for: it is sent again, with its
    own secrets (Erneut), or discarded (Verwerfen)."""

    round: Optional[int]
    words: str = ""
    unsent: Optional[SubmissionEntry] = None


class SubmissionRun(QObject):
    """One submission on its way: :class:`Submitting` with its progress as
    a Qt signal, for the sheet and for the window. ``draft`` and
    ``choice`` name the work and where it goes: one run per both."""

    changed = Signal(object)            # Progress

    def __init__(self, draft: ArticleDraft, choice: Choice, *, writer: str, round_: int,
                 ports: Ports, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.draft = draft
        self._started = False
        self._let_go = False
        self._behind: Optional[Callable[[Progress], None]] = None
        self.submitting = Submitting(draft, choice, writer=writer, round_=round_,
                                     ports=ports, on_change=self.changed.emit)

    @property
    def progress(self) -> Progress:
        return self.submitting.progress

    @property
    def choice(self) -> Choice:
        return self.submitting.choice

    @property
    def finished(self) -> bool:
        """Ended, its first message included: a run whose message is still
        being signed or sent, or did not go while no sheet showed it, is
        shown again for its work (Send Again there) until :meth:`let_go`."""
        return _ended(self.progress) and (self._let_go or self.progress.message
                                          in (MessageStatus.NONE, MessageStatus.SENT))

    def let_go(self) -> None:
        """A sheet showing it in, its message not sent, was closed: the
        person saw Send Again and chose not to; the text may be submitted
        elsewhere."""
        self._let_go = True

    def behind_the_sheet(self, on_change: Optional[Callable[[Progress], None]]) -> None:
        """What follows its progress while no sheet shows it (the status
        bar's words), or ``None`` while a sheet shows it again: one at a
        time, so a sheet closed twice says each change once."""
        if self._behind is not None:
            self.changed.disconnect(self._behind)
        self._behind = on_change
        if on_change is not None:
            self.changed.connect(on_change)

    def start(self) -> None:
        """Once: a run shown again (one per work) goes on where it is."""
        if not self._started:
            self._started = True
            self.submitting.start()

    def cancel(self) -> None:
        self.submitting.cancel()

    def retry(self) -> None:
        self.submitting.retry()

    def retry_message(self) -> None:
        self.submitting.retry_message()


class _Account(QObject):
    """What belongs to one account."""

    def __init__(self, profile, *, session_pool, relay_pool, relay_directory, server: str,
                 entitled_relays, nam, parent: QObject) -> None:
        super().__init__(parent)
        self.profile = profile
        self.writer = profile.user_pubkey
        self.api = StandupApi(session_signer(session_pool, profile), server=server, nam=nam,
                              parent=self)
        lists = dict(session_pool=session_pool, relay_directory=relay_directory,
                     query=ListQuery(relay_pool, parent=self),
                     publisher=functools.partial(publish_once, relay_pool), clock=time.time,
                     entitled_relays=entitled_relays, parent=self)
        self.submissions = SubmissionsList(profile, **lists)
        self.read_state = ReadState(profile, **lists)
        self.media_keys = MediaKeys()
        self.epochs = trust.EpochStore()
        self.relay_lookup = trust.relay_key_lookup(relay_pool, relay_directory)
        self.destinations = trust.Destinations(self.api, self.relay_lookup, self.epochs,
                                               parent=self)
        self.signer = _PoolSigner(session_pool, profile)
        self.runs: List[SubmissionRun] = []
        # Why the list of submissions could not be read, once read for a
        # sheet ("" when it could): said again without asking again.
        self.list_problem = ""


def _ended(progress: Progress) -> bool:
    """In, failed or cancelled: nothing of the submission goes on (its
    first message may)."""
    return progress.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED)


def _stopped_words(runs: Sequence[SubmissionRun]) -> str:
    """What the status bar says when the account of ``runs`` is let go
    while some are on their way, or ``""``. The words follow what reached
    the server (lesson 8): nothing, a request it may hold (the submit
    sheet, opened again with that account, finds out), a receipt with a
    picture still going up, or a first message being signed or sent (one
    that did not go was said then). The run furthest from known is
    named."""
    going = [run for run in runs if not _ended(run.progress)]
    if going:
        def known(run: SubmissionRun) -> int:
            progress = run.progress
            return (0 if progress.sent and progress.receipt is None
                    else 1 if progress.receipt is not None else 2)

        run = min(going, key=known)
        progress = run.progress
        text = (words.STOPPED_PICTURES if progress.receipt is not None
                else words.STOPPED_UNCONFIRMED if progress.sent
                else words.STOPPED_NOTHING_SENT)
        others = len(going) - 1
    else:
        talking = [run for run in runs if run.progress.message
                   in (MessageStatus.SIGNING, MessageStatus.SENDING)]
        if not talking:
            return ""
        run, text, others = talking[0], words.STOPPED_MESSAGE, 0
    said = _(text).format(publication=run.choice.name)
    if others:
        said += " " + ngettext("{count} other submission stopped as well.",
                               "{count} other submissions stopped as well.",
                               others).format(count=others)
    return said


class StandupWriter(QObject):
    """See the module docstring. ``server``, ``uploads`` and
    ``accounts(profile)`` (what belongs to one account, as _Account has
    it) are seams for tests; the app's server is the one a developer
    names, or none."""

    status = Signal(str)
    # The account of this pubkey was let go (sign-out, another account, a
    # signer paired anew): a sheet still open for it closes (RG-M2b 9).
    stopped = Signal(str)

    def __init__(self, *, session_pool, relay_pool, relay_directory, media_store,
                 entitled_relays=None, server: Optional[str] = None, nam=None,
                 uploads: Optional[Callable] = None, accounts: Optional[Callable] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._session_pool = session_pool
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._entitled_relays = entitled_relays
        self._server = configured_server() if server is None else server
        self._nam = nam
        self._uploads = uploads or BlossomUploads(media_store, parent=self)
        self._accounts = accounts or self._new_account
        self._account: Optional[_Account] = None

    @property
    def server(self) -> str:
        """Where submissions go, or ``""``: the address comes from the
        association, and without one nothing is sent."""
        return self._server

    def account(self, profile) -> _Account:
        account = self._account
        if account is None or account.writer != profile.user_pubkey:
            self.stop()
            account = self._account = self._accounts(profile)
        return account

    def _new_account(self, profile) -> _Account:
        return _Account(profile, session_pool=self._session_pool, relay_pool=self._relay_pool,
                        relay_directory=self._relay_directory, server=self._server,
                        entitled_relays=self._entitled_relays, nam=self._nam, parent=self)

    # -- what the sheet asks -------------------------------------------------

    def instance(self, profile, on_operator: Callable[[str], None]) -> None:
        """Who runs the server (``GET /api/editorial/instance``), for the
        privacy line that names it; nothing when it cannot be asked."""
        self.account(profile).api.instance(lambda instance: on_operator(instance.operator),
                                           lambda _error: None)

    def destinations(self, profile, on_done: Callable[[List[Destination], List[str]], None],
                     on_failure: Callable[[object], None]) -> None:
        """The publications open for submissions, each with its verified
        key, and the ones this account submitted to before, newest first
        (from its list, once read)."""
        account = self.account(profile)

        account.list_problem = ""                   # read anew

        def found(destinations: List[Destination]) -> None:
            def used() -> None:
                entries = sorted((e for e in account.submissions.entries() if e.role == "writer"),
                                 key=lambda e: e.updated_at, reverse=True)
                on_done(destinations, list(dict.fromkeys(e.publication for e in entries)))

            def read(result) -> None:
                account.list_problem = _list_problem(result)
                used()

            if account.submissions.loaded:
                used()
            else:
                account.submissions.load(read)

        account.destinations.find(found, on_failure)

    def running(self, profile, d: str, publication: str = "") -> Optional[SubmissionRun]:
        """The run of the work ``d`` (to ``publication``, or to any) that
        has not ended: there is one per work and publication."""
        account = self._account
        if account is None or account.writer != profile.user_pubkey:
            return None
        return next((run for run in account.runs if not run.finished and run.draft.d == d
                     and publication in ("", run.choice.publication)), None)

    def round_for(self, profile, publication: str, d: str,
                  on_done: Callable[[RoundAnswer], None]) -> None:
        """The round this text goes to ``publication`` in (see
        :class:`RoundAnswer`). Never guessed: when the list or the server
        cannot be read now, the answer says so, as a guess could take the
        place of the entry, and with it the keys, of a submission sent
        before (RG-M2b 1)."""
        account = self.account(profile)
        if self.running(profile, d, publication) is not None:
            answer_later(on_done, RoundAnswer(None))
            return

        def known() -> None:
            mine = sorted((e for e in account.submissions.entries()
                           if e.role == "writer" and e.publication == publication
                           and e.d == d and e.status_key),
                          key=lambda e: e.round)[-formats.MAX_LOOKUP_ITEMS:]
            if not mine:
                on_done(RoundAnswer(1))
                return

            def found(items) -> None:
                states = {sid: item.submission.state if item else None
                          for sid, item in items.items()}
                choice = choose_round(mine, states)
                on_done(RoundAnswer(choice.round, unsent=choice.unsent))

            # Every entry of this work is asked about (contract 2): the
            # server has something it could not say, or nothing could be
            # asked: not told, so not guessed. Rounds before the newest
            # 50 are closed (a round opens only after the last closed).
            look_up_all(account.api, mine, found,
                        lambda _error: on_done(RoundAnswer(None, _(drafting.ROUND_UNKNOWN))))

        def decide() -> None:
            if account.list_problem:
                on_done(RoundAnswer(None, account.list_problem))
            elif not account.submissions.complete:
                # A page of the list has not arrived: an entry of this
                # text may be on it.
                on_done(RoundAnswer(None, _(SUBMISSIONS_MISSING_PAGE)))
            else:
                known()

        def read(result) -> None:
            account.list_problem = _list_problem(result)
            decide()

        # A list read once for this sheet and not readable is said again,
        # not asked again (another signer prompt); Try Again reads anew.
        if account.submissions.loaded or account.list_problem:
            answer_later(decide)
        else:
            account.submissions.load(read)

    def discard(self, profile, entry: SubmissionEntry, on_done: Callable[[bool], None]) -> None:
        """Verwerfen (contract 13): a tombstone for ``entry``, an earlier
        try the server has no receipt for; ``on_done(stored)``."""
        self.account(profile).submissions.forget(entry, lambda result: on_done(result.stored))

    def submit(self, profile, draft: ArticleDraft, choice: Choice,
               answer: RoundAnswer) -> SubmissionRun:
        """A run for ``draft`` to ``choice``'s publication in the answered
        round, or the one already on its way there (one per work and
        publication: a second would record the same sid with other keys)."""
        account = self.account(profile)
        running = self.running(profile, draft.d, choice.publication)
        if running is not None:
            return running
        # Owned by the list (and the sheet showing it), not by Qt: one that
        # has ended goes once nothing shows it.
        run = SubmissionRun(draft, choice, writer=account.writer, round_=answer.round,
                            ports=self._ports(account, choice.publication))
        account.runs = [r for r in account.runs if not r.finished] + [run]
        return run

    def in_background(self, run: SubmissionRun) -> None:
        """The sheet closed: say in the status bar how ``run`` goes on,
        each change once: submitting, in, why the signer app asks for the
        first message, that it did not go, or a failure."""
        if run.finished:
            return
        name = run.choice.name
        said = [""]

        def say(text: str) -> None:
            if text and text != said[0]:
                said[0] = text
                self.status.emit(text)

        def words_of(progress: Progress) -> str:
            if progress.phase is Phase.DONE:
                return (_background_message_words(progress, name)
                        or _(words.SUBMITTED).format(publication=name))
            if progress.phase is Phase.FAILED and progress.failure is not None:
                return _(progress.failure.headline).format(publication=name)
            return ""

        say(words_of(run.progress) or _(words.IN_THE_BACKGROUND))
        run.behind_the_sheet(lambda progress: say(words_of(progress)))

    def stop(self, on_done: Optional[Callable[[], None]] = None) -> None:
        """Sign-out or another account (13): nothing on its way answers
        any more, the submissions list and the media keys are forgotten
        at once, the server is asked to revoke the session (a token left
        alive would act for the account for up to 12 hours), and what was
        read is written before its list stops; ``on_done`` once both are
        done."""
        account, self._account = self._account, None
        if account is None:
            if on_done is not None:
                on_done()
            return
        said = _stopped_words(account.runs)
        for run in account.runs:
            run.submitting.stop()
        account.destinations.stop()
        account.submissions.stop()
        account.media_keys.clear()
        waiting = {"session", "read state"}

        def finished(what: str) -> None:
            waiting.discard(what)
            if not waiting:
                # Its server client, its lists and their queries go with it
                # (RG-M2b 11: every account signed in once stayed alive).
                account.deleteLater()
                if on_done is not None:
                    on_done()

        account.api.close_session(lambda: finished("session"))
        account.api.stop()

        def written(_result) -> None:
            account.read_state.stop()
            finished("read state")

        account.read_state.before_sign_out(written)
        self.stopped.emit(account.writer)
        if said:
            # After the account change is done, so what the window says
            # about that change does not cover what was lost.
            answer_later(self.status.emit, said)

    # -- the ports -----------------------------------------------------------

    def _ports(self, account: _Account, publication: str) -> Ports:
        api = account.api

        def confirm_key(on_key, on_refused) -> None:
            trust.confirm_key(api, publication, account.relay_lookup, account.epochs, on_key,
                              on_refused)

        def submit(body, on_sending, on_success, on_failure):
            call = api.submit(body, on_success, on_failure, on_sending=on_sending)
            return lambda: api.drop(call)

        def lookup(sealed: Sealed, on_done, on_failure) -> None:
            resolve_unconfirmed(api, sealed.entry, on_done, on_failure, body=sealed.body)

        def send_message(sealed: Sealed, text: str, on_signed, on_sent, on_failure) -> None:
            self._send_message(account, sealed, text, on_signed, on_sent, on_failure)

        def forget(entry) -> None:
            # Best effort: an entry left behind is found not sent the next
            # time, and sent again with its own secrets (contract 2).
            account.submissions.forget(entry, lambda _result: None)

        return Ports(confirm_key=confirm_key, upload=self._uploads,
                     held=account.submissions.entry, record=account.submissions.record,
                     forget=forget, submit=submit, lookup=lookup, send_message=send_message,
                     later=_later, clock=time.time)

    @staticmethod
    def _send_message(account: _Account, sealed: Sealed, text: str, on_signed, on_sent,
                      on_failure) -> None:
        """The first message, after the receipt: sealed by the account
        (one signer prompt), wrapped to the submission's room, sent with a
        session; only the server's ``seq`` says it is there."""
        room = sealed.body["p_sub"]
        try:
            rumor = conversation.message_rumor(room=room, submission_id=sealed.submission_id,
                                               text=text, sender=account.writer,
                                               created_at=int(time.time()))
        except formats.FormatError:
            answer_later(on_failure)
            return

        def wrapped(wrap: dict) -> None:
            on_signed()
            account.api.send_message(sealed.submission_id, wrap, kind_hint="message",
                                     on_success=lambda _seq: on_sent(),
                                     on_failure=lambda _error: on_failure())

        conversation.seal_and_wrap(account.signer, rumor, sealed.s_sub, room,
                                   on_success=wrapped, on_failure=lambda _why: on_failure())


def _background_message_words(progress: Progress, publication: str) -> str:
    """With the sheet closed, what the status bar says of the first
    message: why the signer app asks now, or that it did not go; ``""``
    otherwise (it is sent, or there is none)."""
    if progress.message is MessageStatus.SIGNING:
        return _(words.MESSAGE_SIGNING_IN_THE_BACKGROUND).format(publication=publication)
    if progress.message is MessageStatus.FAILED:
        return _(words.MESSAGE_NOT_SENT_IN_THE_BACKGROUND).format(publication=publication)
    return ""


def _list_problem(read) -> str:
    """Why the list of submissions could not be read, or ``""``."""
    return "" if read.readable else (read.words or _(SUBMISSIONS_UNREADABLE))


def _later(ms: int, callback: Callable[[], None]) -> None:
    QTimer.singleShot(max(0, int(ms)), callback)


__all__ = ["BlossomUploads", "RoundAnswer", "StandupWriter", "SubmissionRun", "thumbnail"]
