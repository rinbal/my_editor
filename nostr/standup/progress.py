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
    ROUND = "round"                 # chosen again after stale_revision (contract 2)
    CHECKING = "checking"           # no answer: asked by the status key
    REUPLOADING = "reuploading"     # files the receipt missed
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
    Step.KEY: Phase.ENCRYPT, Step.ENCRYPTING: Phase.ENCRYPT, Step.UPLOADING: Phase.UPLOAD,
    Step.REUPLOADING: Phase.UPLOAD, Step.RECORDING: Phase.SIGNER, Step.SIGNING: Phase.SIGNER,
    Step.SENDING: Phase.SEND, Step.ROUND: Phase.SEND, Step.CHECKING: Phase.UNCONFIRMED,
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
    """What one :class:`Held` says (untranslated): ``failed``, the second
    line of a failure; ``stopped``, the status bar's when the account was
    let go while the run went on (``{publication}`` filled in)."""

    failed: str
    stopped: str


HELD_WORDS = {
    Held.NOTHING: HeldWords(words.NOTHING_SUBMITTED, words.STOPPED_NOTHING_SENT),
    Held.MAYBE: HeldWords(words.MAYBE_RECEIVED, words.STOPPED_UNCONFIRMED),
    Held.STORED: HeldWords(words.STORED_PICTURE_MISSING, words.STOPPED_PICTURES),
    Held.OTHER: HeldWords(words.TEXT_STAYS, words.STOPPED_OTHER_VERSION),
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
    """What did not happen: ``headline`` (untranslated words, with
    ``{publication}`` filled in where they name it), ``detail`` (what the
    server holds, :data:`HELD_WORDS`), ``said`` (the list's or the
    upload's own words, already translated, or ""), and where trying
    again goes on (``retry``; None when it would not help). The second
    line shown is ``said`` followed by ``detail``. ``retry_at``: the
    clock time before which trying again only meets the same limit (a
    429's Retry-After, contract 15); None when it may at once."""

    headline: str
    retry: Optional[Step]
    detail: str
    said: str = ""
    retry_at: Optional[float] = None


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
    # The first message met a limit: sent again from this clock time on.
    message_retry_at: Optional[float] = None

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
