# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a submission on its way shows (plan 10.3): the values the run
of :mod:`.submitting` hands the screen after every step, and nothing
else. :class:`Progress` names the :class:`Step` the run is in, the
:class:`Phase` the handoff shows for it, what the server holds of the
submission as far as the run knows (:class:`Held`, lesson 32: the one
fact the words of a failure and of a stopped run are said from,
:data:`HELD_WORDS`), the first message's :class:`MessageStatus`, and why
a step did not happen (:class:`Failure`). Pure: no Qt, no requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from . import views, words


class Step(Enum):
    KEY = "key"                     # the Redaktion's key (8.1)
    ENCRYPTING = "encrypting"       # SMF1, each picture
    UPLOADING = "uploading"         # to the writer's media servers
    RECORDING = "recording"         # the entry on the list (13)
    SIGNING = "signing"             # the NIP-98 request
    SENDING = "sending"             # on its way: no cancelling now
    # The list read again: another device of this writer stored this round
    # first (stale_revision for a revision 1, contract 2 at 72d5ddc).
    RELOADING = "reloading"
    CHECKING = "checking"           # no answer: asked by the status key
    REUPLOADING = "reuploading"     # files the receipt missed
    # Send Again's: the next revision, built here, after a stored one this
    # app could not take as the writer's (12.4 at bfa8844).
    REVISING = "revising"
    SUBMITTED = "submitted"         # a matching receipt, every file found
    FAILED = "failed"
    CANCELLED = "cancelled"


class Phase(Enum):
    """What the handoff shows (plan 10.3)."""

    ENCRYPT = "encrypt"
    UPLOAD = "upload"
    SIGNER = "signer"
    SEND = "send"
    UNCONFIRMED = "unconfirmed"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


PHASE_OF = {
    Step.KEY: Phase.ENCRYPT, Step.ENCRYPTING: Phase.ENCRYPT, Step.REVISING: Phase.ENCRYPT,
    Step.UPLOADING: Phase.UPLOAD,
    Step.REUPLOADING: Phase.UPLOAD, Step.RECORDING: Phase.SIGNER, Step.SIGNING: Phase.SIGNER,
    Step.SENDING: Phase.SEND, Step.RELOADING: Phase.UNCONFIRMED,
    Step.CHECKING: Phase.UNCONFIRMED,
    Step.SUBMITTED: Phase.DONE,
    Step.FAILED: Phase.FAILED, Step.CANCELLED: Phase.CANCELLED,
}
_CANCELLABLE = frozenset({Step.KEY, Step.ENCRYPTING, Step.UPLOADING, Step.RECORDING,
                          Step.SIGNING})


class Held(Enum):
    """What the server holds of this submission, as far as the run knows
    (lesson 32): the one fact every "nothing is submitted", "not
    confirmed" and "is in" of a failure or of a stopped run is said
    from (:data:`HELD_WORDS`), on every screen. Set when a request goes
    out, by the receipt, by a refusal and by the lookup; never by a
    step's name alone."""

    NOTHING = "nothing"     # nothing went out, or the server said it has none of it
    MAYBE = "maybe"         # a request went out with no answer to judge it by yet
    STORED = "stored"       # a receipt: the server stores this revision (files may miss)
    OTHER = "other"         # it holds another version of the work, or this one not as sent


@dataclass(frozen=True)
class HeldWords:
    """What one :class:`Held` says (untranslated), on every screen:
    ``failed``, the title of a failure (what did not happen, lessons 36);
    ``stopped``, the status
    bar's when the account was let go while the run went on;
    ``sending`` and ``checking``, the handoff's line while the request is
    on its way and while the lookup asks (``{publication}`` filled in);
    ``link_name`` and ``link_name_why``, why Article Details keeps the
    text's link name, and its tooltip."""

    failed: str
    stopped: str
    sending: str = words.SENDING
    checking: str = words.UNCONFIRMED
    link_name: str = words.LINK_NAME_KEPT_FOR_SENDING
    link_name_why: str = words.LINK_NAME_KEPT_FOR_SENDING_WHY


HELD_WORDS = {
    Held.NOTHING: HeldWords(words.NOT_SUBMITTED, words.STOPPED_NOTHING_SENT),
    Held.MAYBE: HeldWords(words.NOT_CONFIRMED, words.STOPPED_UNCONFIRMED),
    # A receipt: whatever is asked or sent again, the server has it.
    Held.STORED: HeldWords(words.PICTURE_NOT_IN, words.STOPPED_PICTURES,
                           sending=words.STORED_SENDING, checking=words.STORED_CHECKING,
                           link_name=words.LINK_NAME_SUBMITTED,
                           link_name_why=words.LINK_NAME_SUBMITTED_WHY),
    Held.OTHER: HeldWords(words.VERSION_NOT_SUBMITTED, words.STOPPED_OTHER_VERSION),
}
# Furthest from known first: the run a stopped account names.
HELD_UNKNOWN_FIRST = (Held.MAYBE, Held.STORED, Held.NOTHING, Held.OTHER)


class MessageStatus(Enum):
    NONE = "none"
    SIGNING = "signing"             # the signer app is asked to seal it
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True)
class Failure:
    """What did not happen, said once (lessons 36): ``title`` (what did
    not happen, from what the server holds, :data:`HELD_WORDS`) and
    ``sentence`` (the likely cause and what to do), untranslated, with
    ``{publication}`` filled in where they name it; ``said``, the list's
    own words in the sentence's place (already translated: one sentence
    with their own cause and what to do, lists.py), or "";
    ``details``, what an upload answered (server names, codes), only
    behind Details; where trying again goes on (``retry``; None when it
    would not help). ``retry_at``: the clock time before which trying
    again only meets the same limit (a 429's Retry-After, contract 15);
    None when it may at once. ``error``: False for an end that is no
    error (the work is in, sent from another device): said calmly, not
    in the error colour."""

    sentence: str
    retry: Optional[Step]
    title: str
    said: str = ""
    details: str = ""
    retry_at: Optional[float] = None
    error: bool = True


@dataclass(frozen=True)
class Progress:
    step: Step
    pictures: int = 0
    uploaded: float = 0.0           # 0 to 1 while uploading
    failure: Optional[Failure] = None
    receipt: Optional[views.Receipt] = field(default=None, repr=False)
    message: MessageStatus = MessageStatus.NONE
    # A request was written to the network once: from then on nothing is
    # cancelled, whatever the step (the server may hold it).
    sent: bool = False
    held: Held = Held.NOTHING
    # The first message did not go (FAILED): said with its title and its
    # sentence, and a limit's wait in ``retry_at`` (sent again from then).
    message_failure: Optional[Failure] = None

    @property
    def phase(self) -> Phase:
        return PHASE_OF[self.step]

    @property
    def cancellable(self) -> bool:
        return self.step in _CANCELLABLE and not self.sent


__all__ = [
    "Failure", "HELD_UNKNOWN_FIRST", "HELD_WORDS", "Held", "HeldWords", "MessageStatus",
    "PHASE_OF", "Phase", "Progress", "Step",
]
