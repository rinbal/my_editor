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

from nostr.blossom.client import BlossomClient, BlossomError
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
    assert len(ALL_CODES) == 23
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


def test_the_servers_own_words_never_reach_the_person():
    """BUD-01's X-Reason is a diagnostic: in English whatever the app's
    language, and in the server's terms ("pubkey", "blobs"). It is kept
    for the diagnostics, never shown."""
    for code in ALL_CODES:
        assert friendly_message(code, detail="Pubkey not in whitelist") == friendly_message(code)


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


def test_format_err_uses_the_code_alone():
    err = BlossomError("HTTP 413", status=413,
                       code=ERROR_CODES.SERVER_TOO_LARGE,
                       detail="Maximum is 10 MiB")
    assert _format_err(err) == friendly_message(ERROR_CODES.SERVER_TOO_LARGE)


# Words a person should never have to read in a media message.
_JARGON = ("blob", "sha", "hash", "http", "mib", "blossom", "authorization",
           "redirect", "described", "mirror", "endpoint", "token", "signed")


def test_copy_is_plain_words():
    for code in ALL_CODES + ["NO_SUCH_CODE"]:
        words = re.findall(r"[a-z0-9]+", friendly_message(code).lower())
        assert not set(words) & set(_JARGON), (code, friendly_message(code))


def test_copy_is_short():
    """One short sentence: what happened, then what to do (lessons 36)."""
    for code in ALL_CODES + ["NO_SUCH_CODE"]:
        message = friendly_message(code)
        assert len(message) <= 110, (code, message)
        assert message.endswith(".") and message.count(". ") == 0, (code, message)
        assert ": " in message, (code, message)


# --------------------------------------------------------------------------- #
# A server's refusal, as the person reads it from an upload
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status, code, words", [
    (401, "AUTH_REJECTED", "doesn’t accept your account"),
    (403, "AUTH_REJECTED", "doesn’t accept your account"),
    (413, "SERVER_TOO_LARGE", "too large"),
    (415, "UNSUPPORTED_TYPE", "doesn’t take this kind of file"),
    (429, "RATE_LIMITED", "busy"),
])
def test_a_refusal_says_the_remedy_for_it_in_plain_words(tmp_path, status, code, words):
    from PySide6.QtWidgets import QApplication
    from nostr.blossom.settings import BlossomSettings
    from nostr.blossom.store import MediaStore
    from tests.blossom_fakes import (FakeBlossomServer, FakeNam, FakeProfile,
                                     FakeSessionPool, SERVER)
    QApplication.instance() or QApplication([])
    settings = BlossomSettings(tmp_path / "s.json")
    settings.set_custom_servers([SERVER])
    server = FakeBlossomServer(SERVER, fail_with=status,
                               fail_reason="Pubkey not in whitelist; blobs of image/png refused")
    nam = FakeNam(None, responder=server)
    store = MediaStore(session_pool=FakeSessionPool(), profile_provider=FakeProfile,
                       settings=settings, client=BlossomClient(nam=nam))
    said, codes = [], []
    store.upload_failed.connect(lambda n, r: said.append(r))
    store.upload_failed_code.connect(lambda n, c: codes.append(c))
    store.upload_bytes(b"abc", name="a.png", mime_type="image/png")
    nam.settle()
    assert codes == [code]
    assert words in said[0]
    found = set(re.findall(r"[a-z0-9]+", said[0].lower()))
    assert not found & (set(_JARGON) | {"pubkey", "whitelist", "png"}), said[0]
