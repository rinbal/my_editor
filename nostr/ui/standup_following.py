# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Following one account's submissions (plan 10.4; editorial contract
10, 12.4, 13): what the Einreichungen list, the pill beside a tab, the
notice at an article and the detail show, kept in one place per account
and stopped with it (criterion 19).

:class:`Following` joins three sources into rows
(``nostr/standup/following.py``):

- the account's submissions list, read once at launch;
- the lookup by status key (:class:`~nostr.standup.watcher.SubmissionWatcher`):
  at launch, when the view opens and every 15 minutes, never a signer
  prompt. A receipt it finds for an entry not yet marked stored marks
  it (13), so a request that got no answer before a restart is resolved
  after it (12.4); a copy stored with another envelope is decided by its
  signed action (``verify_stored``);
- the server's Views (``GET /api/me/submissions``, with the session):
  the works the person is named in. Read when the view opens, and after
  a round while a session is there, never at launch: starting the app
  asks no signer.

A round that failed is asked again quietly, with backoff; only when
those tries are spent does the list say once, in place, that the status
could not be loaded (lessons 36).

A detail (:meth:`Following.open`) is read by :mod:`.standup_reading`
(the member list, the latest version, verified before it is opened);
messages and reactions on their way are :mod:`.standup_talk`'s.

Everything answers through :func:`~.standup_account.live`: after
:meth:`Following.stop` nothing writes a row, the list or the read state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from .. import failures
from ..later import answer_later
from ..standup import conversation, formats, views
from ..standup import following as rules
from ..standup import thread as threads
from ..standup.entries import SubmissionEntry
from ..standup.errors import PASSING, StandupError, cause_words
from ..standup.submitting import Phase
from ..standup.watcher import (
    Followed, NotHandedIn, Round, StoredCopy, StoredOther, SubmissionWatcher, verify_stored,
)
from .standup_account import live
from .standup_reading import Detail, Reading, Version, secret_of
from .standup_talk import Outgoing, Talk

# What an article's notice is about (plan 7.4): a message, or a result.
NEWS_EVENTS = ("request_changes", "accept", "decline", "published", "correction")


@dataclass(frozen=True)
class News:
    """Something new for the writer of the work ``d``: ``kind`` is
    ``"message"`` or one of :data:`NEWS_EVENTS`. A message has its
    ``sender`` and who that is (``thread.role_of``): never a stranger."""

    sid: str
    d: str
    kind: str
    title: str
    publication: str            # its name
    sender: str = ""
    role: threads.Role = threads.Role.UNKNOWN


