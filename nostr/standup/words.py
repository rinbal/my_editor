# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What submitting says, in the plan's words (plan 10.3, 10.7; editorial
contract 12.2). English source texts, marked for translation and
translated where they are shown (``_()``); the German is in
``locale/de.po``. Pure: no Qt.

The handoff's lines are the plan's exactly ("Wird verschlüsselt …",
"Eingereicht. Im Eingang von …"). A refusal of the server is said by
its code (:func:`refusal`), never by the server's own message, which
the app does not show raw. A failure is said once (lessons 36,
:mod:`nostr.failures`): a title, what did not happen, from what the
server holds; one sentence, the likely cause and what to do; one retry
(plan 10.7: the work is kept).
"""

from __future__ import annotations

from i18n import N_

from .. import failures
from .errors import PASSING, Code, cause_words

# -- the handoff, step by step (plan 10.3) -----------------------------------

ENCRYPTING = N_("Encrypting …")
UPLOADING_PICTURE = N_("Uploading picture …")
UPLOADING_PICTURES = N_("Uploading pictures …")
UPLOADING_DETAIL = N_("Encrypted, to your media server.")
WAITING_FOR_SIGNER = N_("Waiting for your signer app.")
WAITING_DETAIL = N_("Confirm the request there.")
SENDING = N_("Sending to {publication} …")
UNCONFIRMED = N_("Not confirmed yet. Checking …")
# The server has the submission (a receipt) while a picture is still
# missing: the body sent again, then asked again (lesson 32).
STORED_SENDING = N_("The server has your submission. Sending it again with the picture …")
STORED_CHECKING = N_("The server has your submission. Checking the picture …")
# Another device of this writer stored this round first (stale_revision
# for a revision 1, contract 2 at 72d5ddc; the contract's German is
# "Auf einem anderen Gerät eingereicht.", and the English stays as it is:
# both decided by the owner on 2026-11-10).
# Not an error: the work is in. Reload reads the list again until that
# device's entry, with the secrets the server holds, is here.
SUBMITTED_ELSEWHERE = N_("Submitted on another device.")
ELSEWHERE_NOT_HERE_YET = N_("Its details aren’t on this device yet.")
ELSEWHERE_HERE_NOW = N_("Its details are on this device now.")
READING_LIST = N_("Reading your list of submissions …")
UNCONFIRMED_DETAIL = N_("You can close the window, MyEditor keeps checking.")
SUBMITTED = N_("Submitted. In the inbox of {publication}.")
SUBMITTED_DETAIL = N_("The server confirmed receipt.")
IN_THE_BACKGROUND = N_("Submitting …")
# In the status bar while the sheet is closed: what a signer prompt is
# for, and a first message that did not go (RG-M2b 11).
MESSAGE_SIGNING_IN_THE_BACKGROUND = N_("Your submission to {publication} is in. Confirm your "
                                       "message to the editorial team in your signer app.")
MESSAGE_NOT_SENT_IN_THE_BACKGROUND = N_("Your message to the editorial team of {publication} "
                                        "wasn’t sent: open Submit to a Publication for this "
                                        "text to send it again.")
MESSAGE_UNCONFIRMED_IN_THE_BACKGROUND = N_("Your message to the editorial team of {publication} "
                                           "may not have arrived: open Submit to a Publication "
                                           "for this text before you send it again.")
# The account was let go (sign-out, another account, a signer paired
# anew) while a submission was on its way (RG-M2b 9).
STOPPED_NOTHING_SENT = N_("Your submission to {publication} stopped when the account "
                          "changed, before anything was sent.")
STOPPED_UNCONFIRMED = N_("Your submission to {publication} stopped when the account changed, "
                         "before the server confirmed it: open Submit to a Publication with "
                         "that account to see whether it arrived.")
STOPPED_OTHER_VERSION = N_("Your submission to {publication} stopped when the account changed, "
                           "and {publication} already has another version of it.")
STOPPED_PICTURES = N_("Your submission to {publication} is in, but a picture was still being "
                      "uploaded when the account changed.")
STOPPED_MESSAGE = N_("Your submission to {publication} is in, but your message may not have "
                     "reached the editorial team: the account changed.")

# The first message to the editorial team, after the receipt.
MESSAGE_SIGNING = N_("Waiting for your signer app: confirm your message there.")
MESSAGE_SENDING = N_("Sending your message …")
MESSAGE_SENT = N_("Your message to the editorial team is sent.")
# A first message that did not go (lessons 36): its title, by what is
# known of it, and one sentence with the cause and what to do; the
# submission is in either way.
MESSAGE_NOT_SENT = N_("Your message wasn’t sent")
MESSAGE_NOT_CONFIRMED = N_("Your message may not have arrived")
MESSAGE_MAYBE_ARRIVED = N_("The server didn’t answer after it was sent: send it again only if "
                           "the editorial team doesn’t reply.")
MESSAGE_NOT_BUILT = N_("MyEditor couldn’t put your message together: your submission is in "
                       "all the same.")
MESSAGE_REFUSED = N_("The server didn’t take your message: your submission is in all the "
                     "same.")
MESSAGE_LIMIT_AT = N_("The server takes no more messages from you right now: send it again at "
                      "{time}.")
MESSAGE_LIMIT_TOMORROW_AT = N_("The server takes no more messages from you right now: send it "
                               "again tomorrow at {time}.")
MESSAGE_LIMIT_LATER = N_("The server takes no more messages from you right now: send it again "
                         "later.")
MESSAGE_LIMIT_OVER = N_("The wait for the server is over: you can send your message again now.")

# -- why Article Details keeps a text's link name (RG-M2r2a 1 and 2) ----------

LINK_NAME_SUBMITTED = N_("A submitted article keeps its link name.")
LINK_NAME_SUBMITTED_WHY = N_("The publication knows this article by its link name. Another "
                             "one would be another submission, and once accepted it is "
                             "published under this one.")
LINK_NAME_KEPT_FOR_SENDING = N_("This article keeps its link name for sending it to the "
                                "publication.")
LINK_NAME_KEPT_FOR_SENDING_WHY = N_("Its submission is made from its link name: another one "
                                    "would be another submission.")
LINK_NAME_CHECKING = N_("Checking whether this article was submitted …")
LINK_NAME_NOT_CHECKED = N_("MyEditor couldn’t check whether this article was submitted, so "
                           "it keeps its link name for now.")
LINK_NAME_CHECKING_WHY = N_("If it was submitted, another link name would be another "
                            "submission.")

# -- what did not happen (lessons 36) -----------------------------------------

# The titles, by what the server holds (HELD_WORDS, lesson 32).
NOT_SUBMITTED = N_("Your article wasn’t submitted")
NOT_CONFIRMED = N_("Your submission isn’t confirmed")
PICTURE_NOT_IN = N_("Your submission is missing a picture")
VERSION_NOT_SUBMITTED = N_("This version wasn’t submitted")

# The sentences: the likely cause, and what to do.
LIST_ENTRY_UNUSABLE = N_("Your list of submissions holds an entry for this text that "
                         "MyEditor cannot use.")
KEY_NOT_CONFIRMED_AGAIN = N_("The editorial team’s key couldn’t be confirmed: try again in "
                             "a moment.")
# Sent, but the server, asked by the status key, has nothing of it.
NOT_RECEIVED = N_("The server didn’t receive your submission: try again.")
SERVER_TROUBLE = failures.SERVER_TROUBLE
LIST_NOT_SAVED = N_("Your list of submissions couldn’t be saved: check your internet "
                    "connection and try again.")
LIST_CHANGED_ELSEWHERE = N_("Another device changed this submission just now.")
UPLOAD_FAILED = N_("A picture couldn’t be uploaded: check your internet connection and try "
                   "again.")
# The server still misses a picture after it was uploaded again twice in
# a row (12.4 at 33d92c9): said once, with Try Again.
PICTURE_MISSING = N_("A picture didn’t arrive at {publication}: try again to upload it once "
                     "more.")
# Stored, a picture missing, and this app holds none of its files (another
# device, or a run that ended before this start): the contract's words.
PICTURES_IN_THE_APP_THAT_SENT = N_("Open the submission in the app you sent it with: that app "
                                   "uploads the pictures again.")
TOO_LARGE = N_("This submission is larger than {publication} takes.")
ANOTHER_VERSION = N_("{publication} already has another version of this submission.")
# This revision is stored, with another envelope or other wraps than this
# app sent (12.4 "No answer after sending").
STORED_OTHER = N_("{publication} received this submission, but not as MyEditor sent it.")
NOT_TAKEN = N_("The server didn’t take the submission.")
# The revision could not be built here: nothing went out (lesson 8).
NOT_BUILT = N_("MyEditor couldn’t put this submission together.")

# Refusals by the contract's codes (12.2), each in the writer's words.
_REFUSALS = {
    Code.PAUSED: N_("{publication} isn’t taking submissions right now."),
    Code.BLOCKED: N_("{publication} doesn’t take submissions from you."),
    Code.LIMIT_OPEN: N_("You already have as many open submissions at {publication} "
                        "as it takes."),
    Code.RATE_LIMITED: N_("You’ve sent {publication} as much as it takes for now: try again "
                          "later."),
    Code.TOO_LARGE: TOO_LARGE,
    Code.STALE_REVISION: ANOTHER_VERSION,
    Code.EPOCH: KEY_NOT_CONFIRMED_AGAIN,
    Code.KEY_EVENT_INVALID: KEY_NOT_CONFIRMED_AGAIN,
    Code.NO_DECIDER: N_("Nobody at {publication} could decide on it: the only person who "
                        "can is named among those who take part."),
    Code.UNAVAILABLE: failures.CHECK_CONNECTION,
    **{code: cause_words(code) for code in PASSING},
    Code.SIGNER_DECLINED: failures.SIGNER_DECLINED,
    Code.SIGNER_UNREACHABLE: failures.SIGNER_SILENT,
}


# A limit of the server (429, contract 15), said with when trying again
# helps, in one sentence (the clock time of its Retry-After; without one,
# the refusal's own words). The publication's, and the server's own (the
# publications' list).
LIMIT_AT = N_("You’ve sent {publication} as much as it takes for now: try again at {time}.")
LIMIT_TOMORROW_AT = N_("You’ve sent {publication} as much as it takes for now: try again "
                       "tomorrow at {time}.")
SERVER_LIMIT_AT = N_("The server can’t take more from you right now: try again at {time}.")
SERVER_LIMIT_TOMORROW_AT = N_("The server can’t take more from you right now: try again "
                              "tomorrow at {time}.")
SERVER_LIMIT_LATER = N_("The server can’t take more from you right now: try again later.")
# Once the wait is over (RG-M2r3 14): Try Again works, and the words say so.
LIMIT_OVER = N_("The wait for {publication} is over: you can try again now.")
SERVER_LIMIT_OVER = N_("The wait for the server is over: you can try again now.")

# The sheet: what did not happen there (lessons 36), each said once with
# its sentence and Try Again.
PUBLICATIONS_NOT_LOADED = N_("The publications couldn’t be loaded")
NOT_CHECKED = N_("MyEditor couldn’t check whether you submitted this text before")
NOT_DISCARDED = N_("The earlier try wasn’t discarded")


def refusal(code: str) -> str:
    """The words (untranslated) for a refusal with ``code``; every code
    not named has the same plain sentence."""
    return _REFUSALS.get(code, NOT_TAKEN)


# What a first message met, as its sentence (the signer's and the
# connection's are a submission's).
_MESSAGE_SENTENCES = {
    Code.REFUSED: MESSAGE_NOT_BUILT,
    Code.RATE_LIMITED: MESSAGE_LIMIT_LATER,
    Code.SIGNER_DECLINED: failures.SIGNER_DECLINED,
    Code.SIGNER_UNREACHABLE: failures.SIGNER_SILENT,
    **{code: cause_words(code) for code in PASSING},
}


def message_failure_words(code: str, *, maybe_stored: bool = False) -> tuple:
    """The title and the sentence (untranslated) for a first message that
    did not go with ``code``: one the server may hold is never said not
    to be sent; any other refusal of the server is its own sentence."""
    if maybe_stored:
        return MESSAGE_NOT_CONFIRMED, MESSAGE_MAYBE_ARRIVED
    return MESSAGE_NOT_SENT, _MESSAGE_SENTENCES.get(code, MESSAGE_REFUSED)


__all__ = [
    "ANOTHER_VERSION", "ELSEWHERE_HERE_NOW", "ELSEWHERE_NOT_HERE_YET", "ENCRYPTING",
    "IN_THE_BACKGROUND", "KEY_NOT_CONFIRMED_AGAIN", "LIMIT_AT", "LIMIT_OVER",
    "LIMIT_TOMORROW_AT",
    "LINK_NAME_CHECKING", "LINK_NAME_CHECKING_WHY", "LINK_NAME_KEPT_FOR_SENDING",
    "LINK_NAME_KEPT_FOR_SENDING_WHY", "LINK_NAME_NOT_CHECKED", "LINK_NAME_SUBMITTED",
    "LINK_NAME_SUBMITTED_WHY", "LIST_CHANGED_ELSEWHERE", "LIST_ENTRY_UNUSABLE", "LIST_NOT_SAVED",
    "MESSAGE_LIMIT_AT", "MESSAGE_LIMIT_LATER", "MESSAGE_LIMIT_OVER", "MESSAGE_LIMIT_TOMORROW_AT",
    "MESSAGE_MAYBE_ARRIVED", "MESSAGE_NOT_BUILT", "MESSAGE_NOT_CONFIRMED", "MESSAGE_NOT_SENT",
    "MESSAGE_NOT_SENT_IN_THE_BACKGROUND", "MESSAGE_REFUSED", "MESSAGE_SENDING", "MESSAGE_SENT",
    "MESSAGE_UNCONFIRMED_IN_THE_BACKGROUND",
    "MESSAGE_SIGNING", "MESSAGE_SIGNING_IN_THE_BACKGROUND", "NOT_BUILT", "NOT_CHECKED",
    "NOT_CONFIRMED", "NOT_DISCARDED", "NOT_RECEIVED", "NOT_SUBMITTED", "NOT_TAKEN",
    "PICTURE_MISSING",
    "PICTURES_IN_THE_APP_THAT_SENT", "PICTURE_NOT_IN", "PUBLICATIONS_NOT_LOADED", "READING_LIST",
    "SENDING", "SERVER_LIMIT_AT",
    "SERVER_LIMIT_LATER", "SERVER_LIMIT_OVER", "SERVER_LIMIT_TOMORROW_AT",
    "SERVER_TROUBLE", "STOPPED_MESSAGE", "STOPPED_NOTHING_SENT", "STOPPED_OTHER_VERSION",
    "STOPPED_PICTURES", "STOPPED_UNCONFIRMED", "STORED_CHECKING", "STORED_OTHER",
    "STORED_SENDING", "SUBMITTED", "SUBMITTED_DETAIL", "SUBMITTED_ELSEWHERE", "TOO_LARGE",
    "UNCONFIRMED", "UNCONFIRMED_DETAIL", "UPLOADING_DETAIL", "UPLOADING_PICTURE",
    "UPLOADING_PICTURES", "UPLOAD_FAILED", "VERSION_NOT_SUBMITTED", "WAITING_DETAIL",
    "WAITING_FOR_SIGNER", "message_failure_words", "refusal",
]
