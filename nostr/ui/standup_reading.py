# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading one submission for its detail (plan 10.4; editorial contract
10, 12.4, 13). It was part of :mod:`.standup_following`, whose
:class:`~.standup_following.Following` owns one :class:`Reading` per
account.

A detail shows at once what the lookup knows and reads the submission
with the session: the member list (who may decide, contract 10) and the
latest version. Each gift wrap of a conversation is opened once and kept
(:meth:`Reading.open_wraps`; RG-M3-a 4): two signatures and two key
agreements each, so a handful is opened at once and more on the worker
thread; each stored signed action is checked once
(``following.SignedChecks``). A refresh opens only what is new. Nothing is opened on the strength of a View
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

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import failures
from ..imports import workers
from ..later import answer_later
from ..standup import confirmations, conversation, crypto, formats, opening, publishing, views
from ..standup.publishing import Accepted
from ..standup import following as rules
from ..standup import thread as threads
from ..standup.entries import SubmissionEntry
from ..standup.former import Formers
from ..standup.errors import PASSING, Code, StandupError, cause_words
from ..standup.watcher import AT_THE_END, read_submission
from .standup_account import live
from .standup_talk import Outgoing

_log = logging.getLogger(__name__)

# Opening a gift wrap costs about a millisecond (9.2: two signatures, two
# key agreements): up to this many are opened at once, more on the
# worker thread.
OPEN_HERE_UP_TO = 16


@dataclass(frozen=True)
class Version:
    """A version opened from its verified envelope (12.4)."""

    revision: int
    work: Mapping
    change_summary: str = ""
    rights: bool = False
    # Its small cover, ``{"mime", "data"}`` as the revision holds it, or None.
    thumb: Optional[Mapping] = None
    # What publishing, confirming and a correction rest on: the opened
    # revision (``opening.OpenedRevision``) and its verified hash.
    opened: Optional[object] = field(default=None, repr=False, compare=False)
    revision_hash: str = ""


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
    formers: Optional[Formers] = None   # people who left, and the revisions' epochs (10)


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
    section: str = ""               # from a placed report that counts (contract 10)


