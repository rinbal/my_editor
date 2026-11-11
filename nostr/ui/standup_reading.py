# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading one submission for its detail (plan 10.4; editorial contract
10, 12.4, 13). It was part of :mod:`.standup_following`, whose
:class:`~.standup_following.Following` owns one :class:`Reading` per
account.

A detail shows at once what the lookup knows and reads the submission
with the session: the member list (who may decide, contract 10) and the
latest version. Nothing is opened on the strength of a View
(``opening.py``): a version is verified against its stored signed
action first, and one that does not verify is not opened and says so. A
writer's room key is the ``s_sub`` of their own list; a contributor's
wrap is opened by their signer once and kept in their list (13). The
connection, or the server's own trouble, is asked again quietly; only
when nothing at all could be read does the detail say so, once (lessons
36).

Everything answers through :func:`~.standup_account.live`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional, Tuple

from .. import failures
from ..later import answer_later
from ..standup import conversation, crypto, formats, opening, views
from ..standup import following as rules
from ..standup import thread as threads
from ..standup.entries import SubmissionEntry
from ..standup.errors import PASSING, Code, StandupError, cause_words
from ..standup.watcher import AT_THE_END, read_submission
from .standup_account import live
from .standup_talk import Outgoing


@dataclass(frozen=True)
class Version:
    """A version opened from its verified envelope (12.4)."""

    revision: int
    work: Mapping
    change_summary: str = ""
    rights: bool = False
    # Its small cover, ``{"mime", "data"}`` as the revision holds it, or None.
    thumb: Optional[Mapping] = None


@dataclass
class _Open:
    """What is known of one submission's detail."""

    items: Dict[int, views.TimelineItem] = field(default_factory=dict)
    members: Optional[tuple] = None
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
    versions: Mapping[int, Version]
    loading: bool
    problem: str
    can_write: bool
    not_confirmed: bool = False
    # The submission was read whole, with its member list (contract 10).
    complete: bool = False


def secret_of(entry: Optional[SubmissionEntry]) -> Optional[bytes]:
    """The room key the list holds for an entry, or None."""
    return bytes.fromhex(entry.s_sub) if entry is not None and entry.s_sub else None


