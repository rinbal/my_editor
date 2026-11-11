# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a submission says to the person who follows it (plan 10.4;
editorial contract 10, 11, 12.4, 13). Pure: no Qt, no network.

One table (:data:`TABLE`, in :mod:`.situations`) takes a submission's
:class:`Situation` to its one status word (the row), the section of the
list it is in (Du bist dran, Läuft, Erledigt), the one sentence of its
detail with a quieter second line, and at most one primary
:class:`Action`. The situation comes from what is held, never from
timing (lessons 2, 32):

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
newest decision's signed action against the member list (10), and the
table names the decision each situation rests on (:func:`rests_on`):
the sentence names the editor only then. Once the submission was read
whole, a decision that is missing, another one or not verified is said
as the server reports it, with "Mehr dazu", and nothing that acts on the
Redaktion's word is offered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AbstractSet, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

from i18n import N_, _

from . import views
from .entries import SubmissionEntry
from .former import Formers
from .progress import Phase, Progress
# The situations' vocabulary moved out; every name is still offered here.
from .situations import (
    ACTION_LABELS, NOTHING_TO_DO, ON_A_DECISION, ON_ITS_WAY, TABLE, Action, Section,
    Situation, Words,
)
from .watcher import Followed, NotHandedIn, StoredOther

DECISIONS = ("request_changes", "accept", "decline", "reopen")
# Who may decide (11.3: curators never do).
DECIDER_ROLES = ("owner", "redaktion")

YOUR_TURN = N_("Your turn")
# 10: a decision whose signed action does not verify (the contract's words).
NOT_SIGNED = N_("This decision carries no signature of the editorial team.")
# Text that differs from the version handed in (the prototype's words).
EDITED = N_("Your changes are in the editor.")
EDITED_MORE = N_("Hand them in as version {number} when you’re ready.")
# The text changed while nobody asked for changes: said, and offered
# quietly (RG-M3-b 10).
EDITED_SINCE = N_("You’ve changed the text since.")
VERSION_IN_REVIEW = N_("{name} is reviewing version {number}.")
SECTION_PLACE = N_("In the section “{section}”.")
# The writer confirmed; contributors still confirm (M-4 M14).
STILL_CONFIRMS = N_("Thanks. {name} still confirms.")
STILL_CONFIRM = N_("Thanks. The others still confirm.")


def my_turn(view: views.SubmissionView, me: str) -> bool:
    """Whether it is this person's turn, as the server says (11.2)."""
    turn = view.turn
    return (turn.type == "writer" and view.writer == me) or (
        turn.type == "contributors" and me in turn.pending)


def correction_taken(view: views.SubmissionView) -> bool:
    """Whether the writer confirmed the waiting correction (12.4: the
    server drops the word when the next correction comes)."""
    return "correction" in (view.confirmations.get(view.writer) or ())


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
        # for a writer-signed work (11.2, ceac858); once they took it, the
        # View holds their ``correction`` confirmation (RG-M4-b 8).
        if mine and view.turn.type == "writer":
            return (Situation.CORRECTION_TAKEN if correction_taken(view)
                    else Situation.CORRECTION)
        return Situation.PUBLISHED if view.placed else Situation.PUBLISHED_UNPLACED
    return Situation.DECLINED if state == "declined" else Situation.WITHDRAWN


@dataclass(frozen=True)
class Row:
    """One submission in the list. ``view`` is the server's, when one is
    known; ``entry`` the list's (a writer's, or a contributor's once the
    wrap was opened). ``confirmed``: the server holds this submission as
    this person's (a writer's entry by its receipt, 12.4; a contributor's
    by the server's View), so its conversation can be read and written."""

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
    confirmed: bool = False

    @property
    def section(self) -> Section:
        return TABLE[self.situation].section

    @property
    def turn(self) -> bool:
        return self.section is Section.TURN

    @property
    def open(self) -> bool:
        """Whether it can be revised or withdrawn (11.3), once nothing
        of it is on its way."""
        return self.view is not None and self.view.is_open and self.role == "writer" \
            and self.situation not in ON_ITS_WAY


