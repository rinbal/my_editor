# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The situations a followed submission can be in, and what each says
(plan 10.4; editorial contract 11): :data:`TABLE` takes a
:class:`Situation` to its status word, its section of the list, the
detail's sentence and quieter line, and at most one primary
:class:`Action`. Moved out of :mod:`.following`, which offers every name
here as before and decides the situation; this is only its vocabulary.
Pure: no Qt, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from i18n import N_, _

from . import words


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
    VIEW = "view"                       # Ansehen: the published article
    REMIND = "remind"                   # Paul erinnern (the writer, 12.4 /remind)


ACTION_LABELS = {
    Action.REVISE: N_("Revise in the Editor"),
    Action.HAND_IN: N_("Hand In New Version …"),
    Action.PUBLISH: N_("Publish …"),
    Action.CONFIRM: N_("Confirm …"),
    Action.COMPARE_CORRECTION: N_("Compare …"),
    Action.RESEND: N_("Send Again"),
    Action.VIEW: N_("View"),
    Action.REMIND: N_("Send a Reminder"),
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
    CORRECTION_TAKEN = "correction taken"   # the writer took it, it is not public yet
    PUBLISHED = "published"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


# A run or a check on its way: nothing else is started meanwhile.
ON_ITS_WAY = frozenset({Situation.SENDING, Situation.UNCONFIRMED})


@dataclass(frozen=True)
class Words:
    """What one situation says (untranslated): ``word`` for the row,
    ``sentence`` and the quieter ``more`` for the detail; ``{name}`` is
    the editor, ``{publication}`` the publication, ``{writer}`` the
    writer, ``{number}`` the version. ``unnamed_word`` and
    ``unnamed_sentence`` stand in while the editor has no known name or
    no verified signature. ``decision`` is the Redaktion's decision the
    situation rests on (10, 11.3): it is true only as far as the newest
    decision of the timeline is that one and its signed action verifies."""

    word: str
    section: Section
    sentence: str
    more: str = ""
    action: Optional[Action] = None
    unnamed_word: str = ""
    unnamed_sentence: str = ""
    decision: str = ""


NOTHING_TO_DO = N_("You don’t need to do anything right now.")

TABLE = {
    Situation.SENDING: Words(N_("Submitting …"), Section.RUNNING, N_("Submitting …")),
    Situation.UNCONFIRMED: Words(N_("Not confirmed yet"), Section.RUNNING,
                                 N_("Not confirmed yet."), N_("MyEditor keeps checking.")),
    Situation.UNSENT: Words(N_("Not sent"), Section.TURN, N_("Not sent."),
                            N_("This submission didn’t reach {publication}. Send it again or "
                               "discard the attempt. Your article stays."), Action.RESEND),
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
        unnamed_sentence=N_("The editorial team asks for changes."),
        decision="request_changes"),
    Situation.CHANGES_BY_WRITER: Words(
        N_("Changes requested"), Section.RUNNING, N_("{name} asks for changes."),
        N_("{writer} is revising the text."),
        unnamed_sentence=N_("The editorial team asks for changes."),
        decision="request_changes"),
    Situation.CONFIRM: Words(N_("Please confirm"), Section.TURN, N_("Accepted."),
                             N_("Confirm your part so it can be published."), Action.CONFIRM,
                             decision="accept"),
    Situation.ACCEPTED: Words(N_("Accepted"), Section.RUNNING, N_("Accepted."),
                              N_("{publication} takes the next step."), decision="accept"),
    Situation.PUBLISH: Words(N_("Accepted"), Section.TURN, N_("Accepted."),
                             N_("Publish it when you’re ready."), Action.PUBLISH,
                             decision="accept"),
    Situation.PUBLISHED_UNPLACED: Words(N_("Published, place to follow"), Section.RUNNING,
                                        N_("Published."),
                                        N_("{publication} gives it its place next."),
                                        Action.VIEW, decision="accept"),
    Situation.CORRECTION: Words(N_("Correction suggested"), Section.TURN,
                                N_("{publication} suggests a correction."),
                                N_("Compare it with what is public. If you take it, it "
                                   "replaces your published text."),
                                Action.COMPARE_CORRECTION),
    # Taken and not published yet, as the web says it (RG-M4-b 8): the
    # writer's confirmation is in the View, so it is not asked again.
    Situation.CORRECTION_TAKEN: Words(N_("Correction taken"), Section.TURN,
                                      N_("Correction taken, not published yet."),
                                      N_("Publish it so everyone reads the corrected text."),
                                      Action.PUBLISH),
    Situation.PUBLISHED: Words(N_("Published"), Section.DONE, N_("Published on {publication}."),
                               action=Action.VIEW, decision="accept"),
    Situation.DECLINED: Words(N_("Declined"), Section.DONE, N_("Declined."),
                              N_("You can submit the text again, or somewhere else."),
                              decision="decline"),
    Situation.WITHDRAWN: Words(N_("Withdrawn"), Section.DONE, N_("Withdrawn."),
                               N_("What the editorial team already had stays there.")),
}

def and_list(items: Sequence[str]) -> str:
    """Names as one list: "Paul, Eva and Jana"."""
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return _("{items} and {last}").format(items=", ".join(items[:-1]), last=items[-1])


# What rests on the Redaktion's word is offered only once that word
# verified (9.4, 12.4): a split is confirmed, a text published, on the
# verified accept alone. A later phase's handlers get no other.
ON_A_DECISION = frozenset({Action.PUBLISH, Action.CONFIRM})


__all__ = ["ACTION_LABELS", "Action", "and_list", "NOTHING_TO_DO", "ON_A_DECISION", "ON_ITS_WAY", "Section",
           "Situation", "TABLE", "Words"]
