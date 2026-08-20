# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the Blossom error taxonomy and its user-facing copy.

Codes travel across signal boundaries and settle into files, so they
must stay plain strings equal to their own names. The copy is part of
the product: sentence case, no "we", no em-dash, no Qt transport string
leaking through.
"""

from __future__ import annotations

import re

import pytest

from nostr.blossom.client import BlossomError
from nostr.blossom.errors import ERROR_CODES, friendly_message, sanitize_reason
from nostr.blossom.store import _format_err


ALL_CODES = [
    value for name, value in vars(ERROR_CODES).items()
    if not name.startswith("_") and isinstance(value, str)
]


def test_every_code_is_its_own_name():
    for name, value in vars(ERROR_CODES).items():
        if name.startswith("_") or not isinstance(value, str):
            continue
        assert value == name


def test_every_code_has_friendly_copy():
    assert len(ALL_CODES) == 14
    for code in ALL_CODES:
        message = friendly_message(code)
        assert message
        assert message[0].isupper()
        assert message.endswith(".")


def test_unknown_code_still_says_something():
    assert friendly_message("NO_SUCH_CODE")
    assert friendly_message("")


def test_copy_never_uses_we_or_an_em_dash():
    for code in ALL_CODES + ["NO_SUCH_CODE"]:
        message = friendly_message(code)
        # Escaped rather than literal so the banned character appears
        # nowhere in the tree, including in the test that forbids it.
        assert "\u2014" not in message
        assert not re.search(r"\bwe\b", message, re.IGNORECASE)


def test_raw_transport_text_is_never_echoed():
    message = friendly_message(
        ERROR_CODES.UPLOAD_FAILED,
        "Error transferring https://x.example - server replied: 502",
    )
    assert "502" not in message
    assert message == friendly_message(ERROR_CODES.UPLOAD_FAILED)


# --------------------------------------------------------------------------- #
# X-Reason: operator-authored text, rendered in a label
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, expected", [
    (b"Quota exceeded for this account", "Quota exceeded for this account"),
    (b"  spaced   out  ", "spaced out"),
    (b"line one\r\nline two", "line one line two"),
    (b"bell\x07and\x00nul", "bell and nul"),
    (b"", ""),
    (b"   ", ""),
    (None, ""),
    (b"\xff\xfe not utf-8", "�� not utf-8"),
])
def test_sanitize_reason_units(raw, expected):
    assert sanitize_reason(raw) == expected


def test_sanitize_reason_caps_length():
    assert len(sanitize_reason(b"x" * 5000)) == 200


def test_a_header_cannot_forge_a_second_line():
    """R7: this string lands in a label, and a raw header can carry
    CR LF. A newline must never survive to make a second line."""
    detail = sanitize_reason(b"Denied\r\nX-Injected: 1")
    message = friendly_message(ERROR_CODES.AUTH_REJECTED, detail=detail)
    assert "\n" not in message
    assert "\r" not in message
    assert message.count(".") >= 1
    assert "X-Injected" in message      # shown as text, not as a header


def test_detail_is_appended_as_its_own_sentence():
    message = friendly_message(ERROR_CODES.SERVER_TOO_LARGE,
                               detail="Maximum is 10 MiB")
    assert message.startswith(friendly_message(ERROR_CODES.SERVER_TOO_LARGE))
    assert message.endswith("Maximum is 10 MiB.")


def test_detail_never_replaces_the_mapped_copy():
    for code in ALL_CODES:
        assert friendly_message(code, detail="whatever").startswith(
            friendly_message(code)
        )


# --------------------------------------------------------------------------- #
# store._format_err
# --------------------------------------------------------------------------- #

def test_format_err_never_leaks_a_qt_transport_string():
    err = BlossomError(
        "Error transferring https://x.example - server replied: 502",
        status=502,
    )
    text = _format_err(err)
    assert "502" not in text
    assert "Error transferring" not in text
    assert text == friendly_message(ERROR_CODES.UPLOAD_FAILED)


def test_format_err_uses_the_code_and_carries_the_detail():
    err = BlossomError("HTTP 413", status=413,
                       code=ERROR_CODES.SERVER_TOO_LARGE,
                       detail="Maximum is 10 MiB")
    assert _format_err(err) == friendly_message(
        ERROR_CODES.SERVER_TOO_LARGE, detail="Maximum is 10 MiB"
    )


@pytest.mark.parametrize("reason", [
    "signer rejected the Blossom auth event: user declined",
    "Connect a Nostr signer first.",
])
def test_signer_strings_pass_through_byte_identical(reason):
    """The asset manager still classifies signer failures by prefix.
    Rewording them here would silently reclassify every one of them."""
    assert _format_err(reason) == reason
