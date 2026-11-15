# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The words around joining, and the checks that run before anything is signed.

Why a request did not work, in one sentence with what to do
(:func:`failure_sentence`; what the server said is
:func:`failure_details`, for Details only), inline field messages, and
the input checks
that spare the user a signer prompt for a request certain to be refused.
The person reading these is joining a club, not debugging a client, so
the copy says what happened and what to do next, and nothing else.

Pure: no network.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

from i18n import N_, _, ngettext

from .. import failures
from .models import ApiError, ErrorCode
from .prices import format_number


# --------------------------------------------------------------------------- #
# Limits                                                                       #
# --------------------------------------------------------------------------- #

# The spec caps these. Checked locally so a request that is certain to be
# refused does not cost the user a signer prompt first.
APPLICATION_TEXT_MAX_LENGTH: int = 2000
EMAIL_MAX_LENGTH: int = 255
NIP05_HANDLE_MAX_LENGTH: int = 255


# --------------------------------------------------------------------------- #
# Checking input before anything is signed                                     #
# --------------------------------------------------------------------------- #

_HANDLE_ALLOWED = re.compile(r"\A[a-z0-9_-]+\Z")
_EMAIL_SHAPE = re.compile(r"\A[^@\s]+@[^@\s]+\Z")

_HANDLE_CHARACTERS = _(
    "Use only lowercase letters, numbers, hyphens (-) and underscores (_)."
)


def nip05_handle_problem(handle: str) -> Optional[str]:
    """What is wrong with a requested name, in plain words, or None.

    The rule is the server's: lowercase letters, digits, hyphen and
    underscore, at most 255 characters, because the name becomes part
    of a public address. Whether it is still free only the server knows;
    :func:`handle_field_message` words that answer. An empty field means
    "no name" and should not be passed here.
    """
    value = handle if isinstance(handle, str) else ""
    if not value:
        return _("Enter a name.")
    if len(value) > NIP05_HANDLE_MAX_LENGTH:
        return _("Use {count} characters or fewer.").format(count=NIP05_HANDLE_MAX_LENGTH)
    if _HANDLE_ALLOWED.match(value):
        return None
    if "@" in value:
        return _("Enter only the part before the @ sign.")
    if any(ch.isspace() for ch in value):
        return _("Spaces are not allowed.") + " " + _HANDLE_CHARACTERS
    if _HANDLE_ALLOWED.match(value.lower()):
        return _("Use lowercase letters only.")
    return _HANDLE_CHARACTERS


def email_problem(address: str) -> Optional[str]:
    """A plain-words reason an e-mail address will be refused, or None."""
    value = address if isinstance(address, str) else ""
    if len(value) > EMAIL_MAX_LENGTH:
        return _("Use an address of {count} characters or fewer.").format(
            count=EMAIL_MAX_LENGTH)
    if not _EMAIL_SHAPE.match(value):
        return _("Enter a valid email address.")
    return None


def _message_too_long() -> str:
    return _("Keep your message to {count} characters or fewer.").format(
        count=format_number(APPLICATION_TEXT_MAX_LENGTH))


def application_text_problem(text: str) -> Optional[str]:
    """A plain-words reason the application message will be refused, or None."""
    if isinstance(text, str) and len(text) > APPLICATION_TEXT_MAX_LENGTH:
        return _message_too_long()
    return None


# --------------------------------------------------------------------------- #
# Plain-language copy                                                          #
# --------------------------------------------------------------------------- #

def _wait_words(seconds: Optional[int]) -> str:
    """A limit of EINUNDZWANZIG, in one sentence with how long to wait."""
    if not seconds or seconds <= 0:
        return _("EINUNDZWANZIG takes no more requests from you right now: wait a few "
                 "minutes, then try again.")
    if seconds < 60:
        return ngettext("EINUNDZWANZIG takes no more requests from you right now: wait "
                        "{count} second, then try again.",
                        "EINUNDZWANZIG takes no more requests from you right now: wait "
                        "{count} seconds, then try again.", seconds).format(count=seconds)
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return ngettext("EINUNDZWANZIG takes no more requests from you right now: wait "
                        "about {count} minute, then try again.",
                        "EINUNDZWANZIG takes no more requests from you right now: wait "
                        "about {count} minutes, then try again.",
                        minutes).format(count=minutes)
    hours = math.ceil(minutes / 60)
    if hours < 24:
        return ngettext("EINUNDZWANZIG takes no more requests from you right now: try "
                        "again in about {count} hour.",
                        "EINUNDZWANZIG takes no more requests from you right now: try "
                        "again in about {count} hours.", hours).format(count=hours)
    return _("EINUNDZWANZIG takes no more requests from you right now: try again "
             "tomorrow.")


