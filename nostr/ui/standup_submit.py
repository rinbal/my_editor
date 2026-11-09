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

The pictures (:class:`BlossomUploads`, :func:`thumbnail`) are
:mod:`.standup_uploads`'s, offered here too.
"""

from __future__ import annotations

import functools
import time
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import _, ngettext

from ..einundzwanzig_api.client import session_signer
from ..list_relays import ListQuery, publish_once
from ..later import answer_later
from ..standup import conversation, formats, trust, words
from ..standup.client import StandupApi
from ..standup import drafting
from ..standup.drafting import ArticleDraft, Choice, Sealed
from ..standup.entries import SubmissionEntry
from ..standup.errors import Code, StandupError
from ..standup.lists import SUBMISSIONS_UNREADABLE, ReadState, SubmissionsList
from ..standup.opening import MediaKeys
from ..standup.servers import configured_server
from ..standup.submitting import (
    HELD_UNKNOWN_FIRST, HELD_WORDS, MessageStatus, Phase, Ports, Progress, Submitting,
)
from ..standup.rounds import RoundAnswer, ask_round
from ..standup.trust import Destination
from ..standup.watcher import resolve_unconfirmed
from .standup_uploads import THUMB_SIDE, BlossomUploads, thumbnail

class _PoolSigner:
    """The account's signer for sealing a message (conversation.py)."""

    def __init__(self, session_pool, profile) -> None:
        self._sign = session_signer(session_pool, profile)

    def sign_event(self, unsigned: dict, *, on_success, on_failure) -> None:
        self._sign(unsigned, on_success, on_failure)


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
        self._sign = session_signer(session_pool, profile)
        self._nam = nam
        self.api = StandupApi(self._sign, server=server, nam=nam, parent=self)
        # Clients of other servers an entry of the list names, made when
        # one is asked about (RG-M2r 9), let go with the account.
        self.clients: Dict[str, StandupApi] = {}
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
        # Let go (StandupWriter.stop): no answer that comes later reaches
        # its client, its lists or its signer again (lesson 31, _live).
        self.stopped = False
        # Try Again asked for the list to be read again (reload).
        self.read_again = False

    def client_for(self, server: str) -> StandupApi:
        """The client of ``server``: the account's own, or, for an entry
        of another server, one of its own, asked about that entry only
        (an entry asked of another server reads as never stored)."""
        if server == self.api.server:
            return self.api
        client = self.clients.get(server)
        if client is None:
            client = self.clients[server] = StandupApi(self._sign, server=server,
                                                       nam=self._nam, parent=self)
        return client


def _live(account: _Account, callback: Callable) -> Callable:
    """``callback``, answered only while ``account`` is not let go: a late
    answer (a signer app, a relay, the server) never opens a session,
    posts, signs or reads for it, and never touches an object deleted
    with it (RG-M2r 3)."""
    def guarded(*args) -> None:
        if not account.stopped:
            callback(*args)
    return guarded


def _ended(progress: Progress) -> bool:
    """In, failed or cancelled: nothing of the submission goes on (its
    first message may)."""
    return progress.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED)


