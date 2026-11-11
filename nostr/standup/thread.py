# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the conversation about a submission shows (plan 7.1; editorial
contract 9, 10, 12.4). Pure: no Qt, no network.

:func:`build_thread` turns the timeline's items of the conversation
channel (events, and the messages that passed every check of 9.2,
``conversation.open_items``) into what is shown, in the server's
``seq`` order:

- :class:`Message`: a bubble, grouped by sender (the name on the first
  of a group, the picture on the last);
- :class:`Caption`: a decision or another step as one quiet line. It
  names the person only when the stored signed action verified (10); one
  that did not is said without a name, as the server reports it;
- :class:`VersionCard`: a new version ("Fassung 2 von Jana"), with the
  writer's note for it (the ``revision-note`` of 9.4, which is not a
  bubble of its own);
- :class:`Day`: a separator where the day changes.

Internal notes never reach here: the writer's side holds no epoch key,
reads only the conversation channel, and :func:`build_thread` drops an
item of any other channel (criterion 8). :func:`history` is every event
as a caption, for Info › Verlauf.

The server cannot read a seal, so whoever holds the room key can post
under any key. :func:`role_of` is therefore the one place that says who
a sender is, for the thread, the list's newest message and the notice
at the article: a member of the Redaktion by the member list, the
writer or someone taking part by the submission, and anyone else nobody
at all, whose messages and reactions are not shown. Nobody outside the
Redaktion is ever called the Redaktion (:func:`sender_label`).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from i18n import N_, _

from . import conversation, formats, views
from .following import DECISIONS, SignedChecks, action_verified
from .former import Formers

# The steps the conversation shows as captions; the rest (who was named,
# handed over, reminded) are only in the history.
CAPTIONS = ("submitted", "taken", "request_changes", "accept", "decline", "reopen", "withdrawn",
            "confirmed", "published", "placed")
CARDS = ("revision", "correction")
# The steps the writer, or someone taking part, signs; every other is the
# Redaktion's (the member list says who that is).
_BY_PARTICIPANT = ("submitted", "revision", "withdrawn", "confirmed", "reminder")


class Role(Enum):
    """Who the sender of a message is (:func:`role_of`)."""

    MEMBER = "member"               # of the Redaktion, by the member list
    PARTICIPANT = "participant"     # the writer, or someone taking part
    UNKNOWN = "unknown"             # not taking part; the member list is not read yet
    STRANGER = "stranger"           # neither: never shown


def role_of(sender: str, *, writer: str, participants: Iterable[str],
            members: Optional[Iterable[str]]) -> Role:
    """The role of ``sender`` in a submission of ``writer`` with
    ``participants``. ``members`` are the Redaktion's active members
    (``following.members_of``), or None while the member list is not
    known: nobody is a member then, and nobody a stranger."""
    if members is not None and sender in members:
        return Role.MEMBER
    if sender == writer or sender in participants:
        return Role.PARTICIPANT
    return Role.UNKNOWN if members is None else Role.STRANGER


def sender_label(role: Role, name: str) -> str:
    """What a sender is called (translated): their ``name`` when it is
    known; else "Redaktion" for a member and for nobody else,
    "Mitwirkende" for someone taking part, and nothing while the role is
    not known."""
    if name or role is Role.UNKNOWN:
        return name
    return _("Editorial team") if role is Role.MEMBER else _("Contributor")


def from_known_senders(opened: Sequence[conversation.Opened], *, writer: str,
                       participants: Iterable[str],
                       members: Optional[Iterable[str]]) -> List[conversation.Opened]:
    """``opened`` without what a stranger sent: their messages, edits and
    reactions are not shown at all."""
    taking_part = tuple(participants)
    known = None if members is None else frozenset(members)
    return [item for item in opened
            if role_of(item.sender, writer=writer, participants=taking_part,
                       members=known) is not Role.STRANGER]


@dataclass(frozen=True)
class Day:
    at: int


