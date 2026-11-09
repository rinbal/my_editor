# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a refusal reads (nostr/standup/errors.py, contract 12.2): the
server's code when the status allows it, the status's own otherwise,
and whether the request may have been stored."""

from __future__ import annotations

import json

import pytest

from nostr.standup.errors import Code, error_from_answer


def envelope(code, message="Nicht möglich.", detail=None) -> bytes:
    return json.dumps({"ok": False, "error": {"code": code, "message": message,
                                              "detail": detail or {}}}).encode()


@pytest.mark.parametrize("status, code, expected", [
    (400, "bad_request", Code.BAD_REQUEST),
    (401, "session_expired", Code.SESSION_EXPIRED),
    (403, "limit_open", Code.LIMIT_OPEN),
    (404, "not_found", Code.NOT_FOUND),
    (405, "method_not_allowed", Code.METHOD_NOT_ALLOWED),
    (409, "stale_revision", Code.STALE_REVISION),
    (409, "no_decider", Code.NO_DECIDER),
    (413, "too_large", Code.TOO_LARGE),
    (422, "wrap_invalid", Code.WRAP_INVALID),
    (429, "rate_limited", Code.RATE_LIMITED),
    (500, "internal", Code.SERVER),
    # A code another status carries is not believed: the status's first.
    (403, "stale_revision", Code.FORBIDDEN),
    (409, "something_new", Code.CONFLICT),
    (415, "unsupported", Code.BAD_RESPONSE),
])
def test_the_code_is_one_the_status_may_carry(status, code, expected):
    assert error_from_answer(status, envelope(code)).code == expected


def test_a_proxys_page_still_has_its_status():
    error = error_from_answer(502, b"<html>Bad Gateway</html>")
    assert error.code == Code.SERVER and error.maybe_stored and error.message == ""


@pytest.mark.parametrize("status, stored", [(409, False), (429, False), (500, True), (504, True)])
def test_only_a_server_failure_may_have_stored_the_request(status, stored):
    assert error_from_answer(status, envelope("conflict")).maybe_stored is stored


def test_the_message_and_detail_are_kept_flat_and_clean():
    error = error_from_answer(401, envelope(
        "auth_invalid", "Anmeldung\nungültig",
        {"reason": "a\nb", "n": 3, "nested": {"x": 1}, "list": ["a", "b"]}))
    assert error.message == "Anmeldung ungültig"
    assert error.detail == {"reason": "a b", "n": 3, "list": ("a", "b")}
    assert error.reason == "a b"


def test_the_wait_is_passed_on():
    assert error_from_answer(429, envelope("rate_limited"), retry_after=30).retry_after == 30
