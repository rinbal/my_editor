# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a submission says to the person who follows it (plan 10.4;
editorial contract 10, 11, 12.4, 13). Pure: no Qt, no network.

One table (:data:`TABLE`) takes a submission's :class:`Situation` to its
one status word (the row), the section of the list it is in (Du bist
dran, Läuft, Erledigt), the one sentence of its detail with a quieter
second line, and at most one primary :class:`Action`. The situation
comes from what is held, never from timing (lessons 2, 32):

- a run on its way (``Progress``): "Wird eingereicht …";
- the list's entry and what the server answered for its status key
  (:class:`~.watcher.Followed`): an entry the server has no receipt for
  is not sent (Erneut, Verwerfen: contract 13), one whose round another
  device stored says so, and "Eingereicht" is said only of a receipt
  that confirms the entry (12.4);
- the server's View (:func:`situation_of`): its state, step and turn.

:func:`rows_of` joins the list's entries with the lookup's answers and
the server's Views (works the person is named in) into rows, unread
from ``last_message_seq`` (9.3). :func:`newest_decision` verifies the
newest decision's signed action against the member list (10): the
sentence names the editor only then, and a decision that does not
verify is said as the server reports it, with "Mehr dazu".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AbstractSet, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

from i18n import N_, _

from . import views, words
from .entries import SubmissionEntry
from .progress import Phase, Progress
from .watcher import Followed, NotHandedIn, StoredOther

DECISIONS = ("request_changes", "accept", "decline", "reopen")
# Who may decide (11.3: curators never do).
DECIDER_ROLES = ("owner", "redaktion")


class Section(Enum):
    TURN = "turn"               # Du bist dran
    RUNNING = "running"         # Läuft
    DONE = "done"               # Erledigt


class Action(Enum):
    """The one primary action a situation offers. PUBLISH, CONFIRM and
    COMPARE_CORRECTION are M-4's: the detail shows a button only for an
    action the window handles."""

    REVISE = "revise"                   # Im Editor überarbeiten
    HAND_IN = "hand in"                 # Neue Fassung abgeben …
    PUBLISH = "publish"                 # Veröffentlichen …
    CONFIRM = "confirm"                 # Bestätigen …
    COMPARE_CORRECTION = "correction"   # Vergleichen …
    RESEND = "resend"                   # Erneut senden (the entry is reused)
    RELOAD = "reload"                   # Erneut laden


ACTION_LABELS = {
    Action.REVISE: N_("Revise in the Editor"),
    Action.HAND_IN: N_("Hand In New Version …"),
    Action.PUBLISH: N_("Publish …"),
    Action.CONFIRM: N_("Confirm …"),
    Action.COMPARE_CORRECTION: N_("Compare …"),
    Action.RESEND: N_("Send Again"),
}


class Situation(Enum):
    SENDING = "sending"                 # a run is on its way
    UNCONFIRMED = "unconfirmed"         # sent maybe: the lookup has not said
    UNSENT = "unsent"                   # the server has no receipt for the entry
    ELSEWHERE = "elsewhere"             # another device stored this round
    NOT_HANDED_IN = "not handed in"     # a stored copy that is not the writer's
    PICTURES_MISSING = "pictures missing"
    UNKNOWN = "unknown"                 # stored once; its state is not read yet
    SUBMITTED = "submitted"
    IN_REVIEW = "in review"
    CHANGES_REQUESTED = "changes requested"         # the writer's turn
    CHANGES_BY_WRITER = "changes by the writer"     # as a contributor sees it
    CONFIRM = "confirm"                 # accepted, this person's confirmation waits
    ACCEPTED = "accepted"               # accepted, others act next
    PUBLISH = "publish"                 # accepted, the writer publishes
    PUBLISHED_UNPLACED = "published, place follows"
    CORRECTION = "correction"           # a correction waits for the writer
    PUBLISHED = "published"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Words:
    """What one situation says (untranslated): ``word`` for the row,
    ``sentence`` and the quieter ``more`` for the detail; ``{name}`` is
    the editor, ``{publication}`` the publication, ``{writer}`` the
    writer, ``{number}`` the version. ``unnamed_word`` and
    ``unnamed_sentence`` stand in while the editor has no known name or
    no verified signature."""

    word: str
    section: Section
    sentence: str
    more: str = ""
    action: Optional[Action] = None
    unnamed_word: str = ""
    unnamed_sentence: str = ""