@dataclass(frozen=True)
class Caption:
    seq: int
    at: int
    name: str
    actor: Optional[str]
    revision: Optional[int] = None
    to: Optional[str] = None
    # True: the stored signed action is the actor's; False: it is not;
    # None: not known (no member list yet), or nothing was signed.
    verified: Optional[bool] = None


@dataclass(frozen=True)
class VersionCard:
    seq: int
    at: int
    revision: int
    actor: Optional[str]
    correction: bool = False
    note: str = ""
    verified: Optional[bool] = None


@dataclass(frozen=True)
class Message:
    bubble: conversation.Bubble
    mine: bool
    first: bool = True          # of its group: the sender's name goes above
    last: bool = True           # of its group: the sender's picture goes beside
    role: Role = Role.UNKNOWN   # who the sender is (role_of)


@dataclass(frozen=True)
class SplitCard:
    """A split proposal of the editorial team (9.4), shown where it came:
    ``shares`` as ``(pubkey, weight)``; ``accepted`` when it is the one
    the verified accept names (the only one that counts)."""

    seq: int
    at: int
    author: str
    shares: Tuple[Tuple[str, int], ...]
    accepted: bool = False


ThreadItem = Union[Day, Caption, VersionCard, Message, SplitCard]

SPLIT_PROPOSED = N_("{name} proposes a split: {shares}")
SPLIT_PROPOSED_PLAIN = N_("The editorial team proposes a split: {shares}")
SPLIT_ACCEPTED = N_("Accepted with this split.")


def split_text(card: SplitCard, *, me: str, name_of: Callable[[str], str]) -> str:
    """A split card's line (translated): who proposed it, then each
    share; the person reading it is "you"."""
    shares = ", ".join("{name} {weight} %".format(
        name=_("You") if pk == me else (name_of(pk) or _("Someone")), weight=weight)
        for pk, weight in card.shares)
    name = name_of(card.author)
    words = (_(SPLIT_PROPOSED).format(name=name, shares=shares) if name
             else _(SPLIT_PROPOSED_PLAIN).format(shares=shares))
    return words + (". " + _(SPLIT_ACCEPTED) if card.accepted else "")


