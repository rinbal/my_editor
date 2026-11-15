# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable error codes + friendly copy for Blossom.

Same shape as ``nostr.imports.errors``: a namespace of plain strings
(codes cross signal boundaries and settle into files, so not an enum),
a copy map, and one :func:`friendly_message`. Qt transport strings and
protocol jargon never reach the user; a code does, and the code picks
the sentence.

BUD-01 lets a server attach a human-readable ``X-Reason`` header to any
error response, and every status-code table repeats that clients MUST
NOT parse it for control flow. :func:`sanitize_reason` is the gate that
makes operator-authored text safe to put in a label; control flow stays
on the status and the code.

Codes are additive. Later work extends this module, never replaces it.
"""

from __future__ import annotations

import logging
import re

from i18n import N_, _

from ..bunker import is_signer_answer

_log = logging.getLogger(__name__)


class ERROR_CODES:
    """Namespace of stable error-code strings."""

    # Transport boundary.
    REDIRECT_REFUSED = "REDIRECT_REFUSED"
    UNSAFE_URL = "UNSAFE_URL"
    TOO_LARGE = "TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    HOST_MISMATCH = "HOST_MISMATCH"

    # Upload lifecycle.
    SIGNER_REJECTED = "SIGNER_REJECTED"
    # The signer app was never reached or never answered (asleep, closed,
    # a relay that would not carry the request): it refused nothing.
    SIGNER_SILENT = "SIGNER_SILENT"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    SIGNER_IDENTITY_MISMATCH = "SIGNER_IDENTITY_MISMATCH"
    NO_SIGNER = "NO_SIGNER"

    # Refused before anything was sent: the file, or where it would go.
    FILE_UNREADABLE = "FILE_UNREADABLE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    QUOTA_FULL = "QUOTA_FULL"
    NO_SERVER = "NO_SERVER"

    # Server verdicts. Distinct from the transport codes above because
    # the server answered: it just refused, and the reasons need
    # different copy and a different retry policy.
    HASH_MISMATCH = "HASH_MISMATCH"
    SERVER_TOO_LARGE = "SERVER_TOO_LARGE"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REJECTED = "AUTH_REJECTED"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    # A 409 that names a file the server already holds (X-Identical-Media):
    # it keeps one copy of a picture that looks the same, other bytes or
    # not (nostr.download). Another server takes it.
    LOOKALIKE_REFUSED = "LOOKALIKE_REFUSED"
    # No server could list the person's files, for a reason of its own
    # (not the connection, not the signer, not a refusal of the account).
    LIST_FAILED = "LIST_FAILED"


# One sentence per code: what happened, then what to do about it
# (lessons 36). No protocol words, no host names, no "we", no blame. The
# library shows these under a file and the editor in its status bar, so
# none of them assumes where the file is.
_TRY_AGAIN = N_("The upload didn’t finish: try again.")
_FRIENDLY = {
    ERROR_CODES.REDIRECT_REFUSED: _(
        "Your media server answered in a way MyEditor doesn’t trust: choose another one in "
        "Media Servers."
    ),
    ERROR_CODES.UNSAFE_URL: _("That address can’t be used: use a media server’s https address."),
    ERROR_CODES.TOO_LARGE: _(
        "A media server sent more than MyEditor accepts, so it was stopped: try again later."
    ),
    ERROR_CODES.UNSUPPORTED_FORMAT: _(
        "MyEditor can’t show this kind of file: open it in another app."),
    # Only a mis-wired request reaches this, never the person's doing.
    ERROR_CODES.HOST_MISMATCH: _(_TRY_AGAIN),
    ERROR_CODES.SIGNER_REJECTED: _(
        "Your signer app didn’t approve the request: check it and try again."
    ),
    ERROR_CODES.SIGNER_SILENT: _(
        "Your signer app didn’t answer: open it, make sure it’s running, and try again."
    ),
    ERROR_CODES.UPLOAD_FAILED: _(_TRY_AGAIN),
    ERROR_CODES.NETWORK_UNAVAILABLE: _(
        "Couldn’t reach your media server: check your internet connection and try again."
    ),
    ERROR_CODES.SIGNER_IDENTITY_MISMATCH: _(
        "Your signer app answered for another account, so nothing was sent: switch it and try "
        "again."
    ),
    ERROR_CODES.NO_SIGNER: _("No signer app is connected: connect it, then try again."),
    ERROR_CODES.FILE_UNREADABLE: _(
        "This file couldn’t be read: check that it is still there and try again."),
    ERROR_CODES.FILE_TOO_LARGE: _(
        "This file is larger than your media servers take: choose a smaller one."),
    ERROR_CODES.QUOTA_FULL: _(
        "Your media servers are full: delete files you don’t need, or add a server in Media "
        "Servers."
    ),
    ERROR_CODES.NO_SERVER: _("No media server is set up: add one in Media Servers."),
    # The check stays (client.py, replicate.py); the words are plain.
    ERROR_CODES.HASH_MISMATCH: _(
        "A media server returned the wrong file, so it wasn’t used: try again."
    ),
    ERROR_CODES.SERVER_TOO_LARGE: _(
        "This file is too large for your media server: choose a smaller one, or add a server "
        "in Media Servers."),
    ERROR_CODES.PAYMENT_REQUIRED: _(
        "Your media server asks for payment: choose another one in Media Servers."
    ),
    ERROR_CODES.RATE_LIMITED: _("Your media server is busy: try again later."),
    ERROR_CODES.AUTH_REJECTED: _(
        "This media server doesn’t accept your account: choose another one in Media Servers."
    ),
    ERROR_CODES.UNSUPPORTED_TYPE: _(
        "This media server doesn’t take this kind of file: choose another one in Media "
        "Servers."
    ),
    ERROR_CODES.LOOKALIKE_REFUSED: _(
        "Your media server keeps one copy of a picture like this: add another server in "
        "Media Servers."
    ),
    ERROR_CODES.LIST_FAILED: _(
        "Your media servers didn’t list your files: try again later."
    ),
}

_FALLBACK = _("Something went wrong with that media request, and nothing was lost: try again.")

# The signer's own words, for the importer's record of what it copied.
# The person reads the code's sentence above instead.
_SIGNER_REJECTED = N_("Your signer app declined the request to the media server: {reason}")


def signing_failure_code(reason) -> str:
    """The code for a signature that did not come: SIGNER_REJECTED when the
    signer app itself said no, SIGNER_SILENT when it was never reached or
    never answered, so the sentence sends the person where the problem is."""
    return ERROR_CODES.SIGNER_REJECTED if is_signer_answer(str(reason or "")) \
        else ERROR_CODES.SIGNER_SILENT


def signer_rejected(reason) -> str:
    """The sentence for a signer that refused to sign, with its own words."""
    return _(_SIGNER_REJECTED).format(reason=reason)


# A raw header can carry anything the operator typed, including CR LF.
# This string lands in a plain-text label, so newlines and control
# characters are removed rather than escaped.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")

_MAX_REASON_CHARS = 200


def sanitize_reason(raw) -> str:
    """Operator-authored text, made safe to put in a label.

    BUD-01: an error response "MAY include a human-readable header
    ``X-Reason`` that can be displayed to the user". It is remote input
    on an error path, so it is decoded leniently, stripped of control
    characters and newlines, collapsed and clipped. Returns ``""`` when
    nothing usable survives.
    """
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", errors="replace")
    else:
        text = str(raw)
    text = _CONTROL_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:_MAX_REASON_CHARS].strip()


def friendly_message(code: str, raw: str = "", *, detail: str = "") -> str:
    """Copy for ``code``, suitable to show a user directly.

    ``raw`` (the transport's diagnostic) and ``detail`` (the server's
    own ``X-Reason``) are accepted so callers can pass them without
    branching, and never rendered: the transport's words are not copy,
    and the server's are a diagnostic in its own terms and language
    (BUD-01). ``detail`` goes to the diagnostics log.
    """
    reason = sanitize_reason(detail)
    if reason:
        _log.info("media server said: %s (%s)", reason, code)
    return _FRIENDLY.get(code) or _FALLBACK