NOTHING_TO_DO = N_("You don’t need to do anything right now.")

TABLE = {
    Situation.SENDING: Words(N_("Submitting …"), Section.RUNNING, N_("Submitting …")),
    Situation.UNCONFIRMED: Words(N_("Not confirmed yet"), Section.RUNNING,
                                 N_("Not confirmed yet."), N_("MyEditor keeps checking.")),
    Situation.UNSENT: Words(N_("Not sent"), Section.TURN, N_("Not sent."),
                            N_("The server has nothing of this submission. Send it again, "
                               "or discard it."), Action.RESEND),
    Situation.ELSEWHERE: Words(words.SUBMITTED_ELSEWHERE, Section.RUNNING,
                               words.SUBMITTED_ELSEWHERE, words.ELSEWHERE_NOT_HERE_YET,
                               Action.RELOAD),
    Situation.NOT_HANDED_IN: Words(N_("Not submitted"), Section.TURN, N_("Not submitted."),
                                   words.NOT_HANDED_IN, Action.RESEND),
    Situation.PICTURES_MISSING: Words(N_("Picture missing"), Section.RUNNING,
                                      N_("In the inbox of {publication}, a picture is missing."),
                                      words.PICTURES_IN_THE_APP_THAT_SENT),
    Situation.UNKNOWN: Words("", Section.RUNNING, N_("The current status isn’t loaded yet.")),
    Situation.SUBMITTED: Words(N_("Submitted"), Section.RUNNING,
                               N_("In the inbox of {publication}."), NOTHING_TO_DO),
    Situation.IN_REVIEW: Words(N_("{name} is reviewing"), Section.RUNNING,
                               N_("{name} is reviewing."), NOTHING_TO_DO,
                               unnamed_word=N_("In review"),
                               unnamed_sentence=N_("The editorial team is reviewing.")),
    Situation.CHANGES_REQUESTED: Words(
        N_("Changes requested"), Section.TURN, N_("{name} asks for changes."),
        N_("What exactly is in the conversation below."), Action.REVISE,
        unnamed_sentence=N_("The editorial team asks for changes.")),
    Situation.CHANGES_BY_WRITER: Words(
        N_("Changes requested"), Section.RUNNING, N_("{name} asks for changes."),
        N_("{writer} is revising the text."),
        unnamed_sentence=N_("The editorial team asks for changes.")),
    Situation.CONFIRM: Words(N_("Please confirm"), Section.TURN, N_("Accepted."),
                             N_("Confirm your part so it can be published."), Action.CONFIRM),
    Situation.ACCEPTED: Words(N_("Accepted"), Section.RUNNING, N_("Accepted."),
                              N_("{publication} takes the next step.")),
    Situation.PUBLISH: Words(N_("Accepted"), Section.TURN, N_("Accepted."),
                             N_("Publish it when you’re ready."), Action.PUBLISH),
    Situation.PUBLISHED_UNPLACED: Words(N_("Published, place to follow"), Section.RUNNING,
                                        N_("Published."),
                                        N_("{publication} gives it its place next.")),
    Situation.CORRECTION: Words(N_("Correction suggested"), Section.TURN,
                                N_("{publication} suggests a correction."),
                                N_("Compare it. If you accept it, the address stays the same."),
                                Action.COMPARE_CORRECTION),
    Situation.PUBLISHED: Words(N_("Published"), Section.DONE, N_("Published in {publication}.")),
    Situation.DECLINED: Words(N_("Declined"), Section.DONE, N_("Declined."),
                              N_("You can submit the text again, or somewhere else.")),
    Situation.WITHDRAWN: Words(N_("Withdrawn"), Section.DONE, N_("Withdrawn."),
                               N_("What the editorial team already had stays there.")),
}