@dataclass
class _Opening:
    """The conversation of one submission, opened with one room key: by
    ``seq``, the wrap's id and what it held (None: it failed a check of
    9.2 and is not shown); ``pending``, by ``seq``, the wraps being opened
    now (their ids)."""

    room: bytes = field(repr=False)
    done: Dict[int, Tuple[object, Optional[conversation.Opened]]] = field(default_factory=dict)
    pending: Dict[int, object] = field(default_factory=dict)


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
        # What each conversation held, by (sid, room pubkey), and what the
        # stored signed actions verified to: each worked out once.
        self._openings: Dict[Tuple[str, str], _Opening] = {}
        self.checks = rules.SignedChecks()
        self._generation = 0

    def stop(self) -> None:
        """The room keys and what was opened go with the account, and
        nothing still being opened is kept."""
        self._generation += 1
        self._details = {}
        self._members = {}
        self._openings = {}
        self.checks = rules.SignedChecks()

    def open_wraps(self, sid: str, room: Optional[bytes],
                   items: Sequence[views.TimelineItem]
                   ) -> Tuple[Dict[int, Optional[conversation.Opened]], bool]:
        """What the conversation items of ``sid`` held, by ``seq``, opened
        with ``room`` once each (contract 9.2; internal notes are never
        opened here), and whether some are still being opened: the owner
        then hears ``wraps_opened(sid)`` once they are."""
        if room is None:
            return {}, False
        opening = self._openings.setdefault((sid, crypto.pubkey_of(room)), _Opening(room=room))
        new = [(item.seq, item.wrap) for item in items
               if item.wrap is not None and item.channel == "conversation"
               and item.seq not in opening.pending
               and (item.seq not in opening.done
                    or opening.done[item.seq][0] != item.wrap.get("id"))]
        if new:
            self._open(sid, opening, new)
        return {seq: opened for seq, (_id, opened) in opening.done.items()}, bool(opening.pending)

    def _open(self, sid: str, opening: _Opening, new: List[tuple]) -> None:
        room, generation = opening.room, self._generation
        for seq, wrap in new:
            opening.pending[seq] = wrap.get("id")

        def work() -> List[tuple]:
            out = []
            for seq, wrap in new:
                try:
                    out.append((seq, conversation.open_item(wrap, room, sid, seq=seq)))
                except conversation.NotShown:
                    out.append((seq, None))         # not shown (9.2)
            return out

        def done(results: List[tuple]) -> None:
            if generation != self._generation:
                return                              # the account was let go meanwhile
            for seq, opened in results:
                opening.done[seq] = (opening.pending.pop(seq, None), opened)
            if not opening.pending and len(new) > OPEN_HERE_UP_TO:
                self._owner.wraps_opened(sid)

        def failed(error: BaseException) -> None:
            # A fault of this app, not of the wrap: logged, and not shown.
            _log.warning("opening a conversation failed: %s", type(error).__name__)
            done([(seq, None) for seq, _wrap in new])

        if len(new) > OPEN_HERE_UP_TO:
            workers.run_blocking(work, done, failed, parent=self._owner)
        else:
            done(work())

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
        opened; whether anything new joined (only then is it drawn again)."""
        state = self._details.get(sid)
        if state is None:
            return False
        new = {item.seq: item for item in items if state.items.get(item.seq) != item}
        state.items.update(new)
        return bool(new)

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
        self._formers(state, detail)
        self._members[detail.submission.publication] = rules.members_of(detail.members)
        self.add(sid, detail.timeline)
        self._owner.serve(detail.submission)
        record = next((r for r in detail.revisions
                       if r.revision == detail.submission.revision), None)
        taken = detail.address_taken      # another work at the writer's link name (12.4)

        def opened(version: Optional[Version]) -> None:
            # Published on another device: public, known from the relays.
            self._owner.unlisted.look(sid, version, taken=taken)

        if record is not None:
            self._open_version(sid, state, detail.submission, record, opened)
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
        correction = record.signed is not None and views.signed_path(
            record.signed) == views.action_path("correction", sid)
        writer = self._writer_revision(state, record.revision)
        if correction and writer is None and record.revision > 1:
            # A correction takes its p_sub, kind and title from the writer's
            # verified revision before it: that one is opened first (M-4).
            def walked(before: Optional[Version]) -> None:
                if before is None or self._writer_revision(state, record.revision) is None:
                    if latest:
                        state.not_confirmed = True
                    done(None)
                    return
                self._open_version(sid, state, view, record, on_done)

            self.version(sid, record.revision - 1, walked)
            return
        try:
            verified = opening.verify_revision(
                view, record, redaktion=rules.deciders_of(state.members or ()),
                writer_revision=writer if correction else None, formers=state.formers)
        except opening.NotConfirmed:
            if latest:
                state.not_confirmed = True
            done(None)
            return
        if state.formers is not None:
            state.formers.saw(record.revision, verified.epoch)

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
                        thumb=revision.get("thumb"), opened=opened,
                        revision_hash=verified.revision_hash)
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

    def _formers(self, state: _Open, detail) -> None:
        """The people who left that a read served, joined by pubkey
        (contract 10: a later page needs only the new ones). A decision
        that does not verify at the current revision's epoch has its own
        revision read (``?revision=n``), once, and is checked again."""
        if state.formers is None:
            sid, generation = detail.submission.submission_id, self._generation

            def want(revision: int) -> None:
                def read() -> None:
                    if generation == self._generation and self._details.get(sid) is state:
                        self.version(sid, revision,
                                     lambda _v: self._owner.detail_changed.emit(sid))
                self._later(0, read)

            state.formers = Formers(detail.submission.publication, want)
        state.formers.join(detail.former)

    @staticmethod
    def _writer_revision(state: _Open, before: int):
        """The writer's newest verified revision before ``before`` opened
        here (not a correction), or None."""
        for number in sorted((n for n in state.versions if n < before), reverse=True):
            opened = state.versions[number].opened
            if opened is not None and not opened.verified.correction:
                return opened.verified
        return None

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
            self._formers(state, detail)
            record = next((r for r in detail.revisions if r.revision == revision), None)
            if record is None:
                on_done(None)
            else:
                self._open_version(sid, state, view, record, on_done)

        self._owner.api_for(sid).submission(
            sid, live(account, read), live(account, lambda _error: on_done(None)),
            since=AT_THE_END, revision=revision)

    def accepted(self, sid: str) -> Optional["Accepted"]:
        """The verified newest accept of ``sid`` (``publishing.verified_accept``),
        once its member list is known; None otherwise."""
        state = self._details.get(sid)
        if state is None or state.members is None:
            return None
        items = [state.items[seq] for seq in sorted(state.items)]
        return publishing.verified_accept(items, sid, rules.deciders_of(state.members),
                                          self.checks, state.formers)

    def signed(self, sid: str) -> rules.Signed:
        """What the creators' own verified confirmations of ``sid`` say
        (``confirmations.signed_of``; F5 M1), once its detail was read;
        :data:`rules.UNREAD` before that. Read from the detail alone (the
        rows ask it while they are made)."""
        state = self._details.get(sid)
        if state is None or state.view is None:
            return rules.UNREAD
        return confirmations.signed_of(
            state.view, [state.items[seq] for seq in sorted(state.items)],
            verified={n: v.opened.verified for n, v in state.versions.items()
                      if v.opened is not None},
            accepted=self.accepted(sid), checks=self.checks)

    def read_sids(self) -> Tuple[str, ...]:
        """The submissions whose detail was read in this run."""
        return tuple(self._details)

    def published(self, sid: str) -> Optional[publishing.Published]:
        """The published work whose own event proves its address, whoever
        reported it (``publishing.published_work``, contract 10), once a
        version was verified here; None otherwise."""
        state, row = self._details.get(sid), self._owner.row(sid)
        view = row.view if row is not None else None
        coordinate = next((v.opened.verified.coordinate_hash for v in (
            state.versions.values() if state is not None else ())
            if v.opened is not None and v.opened.verified.coordinate_hash), "")
        if view is None or not coordinate:
            return None
        items = [state.items[seq] for seq in sorted(state.items)]
        accepted = self.accepted(sid)
        return publishing.published_work(
            items, sid, writer=view.writer, coordinate_hash=coordinate,
            publication=view.publication, checks=self.checks,
            accepted=accepted.revision if accepted is not None else None,
            corrections={v.revision_hash: n for n, v in state.versions.items()
                         if v.opened is not None and v.opened.verified.correction})

    def published_event(self, sid: str) -> Optional[dict]:
        """The writer's own work as it stands in public, for a correction
        they take over to replace at its address; None otherwise (also
        when the publication published it: then it republishes)."""
        published, row = self.published(sid), self._owner.row(sid)
        return published.event if published is not None and row is not None \
            and published.event.get("pubkey") == row.writer else None

    def section(self, sid: str) -> str:
        """The section of the newest ``placed`` report that counts for the
        proven address (``publishing.section_of``), or ``""``."""
        published, state = self.published(sid), self._details.get(sid)
        if published is None or state is None or state.view is None:
            return ""
        items = [state.items[seq] for seq in sorted(state.items)]
        return publishing.section_of(items, sid, published.address, state.view.publication,
                                     self.checks) or ""

    def accepted_split(self, sid: str) -> Optional[Tuple[Tuple[str, int, str], ...]]:
        """The split to confirm (9.4): ``(pubkey, weight, role)`` of the
        ``split-proposal`` the verified accept names, for the revision it
        accepted; None when it names none, or it is not here (yet)."""
        state, row = self._details.get(sid), self._owner.row(sid)
        accepted = self.accepted(sid)
        if state is None or row is None or row.view is None or accepted is None \
                or not accepted.split_rumor_id:
            return None
        items = [state.items[seq] for seq in sorted(state.items)]
        held, _opening = self.open_wraps(sid, state.room, items)
        opened = [held[item.seq] for item in items if held.get(item.seq) is not None]
        # Who decided counted then, also one who left since (10), and so
        # does a proposal of one whose served Former proves they could
        # decide when they made it (RG-M4-a 7, RG-M4-c 6).
        deciders = tuple({*rules.deciders_of(state.members), accepted.actor})
        decision = next(item.event.signed for item in items if item.seq == accepted.seq)
        then = state.formers.decided_then if state.formers is not None else None
        taken = {item.seq: item.at for item in items if item.channel == "conversation"}
        bubbles = conversation.fold(opened, writer=row.writer, redaktion=deciders,
                                    decided_then=then, taken_at=taken)
        split = conversation.accepted_split(bubbles, decision, submission_id=sid,
                                            redaktion=deciders)
        content = split.structured[1] if split is not None else {}
        if content.get("revision_hash") != row.view.revision_hash:
            return None
        return tuple((s["pubkey"], int(s["weight"]), str(s.get("role") or ""))
                     for s in content.get("shares") or ())

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
        held, opening = self.open_wraps(sid, state.room, items)
        opened = [held[item.seq] for item in items if held.get(item.seq) is not None]
        view = row.view
        who = dict(sid=sid, writer=row.writer,
                   participants=view.participants if view is not None else (),
                   deciders=deciders, checks=self.checks, formers=state.formers)
        members = rules.members_of(state.members) if state.members is not None else None
        return Detail(
            row=row, publication=owner.publication_name(row.publication),
            decision=rules.newest_decision(items, sid, deciders, self.checks, state.formers),
            thread=tuple(threads.build_thread(items, opened, me=self._account.writer,
                                              members=members, **who)),
            history=tuple(threads.history(items, **who)), opened=tuple(opened),
            outgoing=tuple(owner.talk.outgoing(sid)), members=state.members,
            latest=state.versions.get(view.revision) if view is not None else None,
            versions=dict(state.versions), loading=state.loading or opening,
            problem=state.problem,
            # Only where the server holds the submission as this person's
            # and its latest version verified: the room key is its own then.
            can_write=state.room is not None and row.confirmed and not state.not_confirmed
            and row.situation is not rules.Situation.WITHDRAWN,
            not_confirmed=state.not_confirmed, complete=state.members is not None,
            section=self.section(sid) if row.situation is rules.Situation.PUBLISHED else "")


__all__ = ["Detail", "Reading", "Version", "secret_of"]