def _local_day(at: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(at).date()


def _verified(event: views.TimelineEvent, sid: str, *, writer: str,
              participants: Iterable[str], deciders: Optional[Iterable[str]],
              checks: Optional[SignedChecks], formers: Optional[Formers]) -> Optional[bool]:
    if event.signed is None:
        return None
    if event.name in _BY_PARTICIPANT:
        return action_verified(event, sid, (writer, *participants), checks)
    if event.name == "published" and event.actor == writer:
        return action_verified(event, sid, (writer,), checks)
    return action_verified(event, sid, deciders, checks, formers)


def _caption(item: views.TimelineItem, sid: str, **who) -> Caption:
    event = item.event
    return Caption(seq=item.seq, at=item.at, name=event.name, actor=event.actor,
                   revision=event.revision, to=event.to,
                   verified=_verified(event, sid, **who))


def history(items: Sequence[views.TimelineItem], *, sid: str, writer: str,
            participants: Iterable[str] = (), deciders: Optional[Iterable[str]] = None,
            checks: Optional[SignedChecks] = None,
            formers: Optional[Formers] = None) -> List[Caption]:
    """Every event of the conversation channel as a caption, oldest
    first (Info › Verlauf). ``checks`` keeps the signed actions checked;
    ``formers`` verify people who left."""
    who = dict(writer=writer, participants=tuple(participants), deciders=deciders,
               checks=checks, formers=formers)
    return [_caption(item, sid, **who) for item in sorted(items, key=lambda i: i.seq)
            if item.event is not None and item.channel == "conversation"]


def build_thread(items: Sequence[views.TimelineItem],
                 opened: Sequence[conversation.Opened], *, sid: str, me: str, writer: str,
                 participants: Iterable[str] = (), deciders: Optional[Iterable[str]] = None,
                 members: Optional[Iterable[str]] = None,
                 checks: Optional[SignedChecks] = None,
                 formers: Optional[Formers] = None,
                 day_of: Callable[[int], object] = _local_day) -> List[ThreadItem]:
    """What the conversation shows (see the module docstring).
    ``opened`` are the messages of ``items`` that passed the checks of
    9.2; ``deciders`` the members who may decide (None while the member
    list is not known: no editor is named then, and structured notes of
    the Redaktion are not read); ``members`` every active member of the
    Redaktion (None likewise); ``checks`` keeps the signed actions
    checked; ``formers`` verify people who left. ``me`` always takes part."""
    who = dict(writer=writer, participants=tuple(participants), deciders=deciders,
               checks=checks, formers=formers)
    senders = dict(writer=writer, participants=(*who["participants"], me),
                   members=None if members is None else frozenset(members))
    opened = from_known_senders(opened, **senders)
    notes: Dict[int, str] = {}
    shown: List[Tuple[int, int, object]] = []          # (seq, time, what)
    named = _named_split(items, sid, deciders, checks, formers)
    for bubble in conversation.fold(opened, writer=writer, redaktion=deciders or ()):
        if bubble.structured is None:
            shown.append((bubble.seq, bubble.created_at, bubble))
        elif bubble.structured[0] == "revision-note":
            notes[bubble.structured[1]["revision"]] = bubble.structured[1]["note"]
        elif bubble.structured[0] == "split-proposal" and deciders is not None:
            shown.append((bubble.seq, bubble.created_at, SplitCard(
                seq=bubble.seq, at=bubble.created_at, author=bubble.author,
                shares=tuple((s["pubkey"], s["weight"])
                             for s in bubble.structured[1]["shares"]),
                accepted=bubble.id == named)))
    for item in items:
        event = item.event
        if event is None or item.channel != "conversation":
            continue
        if event.name in CAPTIONS:
            shown.append((item.seq, item.at, _caption(item, sid, **who)))
        elif event.name in CARDS and event.revision is not None:
            shown.append((item.seq, item.at, VersionCard(
                seq=item.seq, at=item.at, revision=event.revision, actor=event.actor,
                correction=event.name == "correction",
                verified=_verified(event, sid, **who))))
    shown.sort(key=lambda entry: entry[0])
    out: List[ThreadItem] = []
    day = None
    for index, (_seq, at, what) in enumerate(shown):
        if day_of(at) != day:
            day = day_of(at)
            out.append(Day(at))
        if isinstance(what, VersionCard):
            out.append(replace(what, note=notes.get(what.revision, "")))
        elif isinstance(what, (Caption, SplitCard)):
            out.append(what)
        else:
            before = out[-1] if out else None
            after = shown[index + 1] if index + 1 < len(shown) else None
            first = not (isinstance(before, Message) and before.bubble.author == what.author)
            last = not (after is not None and isinstance(after[2], conversation.Bubble)
                        and after[2].author == what.author and day_of(after[1]) == day)
            out.append(Message(bubble=what, mine=what.author == me, first=first, last=last,
                               role=role_of(what.author, **senders)))
    return out


def _named_split(items, sid: str, deciders, checks, formers) -> Optional[str]:
    """The split rumor id the newest decision names, when it is an accept
    whose signed action verifies as a decider's."""
    decisions = [item for item in items if item.event is not None
                 and item.event.name in DECISIONS and item.channel == "conversation"]
    if not decisions or deciders is None:
        return None
    event = max(decisions, key=lambda item: item.seq).event
    if event.name != "accept" or not action_verified(event, sid, tuple(deciders), checks,
                                                     formers):
        return None
    body = (checks or SignedChecks()).body(event.signed, path=views.action_path("accept", sid),
                                           sid=sid) or {}
    named = body.get("split_rumor_id")
    return named if formats.is_hex64(named) else None


def own_reaction(opened: Sequence[conversation.Opened], target: str,
                 me: str) -> Optional[Tuple[str, str]]:
    """``(rumor id, emoji)`` of this person's standing reaction to the
    message ``target``, or None: the newest one, unless they took it
    back (9.3). What a taking back names."""
    newest: Optional[Tuple[Tuple[int, int], str, str]] = None
    taken_back = set()
    for item in opened:
        rumor = item.rumor
        if item.sender != me:
            continue
        if rumor["kind"] == conversation.REACTION_KIND \
                and formats.tag_value(rumor, "e") == target:
            when = (rumor["created_at"], item.seq)
            if newest is None or when > newest[0]:
                newest = (when, rumor["id"], rumor["content"])
        elif rumor["kind"] == conversation.DELETION_KIND:
            taken_back.add(formats.tag_value(rumor, "e"))
    if newest is None or newest[1] in taken_back:
        return None
    return newest[1], newest[2]


# --------------------------------------------------------------------------- #
# The words                                                                    #
# --------------------------------------------------------------------------- #

# (with the person's name, without it): a name only for a verified
# signature, or for what is the server's own word (who is responsible).
_CAPTION_WORDS = {
    "submitted": ("", N_("Submitted · Version {number}")),
    "revision": ("", N_("Version {number} handed in")),
    "taken": (N_("{name} took it on"), N_("The editorial team took it on")),
    "handed_over": (N_("Handed over to {name}"), N_("Handed over")),
    "handed_back": ("", N_("Handed back to the editorial team")),
    "named": (N_("{name} is responsible"), N_("An editor is responsible")),
    "request_changes": (N_("{name} asked for changes"), N_("Changes requested")),
    "accept": (N_("{name} accepted"), N_("Accepted")),
    "decline": (N_("{name} declined"), N_("Declined")),
    "reopen": (N_("{name} reopened it"), N_("Reopened")),
    "withdrawn": ("", N_("Withdrawn")),
    "confirmed": (N_("{name} confirmed"), N_("Confirmed")),
    "published": ("", N_("Published")),
    "placed": ("", N_("Placed")),
    "correction": ("", N_("Correction suggested")),
    "reminder": ("", N_("Reminder sent")),
    "reupload_requested": ("", N_("The editorial team asked for a picture again")),
}
_BY_YOU = {"withdrawn": N_("Withdrawn by you"), "confirmed": N_("Confirmed by you"),
           "published": N_("Published by you")}
# Who a caption names: the person it goes to, where it has one.
_NAMES_TO = ("handed_over", "named")


def caption_text(caption: Caption, *, me: str, name_of: Callable[[str], str]) -> str:
    """A caption's one line (translated). ``name_of(pubkey)`` is a
    person's name, ``""`` when not known."""
    if caption.name in _BY_YOU and caption.actor == me and caption.verified is not False:
        return _(_BY_YOU[caption.name])
    named, plain = _CAPTION_WORDS[caption.name]
    person = caption.to if caption.name in _NAMES_TO else caption.actor
    # The server's own word (no signed action) may name whom it names; a
    # signed step names its signer only once the signature verified.
    trusted = caption.verified is True or caption.name == "named"
    name = name_of(person) if named and person and trusted else ""
    words = _(named).format(name=name) if name else _(plain)
    return words.format(number=caption.revision or 1)


def card_title(card: VersionCard, *, me: str, name_of: Callable[[str], str]) -> str:
    """"Fassung 2 von Jana" (plan 7.1), "Fassung 2 von dir" for one's
    own; a correction says whose it is only when verified."""
    if card.correction:
        return _("Correction, version {number}").format(number=card.revision)
    if card.actor == me:
        return _("Version {number} by you").format(number=card.revision)
    name = name_of(card.actor) if card.actor and card.verified is True else ""
    if name:
        return _("Version {number} by {name}").format(number=card.revision, name=name)
    return _("Version {number}").format(number=card.revision)


def unsigned(caption: Caption) -> bool:
    """Whether "Mehr dazu" belongs beside it: a decision whose signed
    action does not verify (10)."""
    return caption.name in DECISIONS and caption.verified is False


__all__ = [
    "CAPTIONS", "CARDS", "Caption", "Day", "Message", "Role", "SplitCard", "ThreadItem",
    "VersionCard", "build_thread", "caption_text", "card_title", "from_known_senders", "history",
    "own_reaction", "role_of", "sender_label", "split_text", "unsigned",
]