YOUR_TURN = N_("Your turn")
# 10: a decision whose signed action does not verify (the contract's words).
NOT_SIGNED = N_("This decision carries no signature of the editorial team.")
# Text that differs from the version handed in (the prototype's words).
EDITED = N_("Your changes are in the editor.")
EDITED_MORE = N_("Hand them in as version {number} when you’re ready.")
VERSION_IN_REVIEW = N_("{name} is reviewing version {number}.")
SECTION_PLACE = N_("In the section “{section}”.")


def my_turn(view: views.SubmissionView, me: str) -> bool:
    """Whether it is this person's turn, as the server says (11.2)."""
    turn = view.turn
    return (turn.type == "writer" and view.writer == me) or (
        turn.type == "contributors" and me in turn.pending)


def situation_of(view: views.SubmissionView, me: str) -> Situation:
    """A View's situation for ``me`` (11.1, 11.2): the state, the step
    and the turn, all the server's."""
    state, mine = view.state, view.writer == me
    if state == "submitted":
        return Situation.SUBMITTED
    if state == "in_review":
        return Situation.IN_REVIEW
    if state == "changes_requested":
        return Situation.CHANGES_REQUESTED if mine else Situation.CHANGES_BY_WRITER
    if state == "accepted":
        if view.step == "confirm" and me in view.turn.pending:
            return Situation.CONFIRM
        if view.step == "writer_publishes" and mine:
            return Situation.PUBLISH
        return Situation.ACCEPTED
    if state == "published":
        # A correction stored and not yet published is the writer's turn
        # for a writer-signed work (11.2, ceac858).
        if mine and view.turn.type == "writer":
            return Situation.CORRECTION
        return Situation.PUBLISHED if view.placed else Situation.PUBLISHED_UNPLACED
    return Situation.DECLINED if state == "declined" else Situation.WITHDRAWN


@dataclass(frozen=True)
class Row:
    """One submission in the list. ``view`` is the server's, when one is
    known; ``entry`` the list's (a writer's, or a contributor's once the
    wrap was opened)."""

    sid: str
    title: str
    publication: str
    kind: str
    role: str
    writer: str
    d: str
    situation: Situation
    updated_at: int
    unread: bool = False
    view: Optional[views.SubmissionView] = None
    entry: Optional[SubmissionEntry] = None

    @property
    def section(self) -> Section:
        return TABLE[self.situation].section

    @property
    def turn(self) -> bool:
        return self.section is Section.TURN

    @property
    def open(self) -> bool:
        """Whether it can be revised or withdrawn (11.3)."""
        return self.view is not None and self.view.is_open and self.role == "writer"


def _entry_situation(entry: SubmissionEntry, known: Optional[Followed], *, me: str,
                     nothing_held: bool, refused: bool) -> Situation:
    """What the list's entry and the lookup's answer for it come to
    (12.4, 13): never from timing. ``nothing_held``: the server answered
    that it holds nothing for the entry's status key."""
    if known is None:
        if not nothing_held:
            return Situation.UNKNOWN if entry.stored else Situation.UNCONFIRMED
        if refused:
            return Situation.ELSEWHERE
        return Situation.UNKNOWN if entry.stored else Situation.UNSENT
    status = known.status
    if isinstance(status, StoredOther):
        return Situation.UNCONFIRMED
    if isinstance(status, NotHandedIn):
        return Situation.NOT_HANDED_IN
    situation = situation_of(known.item.submission, me)
    if situation is Situation.SUBMITTED and known.item.receipt.blobs_missing:
        return Situation.PICTURES_MISSING       # never "Eingereicht" then (12.4)
    return situation


