# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The words around joining, and the checks that run before anything is signed.

Alerts (:func:`humanize`), inline field messages, and the input checks
that spare the user a signer prompt for a request certain to be refused.
The person reading these is joining a club, not debugging a client, so
the copy says what happened and what to do next, and nothing else.

Pure: no Qt, no network.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from .models import ApiError, ErrorCode


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

_HANDLE_CHARACTERS = (
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
        return "Enter a name."
    if len(value) > NIP05_HANDLE_MAX_LENGTH:
        return f"Use {NIP05_HANDLE_MAX_LENGTH} characters or fewer."
    if _HANDLE_ALLOWED.match(value):
        return None
    if "@" in value:
        return "Enter only the part before the @ sign."
    if any(ch.isspace() for ch in value):
        return "Spaces are not allowed. " + _HANDLE_CHARACTERS
    if _HANDLE_ALLOWED.match(value.lower()):
        return "Use lowercase letters only."
    return _HANDLE_CHARACTERS


def email_problem(address: str) -> Optional[str]:
    """A plain-words reason an e-mail address will be refused, or None."""
    value = address if isinstance(address, str) else ""
    if len(value) > EMAIL_MAX_LENGTH:
        return f"Use an address of {EMAIL_MAX_LENGTH} characters or fewer."
    if not _EMAIL_SHAPE.match(value):
        return "Enter a valid email address."
    return None


def application_text_problem(text: str) -> Optional[str]:
    """A plain-words reason the application message will be refused, or None."""
    if isinstance(text, str) and len(text) > APPLICATION_TEXT_MAX_LENGTH:
        return f"Keep your message to {APPLICATION_TEXT_MAX_LENGTH:,} characters or fewer."
    return None


# --------------------------------------------------------------------------- #
# Plain-language copy                                                          #
# --------------------------------------------------------------------------- #

def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _wait_advice(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "Wait a few minutes, then try again."
    if seconds < 60:
        return f"Wait {_plural(seconds, 'second')}, then try again."
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"Wait about {_plural(minutes, 'minute')}, then try again."
    hours = math.ceil(minutes / 60)
    if hours < 24:
        return f"Try again in about {_plural(hours, 'hour')}."
    return "Try again tomorrow."


_COPY: Dict[str, Tuple[str, str]] = {
    ErrorCode.UNAVAILABLE: (
        "Joining in the app isn't available right now",
        "You can join on the EINUNDZWANZIG website instead.",
    ),
    ErrorCode.OFFLINE: (
        "Cannot reach EINUNDZWANZIG",
        "Check your internet connection and try again.",
    ),
    ErrorCode.TIMEOUT: (
        "EINUNDZWANZIG is taking too long to respond",
        "Check your internet connection and try again in a moment.",
    ),
    ErrorCode.UNAUTHORIZED: (
        "EINUNDZWANZIG could not confirm it is you",
        "Make sure the date and time on this computer are set "
        "automatically, then try again. If it keeps happening, check for "
        "a MyEditor update.",
    ),
    ErrorCode.VALIDATION: (
        "Some details need another look",
        "Check the highlighted fields and try again.",
    ),
    ErrorCode.NOT_FOUND: (
        "No application on file yet",
        "EINUNDZWANZIG has nothing on file for this account yet. Send your "
        "membership application first. If you already did, reload and "
        "try again.",
    ),
    ErrorCode.CONFLICT: (
        "Your membership changed in the meantime",
        "Reload to see where things stand, then try again.",
    ),
    ErrorCode.SERVER: (
        "EINUNDZWANZIG is having trouble right now",
        "Try again in a few minutes.",
    ),
    ErrorCode.SIGNER_DECLINED: (
        "The request was not approved",
        "Your signer app did not approve it. Try again, and approve the "
        "request when your signer app asks.",
    ),
    ErrorCode.SIGNER_UNREACHABLE: (
        "Your signer did not answer",
        "Open your signer app, make sure it is running, and try again.",
    ),
    ErrorCode.BAD_RESPONSE: (
        "Something went wrong",
        "MyEditor could not understand the answer from EINUNDZWANZIG. Try "
        "again later. If it keeps happening, check for a MyEditor update.",
    ),
}


def humanize(error: ApiError) -> Tuple[str, str]:
    """``(title, message)`` for an alert about ``error``.

    The title says what happened, the message says what to do next. No
    protocol words, status numbers or internals: the person reading it
    is joining a club, not debugging a client.
    """
    code = getattr(error, "code", "")
    if code == ErrorCode.RATE_LIMITED:
        return ("Too many attempts", _wait_advice(getattr(error, "retry_after", None)))
    if code == ErrorCode.VALIDATION and not getattr(error, "field_errors", None):
        return (_COPY[code][0], "Check your details and try again.")
    return _COPY.get(code, _COPY[ErrorCode.BAD_RESPONSE])


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
        return "That name is taken. Try another one."
    if any(word in text for word in ("255", "greater than", "too long", "max", "lang")):
        return f"Use {NIP05_HANDLE_MAX_LENGTH} characters or fewer."
    if any(word in text for word in ("format", "invalid", "ungültig", "lowercase", "characters")):
        return _HANDLE_CHARACTERS
    return "This name cannot be used. Try another one."


def field_message(error: ApiError, name: str) -> Optional[str]:
    """A short inline message for any application field, or None."""
    if name == "nip05_handle":
        return handle_field_message(error)
    reasons = _field_reasons(error, name)
    if not reasons:
        return None
    if name == "email":
        return "Enter a valid email address."
    if name == "application_text":
        return f"Keep your message to {APPLICATION_TEXT_MAX_LENGTH:,} characters or fewer."
    if name == "statutes_accepted":
        return "Agree to the statutes to continue."
    return "This entry was not accepted."
