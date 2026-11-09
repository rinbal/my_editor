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
from tests.standup_vectors import NOW, VECTORS, pk, sk


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


OFF_CURVE = "ff" * 32                     # 64 hex digits, no point on secp256k1


def _with_contributor(**changes):
    prepared = submission_for(sk("writer"), participants=(pk("contributor"),))
    return dict(prepared.body, **changes)


def _wraps(body, **changes):
    wraps = dict(body["wraps"])
    for name, value in changes.items():
        if value is None:
            wraps.pop(name, None)
        else:
            wraps[name] = value
    return wraps


@pytest.mark.parametrize("change, problem", [
    (lambda b: dict(b, p_sub=OFF_CURVE), "p_sub"),
    (lambda b: dict(b, participants=[pk("writer")]), "participants"),
    (lambda b: dict(b, participants=[pk("contributor"), pk("contributor")]), "participants"),
    (lambda b: dict(b, participants=[OFF_CURVE]), "participants"),
])
def test_keys_and_participants_are_checked_as_the_server_does(change, problem):
    # RG-M1 13: on the curve, not merely 64 hex digits; no participant
    # twice, none who is the writer.
    _clock, server, nam = _setup()
    status, data, _ = _submit(nam, change(_with_contributor()))
    assert (status, data["error"]["code"]) == (422, "invalid")
    assert data["error"]["detail"]["problem"] == problem and server.submissions == {}


@pytest.mark.parametrize("wraps", [
    lambda b: _wraps(b, **{pk("contributor"): None}),                     # a wrap missing
    lambda b: _wraps(b, **{pk("editor"): b["wraps"]["writer"]}),          # one for nobody
    lambda b: _wraps(b, writer=dict(b["wraps"]["writer"], eph=OFF_CURVE)),
    lambda b: _wraps(b, writer=dict(b["wraps"]["writer"], ct=b["wraps"]["writer"]["ct"] + "A")),
])
def test_a_wrap_per_recipient_name_exactly_each_of_the_right_shape(wraps):
    _clock, server, nam = _setup()
    body = _with_contributor()
    status, data, _ = _submit(nam, dict(body, wraps=wraps(body)))
    assert (status, data["error"]["code"]) == (422, "wrap_invalid")


def test_without_anyone_who_could_decide_a_submission_is_refused():
    # 12.4 (5c11475): the only owner is named as a contributor.
    _clock, server, nam = _setup()
    owner = next(iter(server.publication().members))
    server.publication().members = {owner: "owner"}
    prepared = submission_for(sk("writer"), participants=(owner,))
    status, data, _ = _submit(nam, prepared.body)
    assert (status, data["error"]["code"]) == (409, "no_decider")


def test_a_revision_keeps_its_round_and_participants():
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    second = submission_for(sk("writer"), revision=2, s_sub=first.s_sub,
                            status_key=first.status_key, participants=(pk("contributor"),),
                            content="# Zwei")
    status, data, _ = _submit(nam, second.body)
    assert (status, data["error"]["detail"]["problem"]) == (422, "round")


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


# -- the timeline, as the backend pages it (feat/redaktion-backend fde04a3) -------------

def _size(item) -> int:
    return len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode())


def _lookup(nam, keys, since=0):
    body = json.dumps({"items": [{"status_key": k, "since": since} for k in keys]}).encode()
    return _send(nam, "POST", "/api/submissions/lookup", body)


def _two_submissions(server, nam):
    keys = []
    for name in ("writer", "contributor"):
        prepared = submission_for(sk(name))
        assert _submit(nam, prepared.body, secret=sk(name))[0] == 201
        record = server.submissions[prepared.body["submission_id"]]
        record.timeline.append(dict(record.timeline[0], seq=2))      # a second item
        keys.append(prepared.status_key)
    return keys


def test_a_lookup_shares_one_byte_budget_across_its_items():
    # backend tests/test_editorial_limits.py, the same name.
    _clock, server, nam = _setup()
    keys = _two_submissions(server, nam)
    whole = _lookup(nam, keys)[1]["items"]
    assert [(len(i["timeline"]), i["more"]) for i in whole] == [(2, False), (2, False)]
    server.timeline_bytes = sum(_size(i) for i in whole[0]["timeline"])
    shared = _lookup(nam, keys)[1]["items"]
    assert [(len(i["timeline"]), i["more"]) for i in shared] == [(2, False), (0, True)]


def test_the_byte_budget_ends_a_page_before_the_item_that_does_not_fit():
    # backend tests/test_editorial_limits.py, the same name, for GET.
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    record = server.submissions[prepared.body["submission_id"]]
    for seq in (2, 3, 4):
        record.timeline.append(dict(record.timeline[0], seq=seq))
    token = _session(nam)
    path = f"/api/submissions/{prepared.body['submission_id']}"
    whole = _send(nam, "GET", path, authorization=f"Bearer {token}".encode())[1]["timeline"]
    sizes = [_size(i) for i in whole]
    server.timeline_bytes = sum(sizes[:3]) + sizes[3] - 1
    page = _send(nam, "GET", path, authorization=f"Bearer {token}".encode())[1]
    assert page["more"] is True and [i["seq"] for i in page["timeline"]] == [1, 2, 3]


