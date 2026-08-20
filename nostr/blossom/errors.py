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

import re


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
    UPLOAD_FAILED = "UPLOAD_FAILED"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    SIGNER_IDENTITY_MISMATCH = "SIGNER_IDENTITY_MISMATCH"

    # Server verdicts. Distinct from the transport codes above because
    # the server answered: it just refused, and the reasons need
    # different copy and a different retry policy.
    HASH_MISMATCH = "HASH_MISMATCH"
    SERVER_TOO_LARGE = "SERVER_TOO_LARGE"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REJECTED = "AUTH_REJECTED"


# Friendly copy per code. Sentence case, no "we", no blame, says what
# happened and what to do next. An image never disappears from a
# document because of any of these, and the copy says so where it is
# the user's first question.
_FRIENDLY = {
    ERROR_CODES.REDIRECT_REFUSED: (
        "The server redirected this request. For safety it was not sent "
        "again. Try a different server."
    ),
    ERROR_CODES.UNSAFE_URL: (
        "That address is not allowed. Only regular web links can be used "
        "here."
    ),
    ERROR_CODES.TOO_LARGE: (
        "The transfer was larger than allowed and was stopped."
    ),
    ERROR_CODES.UNSUPPORTED_FORMAT: (
        "That file format cannot be shown here. The file itself is "
        "unaffected."
    ),
    ERROR_CODES.HOST_MISMATCH: (
        "The signed authorization does not match this server. Nothing was "
        "sent."
    ),
    ERROR_CODES.SIGNER_REJECTED: (
        "The signer declined the request. Approve it in the signer app and "
        "try again."
    ),
    ERROR_CODES.UPLOAD_FAILED: (
        "The upload did not finish. The image stays in your document and "
        "can be retried."
    ),
    ERROR_CODES.NETWORK_UNAVAILABLE: (
        "The server could not be reached. The image stays in your document "
        "and can be uploaded later."
    ),
    ERROR_CODES.SIGNER_IDENTITY_MISMATCH: (
        "The signer answered for a different identity. Nothing was sent."
    ),
    ERROR_CODES.HASH_MISMATCH: (
        "The server described a different file than the one that was sent. "
        "Nothing was stored and the file stays in your document."
    ),
    ERROR_CODES.SERVER_TOO_LARGE: (
        "That server refused the file for being too large. Try another "
        "server, or a smaller file."
    ),
    ERROR_CODES.PAYMENT_REQUIRED: (
        "That server asks for payment for uploads. The file stays in your "
        "document. Try another server."
    ),
    ERROR_CODES.RATE_LIMITED: (
        "That server is asking for a slower pace. Wait a moment and try "
        "again."
    ),
    ERROR_CODES.AUTH_REJECTED: (
        "That server did not accept the signed authorization. Check the "
        "signer is connected, then try again."
    ),
}

_FALLBACK = "Something went wrong with that media request. Nothing was lost."

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

    ``raw`` is the diagnostic string the transport produced. It is
    accepted so callers can pass it without branching, and deliberately
    not rendered: Qt error strings are not user copy.

    ``detail`` is the server's own sanitized ``X-Reason``. When present
    it is appended as its own sentence, so the mapped copy still leads
    and the operator's wording never replaces it.
    """
    message = _FRIENDLY.get(code) or _FALLBACK
    reason = sanitize_reason(detail)
    if not reason:
        return message
    if not reason.endswith((".", "!", "?")):
        reason = f"{reason}."
    return f"{message} {reason[0].upper()}{reason[1:]}"
