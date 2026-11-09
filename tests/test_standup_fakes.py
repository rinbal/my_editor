# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fake STANDUP server (tests/standup_fakes.py) checks what the real
one checks, so a client that only satisfies its own reader fails here.

Its state machine is the contract's transition table (the fixture's
``transitions``), its NIP-98 checks accept the fixture's signed action
and refuse every way a credential can be wrong, and its faults do what
they say: a connection dropped before the request leaves nothing behind,
one dropped after it leaves the submission stored."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from nostr import events, nip98
from tests.membership_fakes import FakeClock
from tests.standup_fakes import (
    DROP_AFTER,
    DROP_BEFORE,
    SERVER,
    FakeStandupServer,
    Fault,
    StandupNam,
    submission_for,
    transition,
)
from tests.standup_vectors import NOW, VECTORS, sk


@pytest.mark.parametrize("row", VECTORS["transitions"],
                         ids=lambda r: f"{r['action']}-{r['from']}-{r['role']}")
def test_the_state_machine_is_the_contracts(row):
    assert transition(row["from"], row["action"], row["role"]) == row["to"]


def test_the_fixtures_signed_action_passes_the_same_check():
    action = VECTORS["signed_action"]
    event = nip98.check_auth_header(action["authorization"], url=action["url"],
                                    method=action["method"],
                                    body=action["body"].encode("utf-8"), now=NOW)
    assert event == action["event"]


# -- raw requests --------------------------------------------------------------

def _setup():
    clock = FakeClock(NOW)
    server = FakeStandupServer(clock)
    return clock, server, StandupNam(responder=server)


def _send(nam, verb, path, body=None, authorization=None, content_type=b"application/json"):
    request = QNetworkRequest(QUrl(SERVER + path))
    if authorization is not None:
        request.setRawHeader(b"Authorization", authorization)
    if body is not None and content_type:
        request.setRawHeader(b"Content-Type", content_type)
    if verb == "GET":
        reply = nam.get(request)
    elif verb == "POST":
        reply = nam.post(request, body or b"")
    else:
        reply = nam.sendCustomRequest(request, verb.encode("ascii"), body or b"")
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
    raw = bytes(reply.readAll())
    return status, (json.loads(raw) if raw else None), reply


def _auth(secret, url, verb, body, *, now=NOW, edit=None):
    unsigned = nip98.build_unsigned_auth_event(url, verb, body, now=now)
    if edit is not None:
        edit(unsigned)
    return nip98.authorization_header_value(events.sign_event(unsigned, secret))


def _session(nam, secret=None):
    secret = secret or sk("writer")
    url = SERVER + "/api/session"
    status, data, _ = _send(nam, "POST", "/api/session", b"{}", _auth(secret, url, "POST", b"{}"))
    assert status == 200, data
    return data["token"]


def test_a_signed_action_with_everything_right_opens_a_session():
    _clock, server, nam = _setup()
    token = _session(nam)
    assert len(token) == 64 and len(server.sessions) == 1
    # Only the token's hash is kept.
    assert token not in json.dumps(list(server.sessions))


@pytest.mark.parametrize("case", ["url", "method", "payload", "no payload", "expired",
                                  "signature", "replay", "bearer", "nothing"])
def test_every_way_a_signed_action_can_be_wrong_is_refused(case):
    _clock, server, nam = _setup()
    url, body = SERVER + "/api/session", b"{}"
    auth = _auth(sk("writer"), url, "POST", body)
    if case == "url":
        auth = _auth(sk("writer"), SERVER + "/api/sessions", "POST", body)
    elif case == "method":
        auth = _auth(sk("writer"), url, "PUT", body)
    elif case == "payload":
        auth = _auth(sk("writer"), url, "POST", b'{"a":1}')
    elif case == "no payload":
        auth = _auth(sk("writer"), url, "POST", None)
        body = None
    elif case == "expired":
        auth = _auth(sk("writer"), url, "POST", body, now=NOW - 61)
    elif case == "signature":
        event = json.loads(__import__("base64").b64decode(auth[len(b"Nostr "):]))
        event["sig"] = ("0" if event["sig"][0] != "0" else "1") + event["sig"][1:]
        auth = nip98.authorization_header_value(event)
    elif case == "replay":
        assert _send(nam, "POST", "/api/session", body, auth)[0] == 200
    elif case == "bearer":
        auth = b"Bearer " + b"ab" * 32
    elif case == "nothing":
        auth = None
    status, data, _ = _send(nam, "POST", "/api/session", body, auth)
    assert status == 401, data
    expected = {"nothing": "auth_required"}.get(case, "auth_invalid")
    assert data["error"]["code"] == expected
    if case == "expired":
        assert data["error"]["detail"]["reason"] == "expired"