def test_a_revision_event_carries_no_signed_action_its_revision_has_it():
    # backend core/editorial_views.py item_view and REVISION_EVENTS.
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    item, = _lookup(nam, [prepared.status_key])[1]["items"]
    assert item["timeline"][0]["event"]["name"] == "submitted"
    assert item["timeline"][0]["event"]["signed"] is None


KEY = "ab" * 32


@pytest.mark.parametrize("body, detail", [
    ({"items": [{"status_key": KEY}]}, {"fields": ["since"]}),
    ({"items": [{"status_key": KEY, "since": 0, "x": 1}]}, {"fields": ["x"]}),
    ({"items": [], "x": 1}, {"fields": ["x"]}),
    ({}, {"fields": ["items"]}),
    ({"items": []}, {"field": "items"}),
    ({"items": [{"status_key": KEY, "since": 0}] * 2}, {"field": "items"}),
    ({"items": [{"status_key": f"{n:064x}", "since": 0} for n in range(51)]},
     {"field": "items"}),
    ({"items": ["x"]}, {"field": "items"}),
    ({"items": [{"status_key": "AB" * 32, "since": 0}]}, {"field": "status_key"}),
    ({"items": [{"status_key": KEY, "since": -1}]}, {"field": "since"}),
    ({"items": [{"status_key": KEY, "since": True}]}, {"field": "since"}),
], ids=["since missing", "unknown in an item", "unknown field", "no items", "empty",
        "a key twice", "51 keys", "not an object", "upper case", "negative", "a boolean"])
def test_a_lookup_is_checked_as_the_backend_checks_it(body, detail):
    # backend api/submission_reads.py _lookup_items and core/editorial.py.
    _clock, _server, nam = _setup()
    status, data, _ = _send(nam, "POST", "/api/submissions/lookup", json.dumps(body).encode())
    assert (status, data["error"]["code"], data["error"]["detail"]) == (422, "invalid", detail)


def test_lookups_from_one_address_are_limited_per_minute_and_a_large_body_refused():
    clock, _server, nam = _setup()
    asked = json.dumps({"items": [{"status_key": KEY, "since": 0}]}).encode()
    answers = [_send(nam, "POST", "/api/submissions/lookup", asked) for _ in range(31)]
    assert [a[0] for a in answers[:30]] == [200] * 30
    status, data, reply = answers[30]
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert int(bytes(reply.rawHeader("Retry-After"))) == 60 - NOW % 60
    clock.advance(60)
    big = json.dumps({"items": [{"status_key": KEY, "since": 0}], "x": "a" * 16384}).encode()
    status, data, _ = _send(nam, "POST", "/api/submissions/lookup", big)
    assert (status, data["error"]["code"]) == (413, "too_large")


# -- each channel counts apart (feat/redaktion ceac858, backend fde04a3) ---------------

def _with_a_note(server, nam):
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    record = server.submissions[prepared.body["submission_id"]]
    server._event(record, "taken", None)                                  # seq 2
    server._append(record, {"type": "internal", "wrap": {"id": "1" * 64}, "event": None})
    return prepared, record


def _read(nam, secret, sid, query=""):
    token = _session(nam, secret)
    return _send(nam, "GET", f"/api/submissions/{sid}{query}",
                 authorization=f"Bearer {token}".encode())


def test_the_timeline_is_read_from_a_cursor_in_pages():
    # backend tests/test_editorial_reading.py, the same name.
    _clock, server, nam = _setup()
    prepared, record = _with_a_note(server, nam)
    sid = prepared.body["submission_id"]

    def page(query=""):
        return [(i["seq"], i["type"]) for i in _read(nam, sk("editor"), sid, query)[1]["timeline"]]

    assert page() == [(1, "event"), (2, "event"), (1, "internal")]
    assert page("?since=2") == [(1, "internal")]
    assert page("?since=2&since_internal=1") == []
    assert page("?since_internal=1") == [(1, "event"), (2, "event")]
    server.timeline_per_answer["get"] = 2
    assert page() == [(1, "event"), (2, "event")]


def test_an_internal_note_leaves_no_trace_for_participants():
    # backend tests/test_editorial_acceptance.py: last_internal_seq for members only.
    clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    sid = prepared.body["submission_id"]
    before = _read(nam, sk("writer"), sid)[1]["submission"]
    clock.advance(5)
    server._append(server.submissions[sid], {"type": "internal", "wrap": {}, "event": None})
    after = _read(nam, sk("writer"), sid)[1]
    assert after["submission"] == before and after["timeline"][-1]["type"] == "event"
    member = _read(nam, sk("editor"), sid)[1]["submission"]
    assert (member["last_internal_seq"], member["activity_at"]) == (1, NOW + 5)
    assert member["last_seq"] == before["last_seq"] == 1


@pytest.mark.parametrize("query", ["?since=-1", "?since=abc", "?revision=0", "?revision=2",
                                   "?since=" + "9" * 17, "?since_internal=x"])
def test_query_numbers_out_of_range_are_refused(query):
    # backend tests/test_editorial_reading.py, the same name.
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    status, data, _ = _read(nam, sk("writer"), prepared.body["submission_id"], query)
    assert (status, data["error"]["code"]) == (422, "invalid")


def test_strangers_learn_nothing():
    # backend tests/test_editorial_reading.py: 404 not_found, not 403.
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    status, data, _ = _read(nam, sk("contributor"), prepared.body["submission_id"])
    assert (status, data["error"]["code"]) == (404, "not_found")