def _held_view(entry: SubmissionEntry, known: Optional[Followed],
               served: Optional[views.SubmissionView], *,
               nothing_held: bool) -> Optional[views.SubmissionView]:
    """The server's View a writer's row may show (lessons 16, 32): the
    lookup's, asked with the entry's own status key, or a newer one the
    server gave since. Without a lookup answer, a served View counts
    only for an entry that was stored once: the server lists a round by
    its sid, whoever's secrets it holds, so a View alone never says that
    this entry arrived."""
    if known is None:
        return served if entry.stored and not nothing_held else None
    view = known.item.submission
    return served if served is not None and served.last_seq > view.last_seq else view


def _entry_situation(entry: SubmissionEntry, known: Optional[Followed],
                     view: Optional[views.SubmissionView], *, me: str,
                     nothing_held: bool, refused: bool) -> Situation:
    """What the list's entry and the lookup's answer for it come to
    (12.4, 13): never from timing. ``view`` is :func:`_held_view`'s;
    ``nothing_held``: the server answered that it holds nothing for the
    entry's status key."""
    if known is None:
        if nothing_held:
            if refused:
                return Situation.ELSEWHERE
            return Situation.UNKNOWN if entry.stored else Situation.UNSENT
        if not entry.stored:
            return Situation.UNCONFIRMED
        # Stored once: a View says where it stands now, but "Eingereicht"
        # is the receipt's to say (a picture may be missing, 12.4).
        situation = situation_of(view, me) if view is not None else Situation.UNKNOWN
        return Situation.UNKNOWN if situation is Situation.SUBMITTED else situation
    status = known.status
    if isinstance(status, StoredOther):
        return Situation.UNCONFIRMED
    if isinstance(status, NotHandedIn):
        return Situation.NOT_HANDED_IN
    situation = situation_of(view, me)
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
    contributor). A writer's entry takes its situation from the entry
    and the lookup; a served View only refines one stored before
    (:func:`_held_view`). ``running`` are the runs on their way, by sid;
    ``refused(entry)`` says its round was refused as another device's;
    ``seen(sid)`` is the read state. Du bist dran first, the newest
    first in each section."""
    rows: List[Row] = []
    listed = set()
    for entry in entries:
        listed.add(entry.sid)
        served_view = served.get(entry.sid)
        if entry.role == "writer":
            known, nothing = followed.get(entry.sid), entry.sid in unknown
            view = _held_view(entry, known, served_view, nothing_held=nothing)
            situation = _entry_situation(entry, known, view, me=me, nothing_held=nothing,
                                         refused=refused(entry))
            confirmed = view is not None and situation not in (
                Situation.UNCONFIRMED, Situation.NOT_HANDED_IN)
        else:
            view = served_view
            situation = situation_of(view, me) if view is not None else Situation.UNKNOWN
            confirmed = view is not None
        progress = running.get(entry.sid)
        if progress is not None:
            situation = Situation.UNCONFIRMED if progress.phase is Phase.UNCONFIRMED \
                else Situation.SENDING
        rows.append(_row(entry.sid, situation, view, entry, me=me, seen=seen,
                         confirmed=confirmed))
    for sid, view in served.items():
        if sid in listed:
            continue
        situation = situation_of(view, me)
        if view.writer == me and situation is Situation.SUBMITTED:
            # The person's own work without its entry here: no receipt
            # is held for it on this device (12.4).
            situation = Situation.ELSEWHERE
        rows.append(_row(sid, situation, view, None, me=me, seen=seen,
                         confirmed=situation is not Situation.ELSEWHERE))
    order = (Section.TURN, Section.RUNNING, Section.DONE)
    rows.sort(key=lambda row: (order.index(row.section), -row.updated_at, row.sid))
    return rows


def _row(sid: str, situation: Situation, view: Optional[views.SubmissionView],
         entry: Optional[SubmissionEntry], *, me: str, seen: Callable[[str], int],
         confirmed: bool) -> Row:
    role = entry.role if entry is not None else ("writer" if view.writer == me
                                                 else "contributor")
    return Row(
        sid=sid, title=view.title if view is not None else entry.title,
        publication=view.publication if view is not None else entry.publication,
        kind=view.kind if view is not None else entry.kind, role=role,
        writer=view.writer if view is not None else (me if role == "writer" else ""),
        d=entry.d if entry is not None else "", situation=situation,
        updated_at=view.updated_at if view is not None else entry.updated_at,
        unread=view is not None and view.unread(seen(sid)), view=view, entry=entry,
        confirmed=confirmed)


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


def members_of(members: Iterable) -> Tuple[str, ...]:
    """Every active member of the Redaktion (``answers.Member``), curators
    with them: all of them may write in the conversation (11.3)."""
    return tuple(m.pubkey for m in members if m.status == "active")


class SignedChecks:
    """What each stored signed action verified to, kept (RG-M3-a 4): its
    signature, its payload, its path and its body are checked once per
    stored action and body, never again on a refresh; who signed it is
    asked each time (:func:`action_verified`), as the member list may
    change. One per account, dropped with it."""

    def __init__(self) -> None:
        self._bodies: dict = {}

    def body(self, signed: views.SignedRecord, *, path: str,
             sid: str) -> Optional[Mapping]:
        """The verified body of ``signed`` for ``path``, or None when it
        does not verify (contract 10, but for the signer's role)."""
        key = (signed.event.get("id"), path, sid, signed.body)
        if key not in self._bodies:
            try:
                self._bodies[key] = views.signed_action_body(signed, path=path,
                                                             submission_id=sid)
            except views.SignedActionInvalid:
                self._bodies[key] = None
        return self._bodies[key]


def action_verified(event: views.TimelineEvent, sid: str, signers: Optional[Iterable[str]],
                    checks: Optional[SignedChecks] = None,
                    formers: Optional[Formers] = None) -> Optional[bool]:
    """Whether a timeline event's stored signed action verifies as one of
    ``signers`` (10); None while they are not known. ``checks`` keeps what
    was checked; ``formers`` (Redaktion actions only) verify people who left."""
    if signers is None:
        return None
    path = views.action_path(event.name, sid)
    if path is None or event.signed is None:
        return False
    body = (checks or SignedChecks()).body(event.signed, path=path, sid=sid)
    signer = event.signed.event.get("pubkey")
    if body is None or signer != event.actor:
        return False
    at = event.signed.event.get("created_at")
    if signer not in set(signers) and (formers is None or not formers.decides(
            signer, event.name, body, decisions=DECISIONS, at=at)):
        return False
    return event.name not in DECISIONS or body.get("outcome") == event.name


def newest_decision(items: Sequence[views.TimelineItem], sid: str,
                    deciders: Optional[Iterable[str]],
                    checks: Optional[SignedChecks] = None,
                    formers: Optional[Formers] = None) -> Optional[Decision]:
    """The newest decision among ``items``, verified against
    ``deciders`` (None: the member list is not known) and ``formers``."""
    decisions = [item for item in items
                 if item.event is not None and item.event.name in DECISIONS]
    if not decisions:
        return None
    event = max(decisions, key=lambda item: item.seq).event
    signers = None if deciders is None else tuple(deciders)
    return Decision(event.name, event.actor, action_verified(event, sid, signers, checks, formers))


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
    is not signed. ``offered``: the action is there to take, not what
    the situation asks for (a plain button, not the accent)."""

    sentence: str
    more: str = ""
    action: Optional[Action] = None
    unsigned: bool = False
    offered: bool = False


def status_of(row: Row, *, publication: str, name_of: Callable[[str], str],
              decision: Optional[Decision] = None, complete: bool = False,
              edited: bool = False, section: str = "") -> Status:
    """The detail's sentence and action for ``row``. ``decision`` is the
    newest decision of its timeline (:func:`newest_decision`);
    ``complete``: the submission was read whole, with its member list,
    so a decision that is missing, another one or not verified is said
    (10: nothing is said while the page did not load, and a server
    cannot unsay it by leaving the item out). ``edited``: the text in
    the editor differs from the version handed in, so the next version
    can be handed in (while the submission is open). ``section``: the one
    a placed report that counts names (10), never the server's alone."""
    entry = TABLE[row.situation]
    view = row.view
    number = view.revision if view is not None else 1
    writer = name_of(row.writer) if row.writer else ""
    fields = dict(publication=publication, writer=writer or _("The writer"), number=number,
                  name="")
    sentence, more, action = entry.sentence, entry.more, entry.action
    rests = rests_on(row.situation, decision)
    unsigned = complete and not rests
    if entry.decision:
        fields["name"] = name_of(decision.actor) if rests and decision.actor else ""
    elif row.situation is Situation.IN_REVIEW:
        fields["name"] = name_of(view.responsible) if view.responsible else ""
        if fields["name"] and number > 1:
            sentence = VERSION_IN_REVIEW
    if row.situation is Situation.PUBLISHED and section:
        more, fields["section"] = SECTION_PLACE, section
    if "{name}" in sentence and not fields["name"]:
        sentence = entry.unnamed_sentence
    if action in ON_A_DECISION and not rests:
        action = None
    offered = False
    if action is Action.VIEW:
        # Ansehen is there to take, never asked for; only with an address.
        offered = True
        if view is None or view.published is None:
            action = None
    waiting = waiting_for(row)
    if waiting and rests:
        names = [name_of(pk) for pk in waiting]
        sentence, more, action, offered = (
            (STILL_CONFIRMS, "", Action.REMIND, True) if len(waiting) == 1 and names[0]
            else (STILL_CONFIRM, "", Action.REMIND, True))
        fields["name"] = names[0]
    if row.open and row.situation is not Situation.PICTURES_MISSING:
        if edited:
            action = Action.HAND_IN
            if row.situation is Situation.CHANGES_REQUESTED:
                sentence, more = EDITED, EDITED_MORE
                fields["number"] = number + 1
            else:
                more, offered = EDITED_SINCE, True
    return Status(_(sentence).format(**fields), _(more).format(**fields) if more else "",
                  action, unsigned, offered)


def waiting_for(row: Row) -> Tuple[str, ...]:
    """Whom the writer waits for once they confirmed (11.2 ``confirm``,
    turn ``contributors``): the contributors still to confirm."""
    view = row.view
    if (row.situation is not Situation.ACCEPTED or row.role != "writer" or view is None
            or view.step != "confirm" or view.turn.type != "contributors"):
        return ()
    return tuple(pk for pk in view.turn.pending if pk != row.writer)


def rests_on(situation: Situation, decision: Optional[Decision]) -> bool:
    """Whether ``situation`` rests on what it must (10): on no decision
    of the Redaktion at all, or on the newest one of the timeline, which
    is the one :data:`TABLE` names for it and whose signed action
    verified."""
    required = TABLE[situation].decision
    return not required or (decision is not None and decision.name == required
                            and decision.verified is True)


__all__ = [
    "ACTION_LABELS", "Action", "DECIDER_ROLES", "DECISIONS", "Decision", "NOTHING_TO_DO",
    "correction_taken",
    "NOT_SIGNED", "ON_A_DECISION", "ON_ITS_WAY", "Row",
    "Section", "SignedChecks", "Situation", "Status", "TABLE", "Words", "YOUR_TURN", "action_verified",
    "deciders_of", "members_of", "my_turn", "newest_decision", "pill_word", "rests_on",
    "rows_of", "situation_of", "status_of", "status_word", "waiting_for",
]