def _stopped_words(runs: Sequence[SubmissionRun]) -> str:
    """What the status bar says when the account of ``runs`` is let go
    while some are on their way, or ``""``. The words come from what the
    server holds (``Progress.held``, lesson 32, the same fact a failure
    says): nothing, a request it may hold (the submit sheet, opened again
    with that account, finds out), the revision with a picture still
    going up, or, once in, a first message being signed or sent (one that
    did not go was said then). The run furthest from known is named."""
    going = [run for run in runs if not _ended(run.progress)]
    if going:
        run = min(going, key=lambda r: HELD_UNKNOWN_FIRST.index(r.progress.held))
        text = HELD_WORDS[run.progress.held].stopped
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
    # The submissions list of this pubkey was read, or a run recorded an
    # entry on it: what has_writer_entry says may have changed.
    listed = Signal(str)

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
                self.listed.emit(account.writer)
                used()

            # A list read whole is not read again for another sheet; one
            # that missed a page is, each time (RG-M2r 4): the page may
            # have arrived.
            if account.submissions.loaded and account.submissions.complete:
                used()
            else:
                account.submissions.load(_live(account, read))

        account.destinations.find(_live(account, found), _live(account, on_failure))

    def has_writer_entry(self, profile, d: str) -> bool:
        """Whether the text ``d`` has a writer entry of this account: the
        list as read names it, or a run of it is on its way. Its link name
        is fixed then (RG-M2r 10): the sid comes from it. Asked without
        reading anything (an account not in use here says no)."""
        account = self._account
        if not d or account is None or account.writer != profile.user_pubkey:
            return False
        return any(run.draft.d == d for run in account.runs) or any(
            entry.role == "writer" and entry.d == d for entry in account.submissions.entries())

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
        self._choose(account, publication, d, on_done)

    def _choose(self, account: _Account, publication: str, d: str,
                on_done: Callable[[RoundAnswer], None], *,
                stored: Optional[SubmissionEntry] = None) -> None:
        """Contract 2 from step 1: the list read whole, then every entry
        of this work at ``publication`` asked about (rounds.ask_round).
        ``stored``: the entry of a revision 1 the server refused as
        ``stale_revision``; the list is read anew for it."""
        def known() -> None:
            mine = [e for e in account.submissions.entries()
                    if e.role == "writer" and e.publication == publication and e.d == d
                    and e.status_key]
            ask_round(account.client_for, mine, _live(account, on_done), writer=account.writer,
                      stored=stored)

        def decide() -> None:
            if account.list_problem:
                on_done(RoundAnswer(None, account.list_problem, from_list=True))
            elif not account.submissions.complete:
                # A page of the list has not arrived: an entry of this
                # text may be on it.
                on_done(RoundAnswer(None, _(drafting.ROUND_LIST_INCOMPLETE), from_list=True))
            else:
                known()

        def read(result) -> None:
            account.read_again = False
            account.list_problem = _list_problem(result)
            self.listed.emit(account.writer)
            decide()

        # A list read once for this sheet is not read again for another
        # publication (each read may be a signer prompt): what it said is
        # said again, and Try Again reads it anew (reload).
        if stored is None and (account.submissions.loaded or account.list_problem) \
                and not account.read_again:
            answer_later(_live(account, decide))
        else:
            account.submissions.load(_live(account, read))

    def reload(self, profile) -> None:
        """Try Again on a round the list could not tell (RoundAnswer
        ``from_list``): the next :meth:`round_for` reads the list of
        submissions again, whatever it said before."""
        account = self.account(profile)
        account.list_problem = ""
        account.read_again = True

    def discard(self, profile, entry: SubmissionEntry, on_done: Callable[[bool], None]) -> None:
        """Verwerfen (contract 13): a tombstone for ``entry``, an earlier
        try the server has no receipt for; ``on_done(stored)``."""
        account = self.account(profile)
        account.submissions.forget(entry, _live(account, lambda result: on_done(result.stored)))

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

    def stop(self, on_done: Optional[Callable[[], None]] = None, *,
             on_written: Optional[Callable[[], None]] = None) -> None:
        """Sign-out or another account (13): nothing on its way answers
        any more, the submissions list and the media keys are forgotten
        at once, the server is asked to revoke the session (a token left
        alive would act for the account for up to 12 hours), and what was
        read is written before its list stops; ``on_done`` once both are
        done. ``on_written`` once what was read is written, or its wait
        (lists.SIGN_OUT_WAIT_MS) passed: the writing needs the account's
        signer, so a sign-out forgets the signer only then (RG-M2r 8)."""
        account, self._account = self._account, None
        if account is None:
            if on_written is not None:
                on_written()
            if on_done is not None:
                on_done()
            return
        said = _stopped_words(account.runs)
        account.stopped = True
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
        for client in account.clients.values():
            client.stop()                           # they only looked up: no session

        def written(_result) -> None:
            account.read_state.stop()
            if on_written is not None:
                on_written()
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

        def choose_round(entry: SubmissionEntry, on_done) -> None:
            self._choose(account, publication, entry.d, on_done, stored=entry)

        return Ports(confirm_key=confirm_key, upload=self._uploads,
                     held=account.submissions.entry, record=account.submissions.record,
                     forget=forget, submit=submit, lookup=lookup, send_message=send_message,
                     choose_round=choose_round, later=_later, clock=time.time)

    @staticmethod
    def _send_message(account: _Account, sealed: Sealed, text: str, on_signed, on_sent,
                      on_failure) -> None:
        """The first message, after the receipt: sealed by the account
        (one signer prompt), wrapped to the submission's room, sent with a
        session; only the server's ``seq`` says it is there."""
        room = sealed.p_sub
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
                                     on_failure=lambda error: on_failure(_limit_wait(error)))

        # The signer app may answer after the account was let go: then the
        # message is not posted (no new session for an account left).
        conversation.seal_and_wrap(account.signer, rumor, sealed.s_sub, room,
                                   on_success=_live(account, wrapped),
                                   on_failure=_live(account, lambda _why: on_failure()))


def _limit_wait(error: StandupError) -> Optional[int]:
    """The seconds a refusal for a limit asks to wait, or None."""
    return error.retry_after if error.code == Code.RATE_LIMITED else None


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


__all__ = [
    "BlossomUploads", "RoundAnswer", "StandupWriter", "SubmissionRun", "THUMB_SIDE", "thumbnail",
]