# Why a request to EINUNDZWANZIG did not work and what to do, one sentence
# each (lessons 36). What did not happen is the title, said by the screen
# that asked (the membership window), so it names the action, not the cause.
_SENTENCES: Dict[str, str] = {
    ErrorCode.UNAVAILABLE: N_("Joining in the app isn’t available right now: you can join "
                              "on the EINUNDZWANZIG website instead."),
    ErrorCode.OFFLINE: failures.CHECK_CONNECTION,
    ErrorCode.TIMEOUT: N_("EINUNDZWANZIG took too long to answer: check your internet "
                          "connection and try again."),
    ErrorCode.UNAUTHORIZED: N_("EINUNDZWANZIG couldn’t confirm it’s you: let this computer "
                               "set its date and time automatically, then try again."),
    ErrorCode.VALIDATION: N_("Some details need another look: check the highlighted fields "
                             "and try again."),
    ErrorCode.NOT_FOUND: N_("EINUNDZWANZIG has no application from this account yet: send "
                            "your membership application first."),
    ErrorCode.CONFLICT: N_("Your membership changed in the meantime: try again to see where "
                           "it stands now."),
    ErrorCode.SERVER: N_("EINUNDZWANZIG had a problem just now: try again in a few minutes."),
    ErrorCode.SIGNER_DECLINED: failures.SIGNER_DECLINED,
    ErrorCode.SIGNER_UNREACHABLE: failures.SIGNER_SILENT,
    ErrorCode.BAD_RESPONSE: N_("MyEditor couldn’t read the answer from EINUNDZWANZIG: try "
                               "again later, or check for a MyEditor update."),
}
_VALIDATION_UNNAMED = N_("Some details need another look: check them and try again.")
# What the server said, kept for Details only, and only this much of it.
_DETAILS_MAX = 300


def failure_sentence(error: ApiError) -> str:
    """Why ``error``'s request did not work and what to do, in one
    sentence (translated). No protocol words, status numbers or the
    server's own text: the person reading it is joining a club, not
    debugging a client (the server's text is :func:`failure_details`)."""
    code = getattr(error, "code", "")
    if code == ErrorCode.RATE_LIMITED:
        return _wait_words(getattr(error, "retry_after", None))
    if code == ErrorCode.VALIDATION and not getattr(error, "field_errors", None):
        return _(_VALIDATION_UNNAMED)
    sentence = _SENTENCES.get(code, _SENTENCES[ErrorCode.BAD_RESPONSE])
    return _(sentence)


def failure_details(error: ApiError) -> str:
    """What the server said of ``error``, for Details: its code and status,
    and its own words, bounded (they come from elsewhere)."""
    status = getattr(error, "status", None)
    said = " ".join(str(getattr(error, "message", "") or "").split())[:_DETAILS_MAX]
    head = " ".join(part for part in (str(getattr(error, "code", "") or ""),
                                       f"({status})" if status else "") if part)
    return ": ".join(part for part in (head, said) if part)


def _field_reasons(error: ApiError, name: str) -> List[str]:
    reasons = (getattr(error, "field_errors", None) or {}).get(name)
    return [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []


def handle_field_message(error: ApiError) -> Optional[str]:
    """A short inline message for the name field, or None.

    The server's wording is a framework default that may be English or
    German; it is read for its meaning and replaced with plain words.
    """
    reasons = _field_reasons(error, "nip05_handle")
    if not reasons:
        return None
    text = " ".join(reasons).lower()
    if any(word in text for word in ("taken", "vergeben", "exists", "already", "bereits")):
        return _("That name is taken. Try another one.")
    if any(word in text for word in ("255", "greater than", "too long", "max", "lang")):
        return _("Use {count} characters or fewer.").format(count=NIP05_HANDLE_MAX_LENGTH)
    if any(word in text for word in ("format", "invalid", "ungültig", "lowercase", "characters")):
        return _HANDLE_CHARACTERS
    return _("This name cannot be used. Try another one.")


def field_message(error: ApiError, name: str) -> Optional[str]:
    """A short inline message for any application field, or None."""
    if name == "nip05_handle":
        return handle_field_message(error)
    reasons = _field_reasons(error, name)
    if not reasons:
        return None
    if name == "email":
        return _("Enter a valid email address.")
    if name == "application_text":
        return _message_too_long()
    if name == "statutes_accepted":
        return _("Agree to the statutes to continue.")
    return _("This entry was not accepted.")
