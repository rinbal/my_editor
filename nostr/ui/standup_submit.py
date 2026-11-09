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

from i18n import _

from ..einundzwanzig_api.client import session_signer
from ..list_relays import ListQuery, publish_once
from ..signed_api import answer_later
from ..standup import conversation, formats, trust, words
from ..standup.client import StandupApi
from ..standup import drafting
from ..standup.drafting import ArticleDraft, Choice, EncryptedPicture, Sealed, round_after
from ..standup.lists import SUBMISSIONS_UNREADABLE, ReadState, SubmissionEntry, SubmissionsList
from ..standup.opening import MediaKeys
from ..standup.servers import configured_server
from ..standup.submitting import Phase, Ports, Progress, Submitting
from ..standup.trust import Destination
from ..standup.watcher import resolve_unconfirmed

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
                self._store.upload_bytes(picture.cipher, name=name,
                                         mime_type="application/octet-stream")
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
    """The round a text goes to a publication in (drafting.round_after):
    ``round``, or None while it is still open there or cannot be told
    (``words`` say why then). ``replaces`` is the list's entry of an
    earlier try of the same round that the server proved it never
    stored: the one entry the new run may take the place of."""

    round: Optional[int]
    words: str = ""
    replaces: Optional[SubmissionEntry] = None


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
        return self.progress.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED)

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


class StandupWriter(QObject):
    """See the module docstring. ``server``, ``uploads`` and
    ``accounts(profile)`` (what belongs to one account, as _Account has
    it) are seams for tests; the app's server is the one a developer
    names, or none."""

    status = Signal(str)

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

        def found(destinations: List[Destination]) -> None:
            def used() -> None:
                entries = sorted((e for e in account.submissions.entries() if e.role == "writer"),
                                 key=lambda e: e.updated_at, reverse=True)
                on_done(destinations, list(dict.fromkeys(e.publication for e in entries)))

            if account.submissions.loaded:
                used()
            else:
                account.submissions.load(lambda _read: used())

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
            mine = [e for e in account.submissions.entries()
                    if e.role == "writer" and e.publication == publication and e.d == d]
            entry = max(mine, key=lambda e: e.round, default=None)
            if entry is None:
                on_done(RoundAnswer(1))
                return

            def found(answer) -> None:
                if entry.status_key in answer.unreadable:
                    on_done(RoundAnswer(None))  # the server has something: never "nothing"
                    return
                item = answer.found.get(entry.status_key)
                round_ = round_after(entry, item.submission.state if item else None)
                # Nothing stored: the same round again, in this entry's place.
                on_done(RoundAnswer(round_, replaces=entry if item is None else None))

            account.api.lookup([(entry.status_key, 0)], found,
                               lambda _error: on_done(RoundAnswer(None,
                                                                  _(drafting.ROUND_UNKNOWN))))

        def read(result) -> None:
            if result.readable:
                known()
            else:
                on_done(RoundAnswer(None, result.words or _(SUBMISSIONS_UNREADABLE)))

        if account.submissions.loaded:
            answer_later(known)
        else:
            account.submissions.load(read)

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
                            ports=self._ports(account, choice.publication, answer.replaces))
        account.runs = [r for r in account.runs if not r.finished] + [run]
        return run

    def in_background(self, run: SubmissionRun) -> None:
        """The sheet closed: say in the status bar how ``run`` goes on."""
        if run.finished:
            return
        self.status.emit(_(words.IN_THE_BACKGROUND))

        def changed(progress: Progress) -> None:
            if progress.phase is Phase.DONE:
                self.status.emit(_(words.SUBMITTED).format(publication=run.choice.name))
            elif progress.phase is Phase.FAILED and progress.failure is not None:
                self.status.emit(_(progress.failure.headline).format(
                    publication=run.choice.name))

        run.changed.connect(changed)

    def stop(self, on_done: Optional[Callable[[], None]] = None) -> None:
        """Sign-out or another account (13): nothing on its way answers
        any more, the submissions list and the media keys are forgotten
        at once, and what was read is written before its list stops;
        ``on_done`` after that."""
        account, self._account = self._account, None
        if account is None:
            if on_done is not None:
                on_done()
            return
        for run in account.runs:
            run.submitting.stop()
        account.destinations.stop()
        account.submissions.stop()
        account.media_keys.clear()
        account.api.stop()

        def written(_result) -> None:
            account.read_state.stop()
            if on_done is not None:
                on_done()

        account.read_state.before_sign_out(written)

    # -- the ports -----------------------------------------------------------

    def _ports(self, account: _Account, publication: str,
               replaces: Optional[SubmissionEntry]) -> Ports:
        api = account.api

        def confirm_key(on_key, on_refused) -> None:
            trust.confirm_key(api, publication, account.relay_lookup, account.epochs, on_key,
                              on_refused)

        def submit(body, on_sending, on_success, on_failure):
            call = api.submit(body, on_success, on_failure, on_sending=on_sending)
            return lambda: api.drop(call)

        def lookup(entry, on_done, on_failure) -> None:
            resolve_unconfirmed(api, entry, on_done, on_failure)

        def send_message(sealed: Sealed, text: str, on_sent, on_failure) -> None:
            self._send_message(account, sealed, text, on_sent, on_failure)

        def record(entry, on_done) -> None:
            account.submissions.record(entry, on_done, replaces=replaces)

        def forget(entry) -> None:
            # Best effort: an entry left behind is found not stored and
            # replaced by name the next time (RoundAnswer.replaces).
            account.submissions.forget(entry, lambda _result: None)

        return Ports(confirm_key=confirm_key, upload=self._uploads, record=record,
                     forget=forget, submit=submit, lookup=lookup, send_message=send_message,
                     later=_later, clock=time.time)

    @staticmethod
    def _send_message(account: _Account, sealed: Sealed, text: str, on_sent,
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
            account.api.send_message(sealed.submission_id, wrap, kind_hint="message",
                                     on_success=lambda _seq: on_sent(),
                                     on_failure=lambda _error: on_failure())

        conversation.seal_and_wrap(account.signer, rumor, sealed.s_sub, room,
                                   on_success=wrapped, on_failure=lambda _why: on_failure())


def _later(ms: int, callback: Callable[[], None]) -> None:
    QTimer.singleShot(max(0, int(ms)), callback)


__all__ = ["BlossomUploads", "RoundAnswer", "StandupWriter", "SubmissionRun", "thumbnail"]
