# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a refusal reads (nostr/standup/errors.py, contract 12.2): the
server's code when the status allows it, the status's own otherwise,
and whether the request may have been stored."""

from __future__ import annotations

import json

import pytest

from nostr import failures
from nostr.standup.errors import PASSING, Code, error_from_answer


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


@pytest.mark.parametrize("raw, own", [
    (None, True),                                           # the server's own envelope
    (b"<html>Not Found</html>", False),                     # a proxy's page
    (b"", False),
    (b'{"error": {"code": "not_found"}}', False),           # no "ok": false
    (b'{"ok": false, "error": {"code": "gone"}}', False),   # a code 404 does not carry
], ids=["envelope", "page", "empty", "no ok", "unknown code"])
def test_only_the_servers_own_envelope_is_its_own_not_found(raw, own):
    # Contract 2 (bde1274): a round is free only on the server's own
    # not_found; a proxy's 404 says nothing about what the server holds.
    error = error_from_answer(404, envelope("not_found") if raw is None else raw)
    assert (error.code, error.own) == (Code.NOT_FOUND, own)


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


@pytest.mark.parametrize("code, sentence", [
    (Code.OFFLINE, failures.CHECK_CONNECTION),
    (Code.TIMEOUT, failures.CHECK_CONNECTION),
    (Code.SERVER, failures.SERVER_TROUBLE),
    (Code.BAD_RESPONSE, failures.SERVER_TROUBLE),
])
def test_a_failure_is_said_by_its_cause_from_the_one_table(code, sentence):
    # RG-M2r3 6: failures.CAUSE_WORDS, the same on the sheet, in the
    # round step and in a refusal of a submission.
    from nostr.standup import words
    from nostr.standup.errors import cause_words
    assert cause_words(code) == sentence
    if code in PASSING:
        assert words.refusal(code) == sentence