def test_a_body_that_is_not_declared_json_is_refused():
    _clock, _server, nam = _setup()
    url = SERVER + "/api/session"
    status, _data, _ = _send(nam, "POST", "/api/session", b"{}",
                             _auth(sk("writer"), url, "POST", b"{}"), content_type=b"text/plain")
    assert status == 415


def test_sessions_expire_and_an_unknown_token_is_refused():
    clock, server, nam = _setup()
    token = _session(nam).encode("ascii")
    assert _send(nam, "GET", "/api/me/submissions", authorization=b"Bearer " + token)[0] == 200
    clock.advance(12 * 3600 + 1)
    status, data, _ = _send(nam, "GET", "/api/me/submissions", authorization=b"Bearer " + token)
    assert (status, data["error"]["code"]) == (401, "session_expired")
    status, data, _ = _send(nam, "GET", "/api/me/submissions",
                            authorization=b"Bearer " + b"cd" * 32)
    assert (status, data["error"]["code"], data["error"]["detail"]["reason"]) == \
        (401, "auth_invalid", "session")


# -- submissions, raw ------------------------------------------------------------

def _submit(nam, body_dict, secret=None):
    secret = secret or sk("writer")
    body = json.dumps(body_dict).encode("utf-8")
    url = SERVER + "/api/submissions"
    return _send(nam, "POST", "/api/submissions", body, _auth(secret, url, "POST", body))


def test_a_submission_is_stored_with_a_receipt_and_the_same_body_returns_it():
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    status, data, _ = _submit(nam, prepared.body)
    assert status == 201, data
    receipt = data["receipt"]
    assert receipt["submission_id"] == prepared.body["submission_id"]
    assert receipt["state"] == "submitted" and receipt["seq"] == 1
    status, again, _ = _submit(nam, prepared.body)
    assert status == 200 and again["receipt"] == receipt
    assert len(server.submissions) == 1


def test_a_submission_signed_by_someone_else_than_the_writer_is_refused():
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    status, data, _ = _submit(nam, prepared.body, secret=sk("contributor"))
    assert (status, data["error"]["code"]) == (422, "invalid")
    assert server.submissions == {}


def test_a_dropped_connection_before_sending_leaves_nothing_behind():
    _clock, server, nam = _setup()
    server.inject("submit", DROP_BEFORE)
    status, _data, reply = _submit(nam, submission_for(sk("writer")).body)
    assert status == 0
    assert reply.error() == QNetworkReply.NetworkError.ConnectionRefusedError
    assert server.submissions == {} and server.accepted == []


def test_a_dropped_answer_leaves_the_submission_stored():
    _clock, server, nam = _setup()
    server.inject("submit", DROP_AFTER)
    status, _data, reply = _submit(nam, submission_for(sk("writer")).body)
    assert status == 0
    assert reply.error() == QNetworkReply.NetworkError.RemoteHostClosedError
    assert len(server.submissions) == 1


def test_an_injected_refusal_carries_its_code_and_wait():
    _clock, server, nam = _setup()
    server.inject("submit", Fault(429, "rate_limited", retry_after=120))
    status, data, reply = _submit(nam, submission_for(sk("writer")).body)
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert bytes(reply.rawHeader("Retry-After")) == b"120"
    assert server.submissions == {}


def test_a_fourth_open_submission_and_a_paused_publication_are_refused():
    _clock, server, nam = _setup()
    for n in range(3):
        assert _submit(nam, submission_for(sk("writer"), d=f"text-{n}").body)[0] == 201
    status, data, _ = _submit(nam, submission_for(sk("writer"), d="text-3").body)
    assert (status, data["error"]["code"]) == (403, "limit_open")
    server.publication().paused = True
    status, data, _ = _submit(nam, submission_for(sk("contributor"), d="text-4").body,
                              secret=sk("contributor"))
    assert (status, data["error"]["code"]) == (403, "paused")
