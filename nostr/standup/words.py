# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What submitting says, in the plan's words (plan 10.3, 10.7; editorial
contract 12.2). English source texts, marked for translation and
translated where they are shown (``_()``); the German is in
``locale/de.po``. Pure: no Qt.

The handoff's lines are the plan's exactly ("Wird verschlüsselt …",
"Eingereicht. Im Eingang von …"). A refusal of the server is said by
its code (:func:`refusal`), never by the server's own message, which
the app does not show raw. What did not happen is said first, then
that the text is still there (plan 10.7: "Nicht gesendet", the work is
kept, one retry).
"""

from __future__ import annotations

from i18n import N_

from .errors import Code
from .trust import KEY_NOT_CONFIRMED

# -- the handoff, step by step (plan 10.3) -----------------------------------

ENCRYPTING = N_("Encrypting …")
UPLOADING_PICTURE = N_("Uploading picture …")
UPLOADING_PICTURES = N_("Uploading pictures …")
UPLOADING_DETAIL = N_("Encrypted, to your media server.")
WAITING_FOR_SIGNER = N_("Waiting for your signer app.")
WAITING_DETAIL = N_("Confirm the request there.")
SENDING = N_("Sending to {publication} …")
UNCONFIRMED = N_("Not confirmed yet. Checking …")
UNCONFIRMED_DETAIL = N_("You can close the window, MyEditor keeps checking.")
SUBMITTED = N_("Submitted. In the inbox of {publication}.")
SUBMITTED_DETAIL = N_("The server confirmed receipt.")
IN_THE_BACKGROUND = N_("Submitting …")
NOT_CONFIRMED = N_("Not confirmed")

# The first message to the editorial team, after the receipt.
MESSAGE_SENDING = N_("Sending your message …")
MESSAGE_SENT = N_("Your message to the editorial team is sent.")
MESSAGE_NOT_SENT = N_("Your message wasn’t sent.")

# -- what did not happen ------------------------------------------------------

NOTHING_SUBMITTED = N_("Nothing is submitted, your text stays in the editor.")
STORED_PICTURE_MISSING = N_("The server has your submission; a picture is still missing.")
NOT_SENT = N_("Not sent. No connection to the server.")
SIGNER_DECLINED = N_("Not sent. Your signer app declined the request.")
SIGNER_SILENT = N_("Not sent. Your signer app didn’t answer.")
LIST_NOT_SAVED = N_("Your list of submissions couldn’t be saved.")
LIST_CHANGED_ELSEWHERE = N_("Another device changed this submission just now.")
UPLOAD_FAILED = N_("A picture couldn’t be uploaded.")
PICTURE_MISSING = N_("A picture can’t be found on your media server.")
TOO_LARGE = N_("This submission is larger than {publication} takes.")
ANOTHER_VERSION = N_("{publication} already has another version of this submission.")
NOT_TAKEN = N_("The server didn’t take the submission.")

# Refusals by the contract's codes (12.2), each in the writer's words.
_REFUSALS = {
    Code.PAUSED: N_("{publication} isn’t taking submissions right now."),
    Code.BLOCKED: N_("{publication} doesn’t take submissions from you."),
    Code.LIMIT_OPEN: N_("You already have as many open submissions at {publication} "
                        "as it takes."),
    Code.RATE_LIMITED: N_("Too many submissions in a short time. Try again later."),
    Code.TOO_LARGE: TOO_LARGE,
    Code.STALE_REVISION: ANOTHER_VERSION,
    Code.EPOCH: KEY_NOT_CONFIRMED,
    Code.KEY_EVENT_INVALID: KEY_NOT_CONFIRMED,
    Code.NO_DECIDER: N_("Nobody at {publication} could decide on it: the only person who "
                        "can is named among those who take part."),
    Code.OFFLINE: NOT_SENT,
    Code.TIMEOUT: NOT_SENT,
    Code.UNAVAILABLE: NOT_SENT,
    Code.SIGNER_DECLINED: SIGNER_DECLINED,
    Code.SIGNER_UNREACHABLE: SIGNER_SILENT,
}


def refusal(code: str) -> str:
    """The words (untranslated) for a refusal with ``code``; every code
    not named has the same plain sentence."""
    return _REFUSALS.get(code, NOT_TAKEN)


__all__ = [
    "ANOTHER_VERSION", "ENCRYPTING", "IN_THE_BACKGROUND", "LIST_CHANGED_ELSEWHERE",
    "LIST_NOT_SAVED", "MESSAGE_NOT_SENT", "MESSAGE_SENDING", "MESSAGE_SENT", "NOTHING_SUBMITTED",
    "NOT_CONFIRMED", "NOT_SENT", "NOT_TAKEN", "PICTURE_MISSING", "SENDING", "SIGNER_DECLINED",
    "SIGNER_SILENT", "STORED_PICTURE_MISSING", "SUBMITTED", "SUBMITTED_DETAIL", "TOO_LARGE", "UNCONFIRMED",
    "UNCONFIRMED_DETAIL", "UPLOADING_DETAIL", "UPLOADING_PICTURE", "UPLOADING_PICTURES",
    "UPLOAD_FAILED", "WAITING_DETAIL", "WAITING_FOR_SIGNER", "refusal",
]