class Reading:
    """See the module docstring. ``owner`` is the account's Following:
    its rows, what the lookup knows (``followed``), its signals, and
    where a read View goes (``serve``)."""

    def __init__(self, account, owner, *, later: Callable[[int, Callable[[], None]], None],
                 clock: Callable[[], float]) -> None:
        self._account = account
        self._owner = owner
        self._later = later
        self._clock = clock
        self._details: Dict[str, _Open] = {}
        # The active members of each publication's Redaktion, as its
        # server last listed them with a submission (thread.role_of).
        self._members: Dict[str, Tuple[str, ...]] = {}

    def stop(self) -> None:
        """The room keys and what was opened go with the account."""
        self._details = {}
        self._members = {}

    def members_of(self, publication: str) -> Optional[Tuple[str, ...]]:
        """The active members of ``publication``'s Redaktion, or None
        while no submission to it was read."""
        return self._members.get(publication)

    def room_of(self, sid: str) -> Optional[Tuple[bytes, str]]:
        """The room of ``sid``, its secret and its pubkey, once known."""
        state = self._details.get(sid)
        secret = state.room if state is not None else None
        return (secret, crypto.pubkey_of(secret)) if secret is not None else None

    def add(self, sid: str, items) -> bool:
        """``items`` join what is known of ``sid``, when its detail was
        opened; whether it was."""
        state = self._details.get(sid)
        if state is not None:
            state.items.update({item.seq: item for item in items})
        return state is not None

    def open(self, sid: str, *, read: bool = True) -> None:
        """What the lookup knows at once, then (``read``) the submission
        read with the session."""
        state = self._details.setdefault(sid, _Open())
        known = self._owner.followed().get(sid)
        if known is not None:
            self.add(sid, known.timeline)
        if state.room is None:
            state.room = secret_of(self._account.submissions.entry(sid))
        if read:
            self.read(sid)

    def read(self, sid: str, on_read: Optional[Callable[[bool], None]] = None) -> None:
        """The submission read again, whole; ``on_read(ok)`` once."""
        state = self._details.get(sid)
        if state is None:
            if on_read is not None:
                answer_later(on_read, False)
            return
        account, owner, api = self._account, self._owner, self._owner.api_for(sid)
        state.loading = True

        def answered(result: object) -> None:
            state.loading = False
            ok = not isinstance(result, StandupError)
            if ok:
                state.problem = ""
                self._take(sid, state, result)
            elif result.code != Code.NOT_FOUND and not state.items \
                    and getattr(owner.row(sid), "view", None) is None:
                state.problem = cause_words(result.code)    # nothing to show: said once
            if on_read is not None:
                on_read(ok)
            owner.detail_changed.emit(sid)

        failures.retry_quietly(
            lambda answer: read_submission(api, sid, live(account, answer),
                                           live(account, answer)),
            settled=lambda result: not (isinstance(result, StandupError)
                                        and result.code in PASSING),
            on_done=answered, later=self._later)
        owner.detail_changed.emit(sid)

    def wraps(self, sid: str) -> Optional[Dict[str, int]]:
        """The gift wrap ids the conversation of ``sid`` holds, with
        their ``seq``; None when its detail is not open."""
        state = self._details.get(sid)
        if state is None:
            return None
        return {item.wrap["id"]: item.seq for item in state.items.values()
                if item.wrap is not None and isinstance(item.wrap.get("id"), str)}

    def _take(self, sid: str, state: _Open, detail) -> None:
        """A read of the submission: its view, its members, its items,
        and its latest version, verified before anything is opened."""
        state.view, state.members = detail.submission, detail.members
        self._members[detail.submission.publication] = rules.members_of(detail.members)
        self.add(sid, detail.timeline)
        self._owner.serve(detail.submission)
        record = next((r for r in detail.revisions
                       if r.revision == detail.submission.revision), None)
        if record is not None:
            self._open_version(sid, state, detail.submission, record)
        self._owner.mark_read(sid)

    def _open_version(self, sid: str, state: _Open, view: views.SubmissionView,
                      record: views.RevisionRecord,
                      on_done: Optional[Callable[[Optional[Version]], None]] = None) -> None:
        """``record`` verified against its signed action (12.4), the room
        key taken (the writer's from their list; a contributor's wrap
        opened by their signer, once, and kept), its envelope opened."""
        done = on_done or (lambda _version: None)
        account = self._account
        latest = record.revision == view.revision
        try:
            verified = opening.verify_revision(
                view, record, redaktion=rules.deciders_of(state.members or ()))
        except opening.NotConfirmed:
            if latest:
                state.not_confirmed = True
            done(None)
            return

        def with_key(secret: bytes) -> None:
            if state.room is None:
                state.room = secret
                self._keep_key(view, record, verified, secret)
            version, confirmed = None, True
            if verified.envelope_sha256 is not None:
                try:
                    opened = opening.open_envelope(verified, record.envelope, secret)
                except opening.NotConfirmed:
                    confirmed = False
                else:
                    account.media_keys.add(opened)
                    revision = opened.revision
                    version = state.versions[record.revision] = Version(
                        revision=record.revision, work=revision["work"],
                        change_summary=revision.get("change_summary") or "",
                        rights=bool((revision.get("rights") or {}).get("confirmed")),
                        thumb=revision.get("thumb"))
            if latest:
                state.not_confirmed = not confirmed
            done(version)
            self._owner.detail_changed.emit(sid)

        if state.room is not None:
            if crypto.pubkey_of(state.room) != verified.p_sub:
                if latest:
                    state.not_confirmed = True      # not this submission's key
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
        if state is None or view is None:
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

        self._owner.api_for(sid).submission(
            sid, live(account, read), live(account, lambda _error: on_done(None)),
            since=AT_THE_END, revision=revision)

    def latest(self, sid: str) -> Optional[Version]:
        """The newest version of ``sid`` opened here, or None."""
        state = self._details.get(sid)
        versions = state.versions if state is not None else {}
        return versions[max(versions)] if versions else None

    def cover(self, sid: str) -> Optional[bytes]:
        """The small cover of ``sid``: the ``thumb`` of its newest version
        opened here, or None (the row then shows the kind's symbol)."""
        version = self.latest(sid)
        thumb = version.thumb if version is not None else None
        if isinstance(thumb, Mapping) and isinstance(thumb.get("data"), str):
            try:
                return formats.b64decode(thumb["data"])
            except formats.FormatError:
                return None
        return None

    def detail(self, sid: str) -> Optional[Detail]:
        owner = self._owner
        row, state = owner.row(sid), self._details.get(sid)
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
        members = rules.members_of(state.members) if state.members is not None else None
        return Detail(
            row=row, publication=owner.publication_name(row.publication),
            decision=rules.newest_decision(items, sid, deciders),
            thread=tuple(threads.build_thread(items, opened, me=self._account.writer,
                                              members=members, **who)),
            history=tuple(threads.history(items, **who)), opened=tuple(opened),
            outgoing=tuple(owner.talk.outgoing(sid)), members=state.members,
            latest=state.versions.get(view.revision) if view is not None else None,
            versions=dict(state.versions), loading=state.loading, problem=state.problem,
            # Only where the server holds the submission as this person's
            # and its latest version verified: the room key is its own then.
            can_write=state.room is not None and row.confirmed and not state.not_confirmed
            and row.situation is not rules.Situation.WITHDRAWN,
            not_confirmed=state.not_confirmed, complete=state.members is not None)


__all__ = ["Detail", "Reading", "Version", "secret_of"]
