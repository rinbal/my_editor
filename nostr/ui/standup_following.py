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

A detail (:meth:`Following.open`) shows at once what the lookup knows
and reads the submission with the session: the member list (who may
decide, contract 10), the latest version, verified before it is opened
(``opening.py``). A writer's room key is the list's ``s_sub``; a
contributor's wrap is opened by their signer once and kept in their
list (13). Messages and reactions on their way are
:mod:`.standup_talk`'s.

Everything answers through :func:`~.standup_account.live`: after
:meth:`Following.stop` nothing writes a row, the list or the read state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, Signal

from .. import failures
from ..later import answer_later
from ..standup import conversation, crypto, formats, opening, views
from ..standup import following as rules
from ..standup import thread as threads
from ..standup.entries import SubmissionEntry
from ..standup.errors import PASSING, StandupError, cause_words
from ..standup.submitting import Phase
from ..standup.watcher import (
    AT_THE_END, Followed, NotHandedIn, Round, StoredCopy, StoredOther, SubmissionWatcher,
    read_submission, verify_stored,
)
from .standup_account import live
from .standup_talk import Outgoing, Talk

# What an article's notice is about (plan 7.4): a message, or a result.
NEWS_EVENTS = ("request_changes", "accept", "decline", "published", "correction")


@dataclass(frozen=True)
class News:
    """Something new for the writer of the work ``d``: ``kind`` is
    ``"message"`` or one of :data:`NEWS_EVENTS`."""

    sid: str
    d: str
    kind: str
    title: str
    publication: str            # its name


@dataclass(frozen=True)
class Version:
    """A version opened from its verified envelope (12.4)."""

    revision: int
    work: Mapping
    change_summary: str = ""
    rights: bool = False


@dataclass
class _Open:
    """What is known of one submission's detail."""

    items: Dict[int, views.TimelineItem] = field(default_factory=dict)
    members: Optional[tuple] = None
    role: str = ""
    view: Optional[views.SubmissionView] = None
    room: Optional[bytes] = field(default=None, repr=False)
    versions: Dict[int, Version] = field(default_factory=dict)
    loading: bool = False
    problem: str = ""               # the one sentence, when nothing could be read
    not_confirmed: bool = False     # the latest version did not verify: not opened