def rows_of(entries: Sequence[SubmissionEntry], *, me: str,
            followed: Mapping[str, Followed], served: Mapping[str, views.SubmissionView],
            unknown: AbstractSet[str], running: Mapping[str, Progress],
            refused: Callable[[SubmissionEntry], bool],
            seen: Callable[[str], int]) -> List[Row]:
    """The rows of the list (contract 12.4 at 3b00351): the list's
    ``entries`` joined with the lookup's answers (``followed``; ``unknown``
    are the sids the server answered it holds nothing for) and the
    server's Views (``served``, where the person is writer or
    contributor). ``running`` are the runs on their way, by sid;
    ``refused(entry)`` says its round was refused as another device's;
    ``seen(sid)`` is the read state. Du bist dran first, the newest
    first in each section."""
    rows: List[Row] = []
    listed = set()
    for entry in entries:
        listed.add(entry.sid)
        known = followed.get(entry.sid) if entry.role == "writer" else None
        view = known.item.submission if known is not None else None
        other = served.get(entry.sid)
        if other is not None and (view is None or other.last_seq > view.last_seq):
            view = other
        progress = running.get(entry.sid)
        if progress is not None and progress.phase is Phase.UNCONFIRMED:
            situation = Situation.UNCONFIRMED
        elif progress is not None:
            situation = Situation.SENDING
        elif entry.role != "writer" or (known is None and view is not None):
            situation = situation_of(view, me) if view is not None else Situation.UNKNOWN
        else:
            situation = _entry_situation(entry, known, me=me,
                                         nothing_held=entry.sid in unknown,
                                         refused=refused(entry))
        rows.append(_row(entry.sid, situation, view, entry, me=me, seen=seen))
    for sid, view in served.items():
        if sid not in listed:
            rows.append(_row(sid, situation_of(view, me), view, None, me=me, seen=seen))
    order = (Section.TURN, Section.RUNNING, Section.DONE)
    rows.sort(key=lambda row: (order.index(row.section), -row.updated_at, row.sid))
    return rows


def _row(sid: str, situation: Situation, view: Optional[views.SubmissionView],
         entry: Optional[SubmissionEntry], *, me: str, seen: Callable[[str], int]) -> Row:
    role = entry.role if entry is not None else ("writer" if view.writer == me
                                                 else "contributor")
    return Row(
        sid=sid, title=view.title if view is not None else entry.title,
        publication=view.publication if view is not None else entry.publication,
        kind=view.kind if view is not None else entry.kind, role=role,
        writer=view.writer if view is not None else (me if role == "writer" else ""),
        d=entry.d if entry is not None else "", situation=situation,
        updated_at=view.updated_at if view is not None else entry.updated_at,
        unread=view is not None and view.unread(seen(sid)), view=view, entry=entry)


# --------------------------------------------------------------------------- #
# The newest decision (10)                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Decision:
    """The newest decision of a timeline: ``verified`` is True when its
    stored signed action is a decider's (10), False when it is not, and
    None while the member list is not known (nothing is said then)."""

    name: str
    actor: Optional[str]
    verified: Optional[bool]


def deciders_of(members: Iterable) -> Tuple[str, ...]:
    """The members who may decide (``answers.Member``): active, owner or
    Redaktion."""
    return tuple(m.pubkey for m in members
                 if m.status == "active" and m.role in DECIDER_ROLES)


def action_verified(event: views.TimelineEvent, sid: str,
                    signers: Optional[Iterable[str]]) -> Optional[bool]:
    """Whether a timeline event's stored signed action verifies as one
    of ``signers`` (10); None while ``signers`` are not known."""
    if signers is None:
        return None
    path = views.action_path(event.name, sid)
    if path is None or event.signed is None:
        return False
    try:
        body = views.signed_action_body(event.signed, path=path, submission_id=sid,
                                        signers=signers)
    except views.SignedActionInvalid:
        return False
    if event.signed.event.get("pubkey") != event.actor:
        return False
    return event.name not in DECISIONS or body.get("outcome") == event.name