class Following(QObject):
    """See the module docstring. ``marks`` are this computer's marks
    (a round refused as another device's); ``on_listed(read)`` is told a
    read of the list; ``discard(entry, on_done(stored))`` writes a keyed
    tombstone; ``later`` and ``clock`` are seams.

    Signals:
      changed()            the rows may have changed.
      detail_changed(str)  the detail of this sid changed.
      news(News)           something new for the writer of a work.
    """

    changed = Signal()
    detail_changed = Signal(str)
    news = Signal(object)

    def __init__(self, account, *, marks, on_listed: Callable[[object], None],
                 discard: Callable[[SubmissionEntry, Callable[[bool], None]], None],
                 later: Callable[[int, Callable[[], None]], None],
                 clock: Callable[[], float] = time.time, timer=None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._account = account
        self._marks = marks
        self._on_listed = on_listed
        self._discard = discard
        self._later = later
        self._clock = clock
        self._watcher = SubmissionWatcher(account.writer, account.submissions.entries,
                                          account.client_for, timer=timer, parent=self)
        self._watcher.updated.connect(self._on_updated)
        self._watcher.round_done.connect(self._on_round)
        self.reading = Reading(account, self, later=later, clock=clock)
        self.talk = Talk(account, room=self.reading.room_of, api_for=self.api_for,
                         stored=self._on_stored, reread=self._reread,
                         changed=self.detail_changed.emit, later=later, clock=clock)
        self._started = False
        self._stopped = False
        self._served: Dict[str, views.SubmissionView] = {}
        self._nothing: set = set()
        # sid -> (the receipt's revision and hash, StoredCopy | NotHandedIn):
        # a verdict holds for that revision only (RG-M3-a 6).
        self._decided: Dict[str, Tuple[tuple, object]] = {}
        self._deciding: set = set()
        self._names: Dict[str, Tuple[str, Optional[str]]] = {}
        self._noticed: Dict[str, int] = {}
        # What the lookup brought that is still being opened, by sid: its
        # notice is decided once it is open.
        self._telling: Dict[str, Followed] = {}
        self._tries = 0
        self._reading_views = False
        # The one sentence (untranslated) when the status could not be
        # loaded and the quiet tries are spent; "" otherwise.
        self.problem = ""
        # Why the list of submissions could not be read (translated), or "".
        self.list_problem = ""
        # The list was read once: only then is an empty one empty.
        self.listed = False

    # -- the account's scope -----------------------------------------------------

    def start(self) -> None:
        """At launch with the key on this computer, else when the view
        first opens: the list read once, then the lookup now and every 15
        minutes. Each read of the list or the read state may be a prompt
        of the signer app."""
        if self._started or self._stopped:
            return
        self._started = True
        account = self._account
        account.read_whole(live(account, self._listed))
        account.read_state.load(live(account, lambda _read: self.changed.emit()))
        account.destinations.find(live(account, self._named), live(account, lambda _e: None))

    def stop(self) -> None:
        """Sign-out or another account: the watcher stops, nothing on its
        way answers, and everything learned is forgotten (the room keys
        with it)."""
        self._stopped = True
        self._watcher.stop()
        self.talk.stop()
        self.reading.stop()
        self._served, self._nothing, self._decided = {}, set(), {}
        self._names, self._noticed, self._telling = {}, {}, {}

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def me(self) -> str:
        """The account's pubkey."""
        return self._account.writer

    def view_opened(self) -> None:
        """The Einreichungen view opened: look now, and read the works
        the person is named in."""
        if self._stopped:
            return
        if not self._started:
            self.start()
        else:
            self.refresh()
        self._read_views()

    def refresh(self) -> None:
        """Look now (Erneut versuchen, a run that ended)."""
        if self._stopped:
            return
        if not self._started:
            self.start()                    # nothing read yet: read it now
            return
        self._tries = 0
        if self._watcher.running:
            self._watcher.refresh()
        else:
            self.reload()

    def reload(self) -> None:
        """Erneut laden: the list of submissions read again, then the
        lookup."""
        if not self._stopped:
            self._account.read_whole(live(self._account, self._listed))

    def leaving(self) -> None:
        """The app is being left: what was read is written now (13)."""
        if not self._stopped:
            self._account.read_state.flush(leaving=True)

    def _listed(self, read) -> None:
        self._on_listed(read)
        self.listed = True
        self.list_problem = "" if read.readable else (read.words or "")
        if read.readable:
            if self._watcher.running:
                self._watcher.refresh()
            else:
                self._watcher.start()
        self.changed.emit()

    def _named(self, destinations) -> None:
        self._names = {d.address: (d.name, d.picture) for d in destinations}
        self.changed.emit()

    # -- the lookup ----------------------------------------------------------------

    def _on_round(self, done: Round) -> None:
        self._nothing = (self._nothing - set(done.asked)) | set(done.nothing)
        if done.failures:
            # Asked again quietly first; said once when the tries are spent.
            if self._tries < len(failures.QUIET_RETRY_MS):
                delay = failures.QUIET_RETRY_MS[self._tries]
                self._tries += 1
                self._later(delay, live(self._account, self._watcher.refresh))
            else:
                self.problem = cause_words(done.failures[0].code)
        else:
            self._tries, self.problem = 0, ""
            if self._account.api.has_session:
                self._read_views()                  # no prompt: the session is there
        self.changed.emit()

    def _on_updated(self, learned: Mapping[str, Followed]) -> None:
        account = self._account
        for sid, known in learned.items():
            entry, status = known.entry, known.status
            if isinstance(status, StoredOther):
                self._decide(known)
            if not entry.stored and self._running(sid) is None:
                # A receipt for this sid with its status key (13): the entry
                # says so from now on, after a restart too (12.4).
                account.submissions.mark_stored([entry])
                self._marks.remember(account.writer, entry.d, stored=True)
            if self.reading.add(sid, known.timeline):
                self.detail_changed.emit(sid)
            self._tell(known)
        self.changed.emit()

    def _decide(self, known: Followed) -> None:
        """This revision is stored with another envelope than the entry
        names: its signed action decides (12.4), once per receipt."""
        sid, entry = known.entry.sid, known.entry
        receipt = _receipt_key(known)
        if sid in self._deciding or self._decided.get(sid, ((),))[0] == receipt:
            return
        self._deciding.add(sid)
        account = self._account

        def decided(outcome: object) -> None:
            self._deciding.discard(sid)
            self._decided[sid] = (receipt, outcome)
            if isinstance(outcome, StoredCopy):
                self._marks.remember(account.writer, entry.d, stored=True)
                account.submissions.record(outcome.entry, live(account, lambda _r: None))
            self.changed.emit()

        verify_stored(account.client_for(entry.server), entry, known.item.receipt,
                      live(account, decided),
                      live(account, lambda _error: self._deciding.discard(sid)),
                      writer=account.writer)

    def _tell(self, known: Followed) -> None:
        """A notice for what is new to the writer since they last read
        (plan 7.4): the newest message of someone else, or a result;
        once per ``seq``. When nothing after what they read is news (their
        own steps and messages, who was named), it counts as read: a
        state change is an indicator, not something unread."""
        view, entry = known.item.submission, known.entry
        read_state = self._account.read_state
        seen = read_state.seen(entry.sid)
        upto = view.last_message_seq
        if upto <= max(seen, self._noticed.get(entry.sid, 0)):
            return
        room = secret_of(entry)
        held, opening = self.reading.open_wraps(entry.sid, room, known.timeline)
        if opening:
            self._telling[entry.sid] = known        # decided once it is open
            return
        self._noticed[entry.sid] = upto
        for item in sorted(known.timeline, key=lambda i: -i.seq):
            if not seen < item.seq <= upto:
                continue
            news = self._news_of(item, entry, view, held)
            if news is not None:
                self.news.emit(news)
                return
        if not known.item.more and room is not None:
            read_state.mark(entry.sid, upto)        # written with the next write (13)

    def wraps_opened(self, sid: str) -> None:
        """What was being opened of ``sid``'s conversation is open (on
        the worker thread): its notice, its row and its detail follow."""
        if self._stopped:
            return
        known = self._telling.pop(sid, None)
        if known is not None:
            self._tell(known)
        self.detail_changed.emit(sid)
        self.changed.emit()

    def _news_of(self, item: views.TimelineItem, entry: SubmissionEntry,
                 view: views.SubmissionView,
                 held: Mapping[int, Optional[conversation.Opened]]) -> Optional[News]:
        """What ``item`` is news of for the writer, or None: a result, or
        a message of someone else who is not a stranger."""
        said = dict(sid=entry.sid, d=entry.d, title=view.title,
                    publication=self.publication_name(view.publication))
        if item.event is not None:
            if item.event.name in NEWS_EVENTS and item.event.actor != self._account.writer:
                return News(kind=item.event.name, **said)
            return None
        message = self._message_of(item, view, held)
        if message is None:
            return None
        return News(kind="message", sender=message[0].sender, role=message[1], **said)

    def _message_of(self, item: views.TimelineItem, view: views.SubmissionView,
                    held: Mapping[int, Optional[conversation.Opened]]
                    ) -> Optional[Tuple[conversation.Opened, threads.Role]]:
        """The plain message of someone else in ``item`` with its
        sender's role, or None: not a message, one's own, one that failed
        a check (``held`` is what the conversation held, opened once), or
        a stranger's."""
        opened = held.get(item.seq) if item.wrap is not None else None
        if opened is None:
            return None
        me = self._account.writer
        rumor = opened.rumor
        plain = rumor["kind"] == conversation.MESSAGE_KIND \
            and formats.tag_value(rumor, "edit") is None \
            and formats.tag_value(rumor, "standup") is None
        if not plain or opened.sender == me:
            return None
        role = threads.role_of(opened.sender, writer=view.writer,
                               participants=view.participants,
                               members=self.reading.members_of(view.publication))
        return None if role is threads.Role.STRANGER else (opened, role)

    # -- the works the person is named in --------------------------------------------

    def _read_views(self) -> None:
        account = self._account
        if self._reading_views or self._stopped or not account.api.configured():
            return
        self._reading_views = True

        def answered(result: object) -> None:
            self._reading_views = False
            if isinstance(result, StandupError):
                # The rows of the list stay; said only when the status of
                # none could be loaded either (one sentence, lessons 36).
                if result.code in PASSING and not self._watcher.followed():
                    self.problem = cause_words(result.code)
            else:
                self._served = {view.submission_id: view for view in result}
            self.changed.emit()

        failures.retry_quietly(
            lambda answer: account.api.my_submissions(live(account, answer),
                                                      live(account, answer)),
            settled=lambda result: not (isinstance(result, StandupError)
                                        and result.code in PASSING),
            on_done=answered, later=self._later)

    # -- the rows ----------------------------------------------------------------------

    def _running(self, sid: str):
        """The progress of a run of ``sid`` that goes on, or None."""
        for run in self._account.runs:
            if run.submitting.submission_id == sid and run.progress.phase not in (
                    Phase.DONE, Phase.FAILED, Phase.CANCELLED):
                return run.progress
        return None

    def _followed(self) -> Dict[str, Followed]:
        known = self._watcher.followed()
        for sid, (receipt, outcome) in self._decided.items():
            if sid in known and _receipt_key(known[sid]) == receipt:
                known[sid] = replace(known[sid], status=outcome if isinstance(
                    outcome, NotHandedIn) else outcome.status)
        return known

    def rows(self) -> List[rules.Row]:
        if self._stopped:
            return []
        account = self._account
        running = {}
        for run in account.runs:
            progress = self._running(run.submitting.submission_id)
            if progress is not None:
                running[run.submitting.submission_id] = progress
        rows = rules.rows_of(
            account.submissions.entries(), me=account.writer, followed=self._followed(),
            served=self._served, unknown=self._nothing, running=running,
            refused=lambda entry: self._marks.round_refused(account.writer, entry),
            seen=account.read_state.seen)
        listed = {row.sid for row in rows}
        for run in account.runs:
            sid = run.submitting.submission_id
            if sid in running and sid not in listed:
                # Before its entry is on the list: on its way all the same.
                rows.insert(0, rules.Row(
                    sid=sid, title=run.draft.title, publication=run.choice.publication,
                    kind="article", role="writer", writer=account.writer, d=run.draft.d,
                    situation=rules.Situation.SENDING, updated_at=int(self._clock())))
        return rows

    def row(self, sid: str) -> Optional[rules.Row]:
        return next((row for row in self.rows() if row.sid == sid), None)

    def row_for_work(self, d: str) -> Optional[rules.Row]:
        """The row the pill of the article ``d`` shows: its own turn
        first, then what is running, then the newest."""
        mine = [row for row in self.rows() if row.d == d and row.role == "writer"] if d else []
        return mine[0] if mine else None

    def publication_name(self, address: str) -> str:
        """The publication's name: as its server lists it, else the name
        of its Site."""
        name = self._names.get(address, ("", None))[0]
        if name:
            return name
        try:
            return formats.parse_address(address)[1]
        except formats.FormatError:
            return ""

    def publication_picture(self, address: str) -> Optional[str]:
        return self._names.get(address, ("", None))[1]

    def snippet(self, sid: str) -> Tuple[str, threads.Role, str]:
        """The newest message of someone else in ``sid``'s conversation:
        its sender, who that is (never a stranger) and its text, as far
        as the lookup brought it and the list holds its key;
        ``("", Role.UNKNOWN, "")`` otherwise."""
        known = self._watcher.followed().get(sid)
        room = secret_of(self._account.submissions.entry(sid))
        if known is not None and room is not None:
            held, _opening = self.reading.open_wraps(sid, room, known.timeline)
            for item in sorted(known.timeline, key=lambda i: -i.seq):
                message = self._message_of(item, known.item.submission, held)
                if message is not None:
                    opened, role = message
                    return opened.sender, role, " ".join(opened.rumor["content"].split())[:200]
        return "", threads.Role.UNKNOWN, ""

    # -- a detail (standup_reading.Reading) ----------------------------------------

    def followed(self) -> Dict[str, Followed]:
        """What the lookup knows, by sid."""
        return self._watcher.followed()

    def serve(self, view: views.SubmissionView) -> None:
        """A View the server just gave joins the rows, unless a newer one
        is known."""
        known = self._served.get(view.submission_id)
        if known is None or view.last_seq >= known.last_seq:
            self._served[view.submission_id] = view
        self.changed.emit()

    def api_for(self, sid: str):
        entry = self._account.submissions.entry(sid)
        return self._account.client_for(entry.server) if entry is not None \
            else self._account.api

    def open(self, sid: str) -> None:
        """Show the detail of ``sid``; what is read up to now counts as
        read."""
        if self._stopped:
            return
        # What the server holds nothing of has nothing to read there.
        row = self.row(sid)
        self.reading.open(sid, read=row is None or row.situation not in (
            rules.Situation.UNSENT, rules.Situation.ELSEWHERE, rules.Situation.SENDING))
        self.mark_read(sid)

    def detail(self, sid: str) -> Optional[Detail]:
        return None if self._stopped else self.reading.detail(sid)

    def version(self, sid: str, revision: int,
                on_done: Callable[[Optional[Version]], None]) -> None:
        if self._stopped:
            answer_later(on_done, None)
        else:
            self.reading.version(sid, revision, on_done)

    def cover(self, sid: str) -> Optional[bytes]:
        return self.reading.cover(sid)

    def mark_read(self, sid: str) -> None:
        """What the conversation holds up to now is read (9.3: from
        ``last_message_seq``), written at most every 5 minutes (13)."""
        row = self.row(sid)
        if row is None or row.view is None or self._stopped:
            return
        read_state = self._account.read_state
        if row.view.last_message_seq > read_state.seen(sid):
            read_state.mark(sid, row.view.last_message_seq)
            read_state.flush()
            self.changed.emit()

    # -- what the conversation tells back ----------------------------------------------

    def _on_stored(self, out: Outgoing, seq: int) -> None:
        """The server stored a message or reaction of this app: it joins
        the conversation at its ``seq`` at once (a stored reaction is not
        dimmed, whether or not a refresh follows)."""
        self.reading.add(out.sid, [views.TimelineItem(
            seq=seq, at=int(self._clock()), type="message", wrap=out.wrap, event=None)])
        if out.is_message:
            self._account.read_state.mark(out.sid, seq)
            self._noticed[out.sid] = max(self._noticed.get(out.sid, 0), seq)
        self.detail_changed.emit(out.sid)

    def _reread(self, sid: str, on_done: Callable[[Optional[Dict[str, int]]], None]) -> None:
        """The conversation read again, whole: the gift wrap ids it
        holds with their ``seq``, or None when it could not be read."""
        self.reading.read(sid, lambda ok: on_done(self.reading.wraps(sid) if ok else None))

    # -- actions -----------------------------------------------------------------------

    def withdraw(self, sid: str, on_done: Callable[[Optional[StandupError]], None]) -> None:
        """Zurückziehen: ``on_done(None)`` once the server stored it, or
        with why it did not."""
        account = self._account

        def withdrawn(view: views.SubmissionView) -> None:
            self.serve(view)
            self.detail_changed.emit(sid)
            self._watcher.refresh()
            on_done(None)

        self.api_for(sid).withdraw(sid, live(account, withdrawn), live(account, on_done))

    def discard(self, sid: str, on_done: Callable[[bool], None]) -> None:
        """Verwerfen (13): a keyed tombstone for the entry the server has
        no receipt for."""
        entry = self._account.submissions.entry(sid)
        if entry is None:
            answer_later(on_done, False)
            return

        def discarded(stored: bool) -> None:
            on_done(stored)
            self.changed.emit()

        self._discard(entry, live(self._account, discarded))



def _receipt_key(known: Followed) -> tuple:
    """The revision a receipt speaks of: its number and hash."""
    receipt = known.item.receipt
    return (receipt.revision, receipt.revision_hash)


__all__ = ["Detail", "Following", "NEWS_EVENTS", "News", "Version"]