@dataclass(frozen=True)
class Detail:
    """What the detail shows of one submission."""

    row: rules.Row
    publication: str
    decision: Optional[rules.Decision]
    thread: Tuple[threads.ThreadItem, ...]
    history: Tuple[threads.Caption, ...]
    opened: Tuple[conversation.Opened, ...]
    outgoing: Tuple[Outgoing, ...]
    members: Optional[tuple]
    latest: Optional[Version]
    loading: bool
    problem: str
    can_write: bool
    not_confirmed: bool = False


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
        self.talk = Talk(account, room=self._room_of, api_for=self._api_for,
                         stored=self._on_stored, reread=self._reread,
                         changed=self.detail_changed.emit, later=later, clock=clock)
        self._started = False
        self._stopped = False
        self._served: Dict[str, views.SubmissionView] = {}
        self._nothing: set = set()
        self._decided: Dict[str, object] = {}       # sid -> StoredCopy | NotHandedIn
        self._deciding: set = set()
        self._names: Dict[str, Tuple[str, Optional[str]]] = {}
        self._details: Dict[str, _Open] = {}
        self._noticed: Dict[str, int] = {}
        self._tries = 0
        self._reading_views = False
        # The one sentence (untranslated) when the status could not be
        # loaded and the quiet tries are spent; "" otherwise.
        self.problem = ""
        # Why the list of submissions could not be read (translated), or "".
        self.list_problem = ""

    # -- the account's scope -----------------------------------------------------

    def start(self) -> None:
        """At launch, and when the view first opens: the list read once,
        then the lookup now and every 15 minutes."""
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
        self._served, self._nothing, self._decided, self._details = {}, set(), {}, {}
        self._names, self._noticed = {}, {}

    @property
    def stopped(self) -> bool:
        return self._stopped

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

    def _listed(self, read) -> None:
        self._on_listed(read)
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
            state = self._details.get(sid)
            if state is not None:
                state.items.update({item.seq: item for item in known.timeline})
                self.detail_changed.emit(sid)
            self._tell(known)
        self.changed.emit()

    def _decide(self, known: Followed) -> None:
        """This revision is stored with another envelope than the entry
        names: its signed action decides (12.4), once per receipt."""
        sid, entry = known.entry.sid, known.entry
        if sid in self._deciding or sid in self._decided:
            return
        self._deciding.add(sid)
        account = self._account

        def decided(outcome: object) -> None:
            self._deciding.discard(sid)
            self._decided[sid] = outcome
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
        once per ``seq``."""
        view, entry = known.item.submission, known.entry
        seen = self._account.read_state.seen(entry.sid)
        upto = view.last_message_seq
        if upto <= max(seen, self._noticed.get(entry.sid, 0)):
            return
        self._noticed[entry.sid] = upto
        room = self._secret_of(entry)
        for item in sorted(known.timeline, key=lambda i: -i.seq):
            if not seen < item.seq <= upto:
                continue
            kind = self._news_kind(item, entry.sid, room)
            if kind:
                self.news.emit(News(entry.sid, entry.d, kind, view.title,
                                    self.publication_name(view.publication)))
                return

    def _news_kind(self, item: views.TimelineItem, sid: str, room: Optional[bytes]) -> str:
        me = self._account.writer
        if item.event is not None:
            return item.event.name if item.event.name in NEWS_EVENTS \
                and item.event.actor != me else ""
        if room is None:
            return ""
        try:
            opened = conversation.open_item(item.wrap, room, sid, seq=item.seq)
        except conversation.NotShown:
            return ""
        rumor = opened.rumor
        plain = rumor["kind"] == conversation.MESSAGE_KIND \
            and formats.tag_value(rumor, "edit") is None \
            and formats.tag_value(rumor, "standup") is None
        return "message" if plain and opened.sender != me else ""

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
        for sid, outcome in self._decided.items():
            if sid in known:
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

    # -- a detail ----------------------------------------------------------------------

    def _api_for(self, sid: str):
        entry = self._account.submissions.entry(sid)
        return self._account.client_for(entry.server) if entry is not None \
            else self._account.api

    def _secret_of(self, entry: Optional[SubmissionEntry]) -> Optional[bytes]:
        return bytes.fromhex(entry.s_sub) if entry is not None and entry.s_sub else None

    def _room_of(self, sid: str) -> Optional[Tuple[bytes, str]]:
        state = self._details.get(sid)
        secret = state.room if state is not None else None
        return (secret, crypto.pubkey_of(secret)) if secret is not None else None

    def open(self, sid: str) -> None:
        """Show the detail of ``sid``: what the lookup knows at once, then
        the submission read with the session; what is read up to now
        counts as read."""
        if self._stopped:
            return
        state = self._details.setdefault(sid, _Open())
        known = self._watcher.followed().get(sid)
        if known is not None:
            state.items.update({item.seq: item for item in known.timeline})
        if state.room is None:
            state.room = self._secret_of(self._account.submissions.entry(sid))
        self._read(sid)
        self.mark_read(sid)

    def _read(self, sid: str, on_read: Optional[Callable[[bool], None]] = None) -> None:
        state = self._details.get(sid)
        if state is None:
            return
        account, api = self._account, self._api_for(sid)
        state.loading = True

        def answered(result: object) -> None:
            state.loading = False
            ok = not isinstance(result, StandupError)
            if ok:
                state.problem = ""
                self._take(sid, state, result)
            elif not state.items and getattr(self.row(sid), "view", None) is None:
                state.problem = cause_words(result.code)    # nothing to show: said once
            if on_read is not None:
                on_read(ok)
            self.detail_changed.emit(sid)

        failures.retry_quietly(
            lambda answer: read_submission(api, sid, live(account, answer),
                                           live(account, answer)),
            settled=lambda result: not (isinstance(result, StandupError)
                                        and result.code in PASSING),
            on_done=answered, later=self._later)
        self.detail_changed.emit(sid)

    def _take(self, sid: str, state: _Open, detail) -> None:
        """A read of the submission: its view, its members, its items,
        and its latest version, verified before anything is opened."""
        state.view, state.role, state.members = detail.submission, detail.my_role, detail.members
        state.items.update({item.seq: item for item in detail.timeline})
        known = self._served.get(sid)
        if known is None or detail.submission.last_seq >= known.last_seq:
            self._served[sid] = detail.submission
        record = next((r for r in detail.revisions
                       if r.revision == detail.submission.revision), None)
        if record is not None:
            self._open_version(sid, state, detail.submission, record)
        self.mark_read(sid)
        self.changed.emit()

    def _open_version(self, sid: str, state: _Open, view: views.SubmissionView,
                      record: views.RevisionRecord,
                      on_done: Optional[Callable[[Optional[Version]], None]] = None) -> None:
        """``record`` verified against its signed action (12.4), the room
        key taken (the writer's from their list; a contributor's wrap
        opened by their signer, once, and kept), its envelope opened."""
        done = on_done or (lambda _version: None)
        account = self._account
        try:
            verified = opening.verify_revision(
                view, record, redaktion=rules.deciders_of(state.members or ()))
        except opening.NotConfirmed:
            state.not_confirmed = state.not_confirmed or record.revision == view.revision
            done(None)
            return

        def with_key(secret: bytes) -> None:
            if state.room is None:
                state.room = secret
                self._keep_key(view, record, verified, secret)
            version = None
            if verified.envelope_sha256 is not None:
                try:
                    opened = opening.open_envelope(verified, record.envelope, secret)
                except opening.NotConfirmed:
                    state.not_confirmed = True
                else:
                    account.media_keys.add(opened)
                    revision = opened.revision
                    version = state.versions[record.revision] = Version(
                        revision=record.revision, work=revision["work"],
                        change_summary=revision.get("change_summary") or "",
                        rights=bool((revision.get("rights") or {}).get("confirmed")))
            done(version)
            self.detail_changed.emit(sid)

        if state.room is not None:
            if crypto.pubkey_of(state.room) != verified.p_sub:
                state.not_confirmed = True          # not this submission's key
                done(None)
                return
            with_key(state.room)
            return
        opening.open_submission_key(
            account.signer, verified, account.writer, live(account, with_key),
            live(account, lambda _error: done(None)),
            served=record.wraps.get(account.writer))

    def _keep_key(self, view, record, verified, secret: bytes) -> None:
        """A contributor's opened key goes into their own list (13)."""
        account = self._account
        if view.writer == account.writer or account.submissions.entry(view.submission_id):
            return
        entry = SubmissionEntry(
            sid=view.submission_id, publication=view.publication, server=account.api.server,
            role="contributor", s_sub=secret.hex(), status_key=None, title=verified.title,
            kind=verified.kind, d="", round=view.round, revision=record.revision,
            revision_hash=record.revision_hash, envelope_sha256=record.envelope_sha256,
            wraps_sha256=crypto.wraps_sha256(verified.wraps)
            if record.envelope_sha256 is not None else None,
            updated_at=int(self._clock()))
        account.submissions.record(entry, live(account, lambda _result: None))

    def version(self, sid: str, revision: int,
                on_done: Callable[[Optional[Version]], None]) -> None:
        """The version ``revision`` of ``sid``, opened from its verified
        envelope; None when it could not be read or did not verify (it
        is then not opened, 12.4). Read once."""
        state = self._details.get(sid)
        view = state.view if state is not None else None
        if state is None or view is None or self._stopped:
            answer_later(on_done, None)
            return
        if revision in state.versions:
            answer_later(on_done, state.versions[revision])
            return
        account = self._account

        def read(detail) -> None:
            record = next((r for r in detail.revisions if r.revision == revision), None)
            if record is None:
                on_done(None)
            else:
                self._open_version(sid, state, view, record, on_done)

        self._api_for(sid).submission(sid, live(account, read),
                                      live(account, lambda _error: on_done(None)),
                                      since=AT_THE_END, revision=revision)

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

    def detail(self, sid: str, *, name_of: Callable[[str], str]) -> Optional[Detail]:
        row, state = self.row(sid), self._details.get(sid)
        if row is None or state is None:
            return None
        items = [state.items[seq] for seq in sorted(state.items)]
        deciders = rules.deciders_of(state.members) if state.members is not None else None
        opened = conversation.open_items(items, state.room, sid).opened \
            if state.room is not None else ()
        view = row.view
        who = dict(sid=sid, writer=row.writer,
                   participants=view.participants if view is not None else (),
                   deciders=deciders)
        return Detail(
            row=row, publication=self.publication_name(row.publication),
            decision=rules.newest_decision(items, sid, deciders),
            thread=tuple(threads.build_thread(items, opened, me=self._account.writer, **who)),
            history=tuple(threads.history(items, **who)), opened=tuple(opened),
            outgoing=tuple(self.talk.outgoing(sid)), members=state.members,
            latest=state.versions.get(view.revision) if view is not None else None,
            loading=state.loading, problem=state.problem,
            can_write=state.room is not None and row.situation is not rules.Situation.WITHDRAWN,
            not_confirmed=state.not_confirmed)

    # -- what the conversation tells back ----------------------------------------------

    def _on_stored(self, out: Outgoing, seq: int) -> None:
        """The server stored a message or reaction of this app: it joins
        the conversation at its ``seq`` at once (a stored reaction is not
        dimmed, whether or not a refresh follows)."""
        state = self._details.get(out.sid)
        if state is not None:
            state.items[seq] = views.TimelineItem(seq=seq, at=int(self._clock()),
                                                  type="message", wrap=out.wrap, event=None)
        if out.is_message:
            self._account.read_state.mark(out.sid, seq)
            self._noticed[out.sid] = max(self._noticed.get(out.sid, 0), seq)
        self.detail_changed.emit(out.sid)

    def _reread(self, sid: str, on_done: Callable[[Optional[Dict[str, int]]], None]) -> None:
        """The conversation read again, whole: the gift wrap ids it
        holds with their ``seq``, or None when it could not be read."""
        def read(ok: bool) -> None:
            state = self._details.get(sid)
            if not ok or state is None:
                on_done(None)
            else:
                on_done({item.wrap["id"]: item.seq for item in state.items.values()
                         if item.wrap is not None and isinstance(item.wrap.get("id"), str)})

        if self._details.get(sid) is None:
            answer_later(on_done, None)
        else:
            self._read(sid, read)

    # -- actions -----------------------------------------------------------------------

    def withdraw(self, sid: str, on_done: Callable[[Optional[StandupError]], None]) -> None:
        """Zurückziehen: ``on_done(None)`` once the server stored it, or
        with why it did not."""
        account = self._account

        def withdrawn(view: views.SubmissionView) -> None:
            self._served[sid] = view
            self.changed.emit()
            self.detail_changed.emit(sid)
            self._watcher.refresh()
            on_done(None)

        self._api_for(sid).withdraw(sid, live(account, withdrawn), live(account, on_done))

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


__all__ = ["Detail", "Following", "NEWS_EVENTS", "News", "Version"]