def newest_decision(items: Sequence[views.TimelineItem], sid: str,
                    deciders: Optional[Iterable[str]]) -> Optional[Decision]:
    """The newest decision among ``items``, verified against
    ``deciders`` (None: the member list is not known)."""
    decisions = [item for item in items
                 if item.event is not None and item.event.name in DECISIONS]
    if not decisions:
        return None
    event = max(decisions, key=lambda item: item.seq).event
    signers = None if deciders is None else tuple(deciders)
    return Decision(event.name, event.actor, action_verified(event, sid, signers))


# --------------------------------------------------------------------------- #
# The words                                                                    #
# --------------------------------------------------------------------------- #

def status_word(row: Row, name_of: Callable[[str], str]) -> str:
    """The row's one status word (translated). ``name_of(pubkey)`` is a
    person's name, or ``""`` when it is not known."""
    entry = TABLE[row.situation]
    name = name_of(row.view.responsible) if row.view is not None and row.view.responsible \
        else ""
    if "{name}" in entry.word and not name:
        return _(entry.unnamed_word)
    return _(entry.word).format(name=name) if entry.word else ""


def pill_word(row: Row, name_of: Callable[[str], str]) -> str:
    """What the pill beside the tab says: whose turn first (plan 10.4)."""
    return _(YOUR_TURN) if row.turn else status_word(row, name_of)


@dataclass(frozen=True)
class Status:
    """The detail's status block (translated): one sentence, a quieter
    line, at most one action, and whether "Mehr dazu" says the decision
    is not signed."""

    sentence: str
    more: str = ""
    action: Optional[Action] = None
    unsigned: bool = False


def status_of(row: Row, *, publication: str, name_of: Callable[[str], str],
              decision: Optional[Decision] = None, edited: bool = False) -> Status:
    """The detail's sentence and action for ``row``. ``edited``: the
    text in the editor differs from the version handed in, so the next
    version can be handed in (while the submission is open)."""
    entry = TABLE[row.situation]
    view = row.view
    number = view.revision if view is not None else 1
    writer = name_of(row.writer) if row.writer else ""
    fields = dict(publication=publication, writer=writer or _("The writer"), number=number,
                  name="")
    sentence, more, action = entry.sentence, entry.more, entry.action
    unsigned = False
    if row.situation in (Situation.CHANGES_REQUESTED, Situation.CHANGES_BY_WRITER):
        fields["name"] = _decider_name(decision, "request_changes", name_of)
        unsigned = decision is not None and decision.verified is False
    elif row.situation is Situation.IN_REVIEW:
        fields["name"] = name_of(view.responsible) if view.responsible else ""
        if fields["name"] and number > 1:
            sentence = VERSION_IN_REVIEW
    elif row.situation in (Situation.CONFIRM, Situation.ACCEPTED, Situation.PUBLISH,
                           Situation.DECLINED):
        unsigned = decision is not None and decision.verified is False
    elif row.situation is Situation.PUBLISHED and view.section:
        more = SECTION_PLACE
        fields["section"] = view.section
    if "{name}" in sentence and not fields["name"]:
        sentence = entry.unnamed_sentence
    if row.open and row.situation is not Situation.PICTURES_MISSING:
        if edited:
            action = Action.HAND_IN
            if row.situation is Situation.CHANGES_REQUESTED:
                sentence, more = EDITED, EDITED_MORE
                fields["number"] = number + 1
    return Status(_(sentence).format(**fields), _(more).format(**fields) if more else "",
                  action, unsigned)


def _decider_name(decision: Optional[Decision], name: str,
                  name_of: Callable[[str], str]) -> str:
    """The editor a sentence may name: the signer of the verified newest
    decision of that kind, never an unverified one's."""
    if decision is None or decision.name != name or decision.verified is not True \
            or not decision.actor:
        return ""
    return name_of(decision.actor)


__all__ = [
    "ACTION_LABELS", "Action", "DECIDER_ROLES", "DECISIONS", "Decision", "NOT_SIGNED", "Row",
    "Section", "Situation", "Status", "TABLE", "Words", "YOUR_TURN", "action_verified",
    "deciders_of", "my_turn", "newest_decision", "pill_word", "rows_of", "situation_of",
    "status_of", "status_word",
]
