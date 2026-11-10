# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The window's side of submitting to a STANDUP publication (plan 10.2,
10.3): the real requests behind the ports of
:mod:`nostr.standup.submitting`, for each account (what belongs to one
is :mod:`.standup_account`'s).

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
:mod:`.standup_uploads`'s, and a run (:class:`SubmissionRun`) is
:mod:`.standup_run`'s, offered here too; the first message is
:mod:`.standup_message`'s.

A read of the list that missed a page (a relay that did not give it) is
read again quietly, with backoff, before anything is said (lessons 36,
:func:`~nostr.failures.retry_quietly`).
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import _

from .. import failures
from ..signer_limits import bound_known
from ..later import answer_later
from ..standup import trust, words
from ..standup.drafting import ArticleDraft, Choice, Sealed
from ..standup.entries import SubmissionEntry, stored_elsewhere
from ..standup.errors import PASSING, StandupError
from ..standup.link_names import LinkNameLock, SubmittedMarks, held_of
from ..standup.lists import SUBMISSIONS_UNREADABLE
from ..standup.ports import Reread
from ..standup.servers import configured_server
from ..standup.submitting import Phase, Ports, Progress
from ..standup.rounds import RoundAnswer, ask_round
from ..standup.trust import Destination
from ..standup.watcher import resolve_unconfirmed
from .standup_account import Account, live
from .standup_handoff import phase_words
from .standup_message import send_first_message
from .standup_run import SubmissionRun, background_message_words, stopped_words
from .standup_uploads import THUMB_SIDE, BlossomUploads, thumbnail


class StandupWriter(QObject):
    """See the module docstring. ``server``, ``uploads`` and
    ``accounts(profile)`` (what belongs to one account, as Account has
    it) are seams for tests; the app's server is the one a developer
    names, or none."""

    status = Signal(str)
    # The account of this pubkey was let go (sign-out, another account, a
    # signer paired anew): a sheet still open for it closes (RG-M2b 9).
    stopped = Signal(str)
    # The submissions list of this pubkey was read, or a run recorded an
    # entry on it: what link_name says may have changed.
    listed = Signal(str)

    def __init__(self, *, session_pool, relay_pool, relay_directory, media_store,
                 entitled_relays=None, server: Optional[str] = None, nam=None,
                 uploads: Optional[Callable] = None, accounts: Optional[Callable] = None,
                 marks: Optional[SubmittedMarks] = None,
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
        self._account: Optional[Account] = None
        # What this computer holds of the texts submitted, across restarts
        # (RG-M2r2a 1); the window's is kept in its file, a test's is not.
        self._marks = marks if marks is not None else SubmittedMarks(kept=False)

    @property
    def server(self) -> str:
        """Where submissions go, or ``""``: the address comes from the
        association, and without one nothing is sent."""
        return self._server

    def account(self, profile) -> Account:
        account = self._account
        if account is None or account.writer != profile.user_pubkey:
            self.stop()
            account = self._account = self._accounts(profile)
        return account

    def _new_account(self, profile) -> Account:
        return Account(profile, session_pool=self._session_pool, relay_pool=self._relay_pool,
                        relay_directory=self._relay_directory, server=self._server,
                        entitled_relays=self._entitled_relays, nam=self._nam, later=_later,
                        parent=self)

    # -- what the sheet asks -------------------------------------------------

    def signer_bound(self, profile, done: Callable[[], None]) -> None:
        """``done()`` once what ``profile``'s signer connection carries is
        known (signer_limits.bound_known), for the measure of the message's
        seal: the session pool reads its relays' documents, and that alone
        is waited for (RG-M2r2b 13), never the signer reattaching, which
        may end before the reading does (lesson 29). Nothing for an account
        let go (lesson 31)."""
        account = self.account(profile)
        if bound_known(profile):
            answer_later(done)
            return
        self._session_pool.read_bounds(profile, live(account, done))

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
                self._listed(account, result)
                used()

            # A list read whole is not read again for another sheet; one
            # that missed a page is, each time (RG-M2r 4): the page may
            # have arrived. Quietly again first (lessons 36).
            if account.submissions.loaded and account.submissions.complete:
                used()
            else:
                account.read_whole(read)

        # The server not reached, or in trouble: asked again quietly first
        # (lessons 36); the sheet says so once the tries are spent.
        def answered(result) -> None:
            if isinstance(result, StandupError):
                on_failure(result)
            else:
                found(result)

        failures.retry_quietly(
            lambda answer: account.destinations.find(live(account, answer),
                                                     live(account, answer)),
            settled=lambda result: not (isinstance(result, StandupError)
                                        and result.code in PASSING),
            on_done=answered, later=_later)

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

    def _choose(self, account: Account, publication: str, d: str,
                on_done: Callable[[RoundAnswer], None]) -> None:
        """Contract 2 from step 1: the list read whole, then every entry
        of this work at ``publication`` asked about (rounds.ask_round).
        An entry the lookups found the server holds is marked stored on
        the list (13, 72d5ddc)."""
        def answered(answer: RoundAnswer) -> None:
            if answer.stored:
                account.submissions.mark_stored(answer.stored)
                for entry in answer.stored:
                    self._marks.remember(account.writer, entry.d, stored=True)
            on_done(answer)

        def known() -> None:
            mine = [e for e in account.submissions.entries()
                    if e.role == "writer" and e.publication == publication and e.d == d
                    and e.status_key]
            ask_round(account.client_for, mine, live(account, answered), later=_later,
                      refused=lambda entry: self._marks.round_refused(account.writer, entry))

        def decide() -> None:
            if account.list_problem:
                on_done(RoundAnswer(None, account.list_problem, from_list=True))
            elif not account.submissions.complete:
                # A page of the list has not arrived, read again quietly:
                # an entry of this text may be on it.
                on_done(RoundAnswer(None, _(failures.CHECK_CONNECTION), from_list=True))
            else:
                known()

        def read(result) -> None:
            account.read_again = False
            self._listed(account, result)
            decide()

        # A list read once for this sheet is not read again for another
        # publication (each read may be a signer prompt): what it said is
        # said again, and Try Again reads it anew (reload).
        if (account.submissions.loaded or account.list_problem) and not account.read_again:
            answer_later(live(account, decide))
        else:
            account.read_whole(read)

    def reload(self, profile) -> None:
        """Try Again on a round the list could not tell (RoundAnswer
        ``from_list``): the next :meth:`round_for` reads the list of
        submissions again, whatever it said before."""
        account = self.account(profile)
        account.list_problem = ""
        account.read_again = True

    def discard(self, profile, entry: SubmissionEntry, on_done: Callable[[bool], None]) -> None:
        """Verwerfen (contract 13): a keyed tombstone for ``entry``, an
        earlier try the server has no receipt for; ``on_done(stored)``."""
        account = self.account(profile)

        def forgotten(result) -> None:
            self._forgotten(account, entry.d, result)
            on_done(result.stored)

        account.submissions.forget(entry, live(account, forgotten))

    def _forgotten(self, account: Account, d: str, result) -> None:
        """A tombstone for an entry of ``d`` was written: once the list,
        read whole, holds no writer entry of ``d`` and no run of it is on
        its way, its link name is free again (the mark goes)."""
        if not result.stored or not account.submissions.complete:
            return
        if any(e.role == "writer" and e.d == d for e in account.submissions.entries()) \
                or any(run.draft.d == d and not run.finished for run in account.runs):
            return
        self._marks.forget(account.writer, d)
        self.listed.emit(account.writer)

    def _listed(self, account: Account, result) -> None:
        """A read of the list answered: what it could not tell, the marks
        of every writer entry it holds (kept across restarts), and that
        Article Details may say another thing now."""
        account.list_problem = _list_problem(result)
        for entry in account.submissions.entries():
            if entry.role == "writer":
                self._marks.remember(account.writer, entry.d, stored=entry.stored)
        self.listed.emit(account.writer)

    # -- the link name (RG-M2r 10, RG-M2r2a 1 and 2) ---------------------------

    def link_name(self, profile, d: str, *, in_use: bool) -> LinkNameLock:
        """Whether the text ``d`` keeps its link name, and why: a run of
        it on its way (what it holds), an entry of the list as read, or
        this computer's mark (kept across restarts). Without one, while
        the account is ``in_use``, the list is read (once) and the name
        stays read-only until a read of it whole answers; with no account
        in use only the mark tells. Never free on a list not read."""
        writer = getattr(profile, "user_pubkey", "")
        if not d or not writer:
            return LinkNameLock()
        account = self._account if self._account is not None \
            and self._account.writer == writer else None
        if account is not None:
            run = next((r for r in account.runs if r.draft.d == d and not r.finished), None)
            if run is not None:
                return LinkNameLock(held=run.progress.held)
            mine = [e for e in account.submissions.entries() if e.role == "writer" and e.d == d]
            if mine:
                return LinkNameLock(held=held_of(any(e.stored for e in mine)))
        mark = self._marks.get(writer, d)
        if mark is not None:
            return LinkNameLock(held=held_of(mark))
        if not in_use:
            return LinkNameLock()
        account = self.account(profile)
        submissions = account.submissions
        # A read on its way first: what the list says is not settled until
        # it answered (lesson 29).
        if account.checking:
            return LinkNameLock(checking=True)
        if submissions.loaded and submissions.complete and not account.list_problem:
            return LinkNameLock()                   # read whole: none of this text
        if account.check_failed:
            return LinkNameLock(unchecked=True)
        self._check(account)
        return LinkNameLock(checking=True)

    def check_link_name(self, profile) -> None:
        """Check Again, for a link name a read could not tell."""
        account = self.account(profile)
        if not account.checking:
            self._check(account)
            self.listed.emit(account.writer)

    def _check(self, account: Account) -> None:
        account.checking, account.check_failed = True, False

        def read(result) -> None:
            account.checking = False
            account.check_failed = not result.readable or not account.submissions.complete
            self._listed(account, result)

        # Never answered within the call: Article Details is being shown
        # with what this call returns (lesson 29).
        account.read_whole(lambda result: answer_later(live(account, read), result))

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
                return (background_message_words(progress, name)
                        or _(words.SUBMITTED).format(publication=name))
            if progress.phase is Phase.FAILED and progress.failure is not None:
                # Its title and its one sentence, as the handoff says them.
                return failures.spoken(*phase_words(progress, name))
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
        said = stopped_words(account.runs)
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

    def _ports(self, account: Account, publication: str) -> Ports:
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
            send_first_message(account, sealed, text, on_signed, on_sent, on_failure,
                               later=_later)

        def record(entry: SubmissionEntry, on_done) -> None:
            # Once the entry is on the list, the link name stays, after a
            # restart too (RG-M2r2a 1); until then the run holds it.
            def recorded(result) -> None:
                if result.stored:
                    self._marks.remember(account.writer, entry.d, stored=result.entry.stored)
                on_done(result)

            account.submissions.record(entry, recorded)

        def forget(entry) -> None:
            # Best effort: an entry left behind is found not sent the next
            # time, and sent again with its own secrets (contract 2).
            account.submissions.forget(entry, live(
                account, lambda result: self._forgotten(account, entry.d, result)))

        def mark_stored(entry) -> None:
            # Best effort too: held here at once, written with the next
            # write when this one does not end on the relays.
            self._marks.remember(account.writer, entry.d, stored=True)
            account.submissions.mark_stored([entry])
            self.listed.emit(account.writer)

        def reload(entry: SubmissionEntry, on_done) -> None:
            self._reread(account, entry, live(account, on_done))

        return Ports(confirm_key=confirm_key, upload=self._uploads,
                     held=account.submissions.entry, record=record,
                     mark_stored=mark_stored, forget=forget, submit=submit, lookup=lookup,
                     send_message=send_message, reload=reload,
                     refuse=lambda entry: self._marks.refuse_round(account.writer, entry),
                     refused=lambda entry: self._marks.round_refused(account.writer, entry),
                     later=_later, clock=time.time)

    def _reread(self, account: Account, entry: SubmissionEntry,
                on_done: Callable[[Reread], None]) -> None:
        """The list read again after a ``stale_revision`` for ``entry``'s
        revision 1: another device stored that round first (contract 2 at
        72d5ddc). Arrived once the list holds a stored entry for the sid
        (its secrets beat this one's in the merge, 13)."""
        def read(result) -> None:
            self._listed(account, result)
            arrived = stored_elsewhere(account.submissions.entry(entry.sid), entry)
            # A page that did not come may hold it: Reload reads again.
            on_done(Reread(arrived, "" if arrived else account.list_problem))

        account.read_whole(read)


def _list_problem(read) -> str:
    """Why the list of submissions could not be read, or ``""``."""
    return "" if read.readable else (read.words or _(SUBMISSIONS_UNREADABLE))


def _later(ms: int, callback: Callable[[], None]) -> None:
    QTimer.singleShot(max(0, int(ms)), callback)


__all__ = [
    "BlossomUploads", "RoundAnswer", "StandupWriter", "SubmissionRun", "THUMB_SIDE", "thumbnail",
]
