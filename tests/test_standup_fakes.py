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

import base64
import hashlib
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
                         ids=lambda r: f"{r['action']}-{r['from']}-{r['role']}-{r['phase']}")
def test_the_state_machine_is_the_contracts(row):
    assert transition(row["from"], row["action"], row["role"], row["phase"]) == row["to"]


@pytest.mark.parametrize("action, role, phase", [
    ("published_by_writer", "writer", "confirm"),
    ("published_by_writer", "writer", "ready_for_release"),
    ("published_by_writer", "writer", None),
    ("published_by_release", "releaser", "writer_publishes"),
    ("published_by_release", "releaser", None),
])
def test_an_accepted_work_is_published_in_no_other_phase(action, role, phase):
    # backend core/editorial_flow.py (607c941): each publishing row names
    # its one phase; the fixture lists only the allowed ones.
    assert transition("accepted", action, role, phase) is None


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


@pytest.mark.parametrize("change, field", [
    (lambda b: dict(b, p_sub=OFF_CURVE), "p_sub"),
    (lambda b: dict(b, participants=[pk("writer")]), "participants"),
    (lambda b: dict(b, participants=[pk("contributor"), pk("contributor")]), "participants"),
    (lambda b: dict(b, participants=[OFF_CURVE]), "participants"),
])
def test_keys_and_participants_are_checked_as_the_server_does(change, field):
    # RG-M1 13: on the curve, not merely 64 hex digits; no participant
    # twice, none who is the writer (backend require_pubkey, _participants).
    _clock, server, nam = _setup()
    status, data, _ = _submit(nam, change(_with_contributor()))
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        422, "invalid", {"field": field})
    assert server.submissions == {}


@pytest.mark.parametrize("wraps, code, field", [
    (lambda b: _wraps(b, **{pk("contributor"): None}), "invalid", "wraps"),   # one missing
    (lambda b: _wraps(b, **{pk("editor"): b["wraps"]["writer"]}), "invalid", "wraps"),
    (lambda b: _wraps(b, writer=dict(b["wraps"]["writer"], eph=OFF_CURVE)),
     "wrap_invalid", "wraps.writer"),
    (lambda b: _wraps(b, writer=dict(b["wraps"]["writer"], ct=b["wraps"]["writer"]["ct"] + "A")),
     "wrap_invalid", "wraps.writer"),
    (lambda b: _wraps(b, writer=dict(b["wraps"]["writer"], ct="AgAA")), "wrap_invalid",
     "wraps.writer"),
], ids=["one missing", "one for nobody", "eph off the curve", "ct too long", "ct too short"])
def test_a_wrap_per_recipient_name_exactly_each_of_the_right_shape(wraps, code, field):
    # backend _wraps: the names wrong is invalid; a wrap of the wrong
    # shape is wrap_invalid naming it (RG-M2a 2).
    _clock, server, nam = _setup()
    body = _with_contributor()
    status, data, _ = _submit(nam, dict(body, wraps=wraps(body)))
    assert (status, data["error"]["code"]) == (422, code)
    assert data["error"]["detail"] == {"field": field}


def test_without_anyone_who_could_decide_a_submission_is_refused():
    # 12.4 (5c11475): the only owner is named as a contributor.
    _clock, server, nam = _setup()
    owner = next(iter(server.publication().members))
    server.publication().members = {owner: "owner"}
    prepared = submission_for(sk("writer"), participants=(owner,))
    status, data, _ = _submit(nam, prepared.body)
    assert (status, data["error"]["code"]) == (409, "no_decider")


def _next(first, content="# Zwei", revision=2, **kw):
    return submission_for(sk("writer"), revision=revision, s_sub=kw.pop("s_sub", first.s_sub),
                          status_key=first.status_key, content=content, **kw)


def test_kind_and_submission_key_stay_while_title_and_contributors_may_change():
    # backend tests/test_editorial_submit.py, the same name (RG-M2a 2).
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    for change in ({"kind": "audio", "signer": "publication"},
                   {"p_sub": _next(first, s_sub=b"\x07" * 32).body["p_sub"]}):
        status, data, _ = _submit(nam, dict(_next(first).body, **change))
        assert (status, data["error"]["code"]) == (409, "conflict"), change
    revised = _next(first, title="Neuer Titel", participants=(pk("contributor"),))
    assert _submit(nam, revised.body)[0] == 201
    record = server.submissions[first.body["submission_id"]]
    assert (record.title, record.participants) == ("Neuer Titel", [pk("contributor")])


def test_revisions_follow_one_another():
    # backend tests/test_editorial_submit.py, the same name.
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    second = _next(first)
    assert _submit(nam, second.body)[0] == 201
    for stale in (_next(first, content="# Drei").body,                # revision 2, other hash
                  _next(first, content="# Vier", revision=4).body,    # skips 3
                  dict(_next(first, content="# X", revision=3).body,
                       revision_hash=second.body["revision_hash"])):   # the same hash
        status, data, _ = _submit(nam, stale)
        assert (status, data["error"]["code"], data["error"]["detail"]) == (
            409, "stale_revision", {"revision": 2})


def test_a_repeat_gets_its_stored_receipt_before_any_other_check():
    # backend api/submissions.py: stored_repeat before refuse_early, so a
    # paused or blocking publication, a new epoch or a lost decider
    # still answer the body the server already has.
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    status, stored, _ = _submit(nam, first.body)
    assert status == 201
    pub = server.publication()
    pub.paused, pub.open = True, False
    pub.blocked.add(pk("writer"))
    pub.members = {}
    status, again, _ = _submit(nam, first.body)
    assert (status, again["receipt"]) == (200, stored["receipt"])


def test_the_revision_cap_is_forbidden_before_a_stale_revision():
    # backend core/editorial_quota.py refuse_revision_over_cap, in
    # refuse_early before _revise (RG-M2a 2).
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    record = server.submissions[first.body["submission_id"]]
    record.revisions[30] = dict(record.current, revision=30)
    status, data, _ = _submit(nam, _next(first, revision=5).body)      # stale too
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        403, "forbidden", {"limit": 30})


def test_what_the_publication_takes():
    # backend tests/test_editorial_submit.py, the same name.
    _clock, server, nam = _setup()
    pub = server.publication()
    content = pub.content

    def rules(**changes):
        event = dict(pub.key_event, content=json.dumps(dict(
            content, rules=dict(content["rules"], **changes))))
        pub.key_event = dict(event, id=pub.key_event["id"])

    body = submission_for(sk("writer")).body
    rules(kinds=["audio"])
    assert _submit(nam, body)[1]["error"]["detail"] == {"field": "kind"}
    rules(kinds=["article"], publish_by={"article": "publication", "audio": "publication"})
    status, data, _ = _submit(nam, body)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        403, "forbidden", {"field": "signer"})
    rules(max_envelope_bytes=100)
    status, data, _ = _submit(nam, body)
    assert (status, data["error"]["detail"]) == (413, {"max_bytes": 100})
    rules()
    pub.open = False
    assert _submit(nam, body)[1]["error"]["code"] == "paused"
    assert server.submissions == {}


def test_a_blocked_writer_is_refused_even_for_a_revision_and_a_paused_one_may_revise():
    # backend check_destination for every body; paused only in _check_new.
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    server.publication().open = False
    assert _submit(nam, _next(first).body)[0] == 201
    server.publication().blocked.add(pk("writer"))
    assert _submit(nam, _next(first, content="# Drei", revision=3).body)[1]["error"]["code"] \
        == "blocked"


def test_a_status_key_belongs_to_one_submission():
    # backend tests/test_editorial_submit.py, the same name.
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    other = submission_for(sk("writer"), d="anderes", status_key=first.status_key)
    status, data, _ = _submit(nam, other.body)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        409, "conflict", {"field": "status_key_hash"})


def test_revisions_an_hour_are_counted_and_refusals_and_repeats_do_not_count(monkeypatch):
    # backend tests/test_editorial_submit.py, the same rule.
    import tests.standup_fakes as fakes
    monkeypatch.setattr(fakes, "REVISIONS_PER_HOUR", 2)
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert _submit(nam, first.body)[0] == 201
    status, data, _ = _submit(nam, dict(_next(first).body, kind="audio", signer="publication"))
    assert data["error"]["code"] == "conflict"
    assert _submit(nam, first.body)[0] == 200                       # a repeat
    assert _submit(nam, _next(first).body)[0] == 201
    status, data, reply = _submit(nam, _next(first, content="# Drei", revision=3).body)
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert int(bytes(reply.rawHeader("Retry-After"))) >= 1


def test_every_submit_request_counts_against_the_signer_and_the_address(monkeypatch):
    # backend api/submissions.py: repeats included, refused ones too.
    import tests.standup_fakes as fakes
    monkeypatch.setattr(fakes, "SUBMIT_REQUESTS_PER_HOUR", 2)
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    assert [_submit(nam, first.body)[0] for _ in range(2)] == [201, 200]
    assert _submit(nam, first.body)[1]["error"]["code"] == "rate_limited"
    monkeypatch.setattr(fakes, "SUBMIT_REQUESTS_PER_ADDRESS_PER_HOUR", 3)
    other = submission_for(sk("contributor"))
    status, data, _ = _submit(nam, other.body, secret=sk("contributor"))
    assert data["error"]["code"] == "rate_limited" and server.submissions.keys() == {
        first.body["submission_id"]}


def test_an_unknown_publication_is_not_found():
    _clock, server, nam = _setup()
    server.publications.clear()
    status, data, _ = _submit(nam, submission_for(sk("writer")).body)
    assert (status, data["error"]["code"]) == (404, "not_found")


@pytest.mark.parametrize("change, code, detail", [
    (lambda b: dict(b, submission_id="a" * 64), "invalid", {"field": "submission_id"}),
    (lambda b: dict(b, envelope_sha256="b" * 64), "invalid", {"field": "envelope_sha256"}),
    (lambda b: dict(b, envelope="***"), "invalid", {"field": "envelope"}),
    (lambda b: dict(b, envelope=base64.b64encode(b"x" * 28).decode(),
                    envelope_sha256=hashlib.sha256(b"x" * 28).hexdigest()),
     "invalid", {"field": "envelope"}),
    (lambda b: dict(b, title="T" * 301), "invalid", {"field": "title"}),
    (lambda b: dict(b, title="a\nb"), "invalid", {"field": "title"}),
    (lambda b: dict(b, blobs=[{"sha256": "c" * 64, "size": 1,
                               "servers": ["http://media.example"]}]),
     "invalid", {"field": "blobs.servers"}),
    (lambda b: dict(b, note="x"), "invalid", {"fields": ["note"]}),
    (lambda b: {k: v for k, v in b.items() if k != "blobs"}, "invalid", {"fields": ["blobs"]}),
    (lambda b: dict(b, v=2), "invalid", {"field": "v"}),
    (lambda b: dict(b, kind="video"), "invalid", {"field": "kind"}),
    (lambda b: dict(b, client="c" * 65), "invalid", {"field": "client"}),
    (lambda b: dict(b, epoch=2), "epoch", None),
    (lambda b: dict(b, key_event_id="d" * 64), "epoch", None),
], ids=["wrong id", "envelope hash", "envelope not base64", "empty envelope", "long title",
        "title with a line break", "http blob server", "unknown field", "a field missing",
        "version 2", "kind", "long client", "old epoch", "other key event"])
def test_bodies_that_are_refused_store_nothing(change, code, detail):
    # backend tests/test_editorial_submit.py _body_problems, the same cases.
    _clock, server, nam = _setup()
    status, data, _ = _submit(nam, change(submission_for(sk("writer")).body))
    assert data["error"]["code"] == code, data
    if detail is not None:
        assert data["error"]["detail"] == detail
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
        assert record.timeline[1]["event"]["name"] == "named"        # a second item
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
    for seq in (3, 4):                                    # 2: the editor named
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
    assert record.timeline[-1]["seq"] == 2                    # the editor named on hand-in
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
    assert member["last_seq"] == before["last_seq"] == 2    # submitted, named


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


def test_a_body_refused_for_the_days_storage_counts_no_revision(monkeypatch):
    # backend core/editorial_intake.py hand_in: count_revision and
    # charge_storage share the transaction; a refusal keeps neither.
    import tests.standup_fakes as fakes
    monkeypatch.setattr(fakes, "REVISIONS_PER_HOUR", 1)
    monkeypatch.setattr(fakes, "STORED_BYTES_PER_DAY", 10)
    _clock, server, nam = _setup()
    first = submission_for(sk("writer"))
    status, data, _ = _submit(nam, first.body)
    assert (status, data["error"]["code"]) == (429, "rate_limited") and server.submissions == {}
    monkeypatch.setattr(fakes, "STORED_BYTES_PER_DAY", 64 * 1024 * 1024)
    assert _submit(nam, first.body)[0] == 201


# -- the conversation, as the backend takes it (api/submission_messages.py, 607c941) ---

_EPH = bytes(range(7, 39))


def _item(nam, secret, sid, body):
    token = _session(nam, secret)
    return _send(nam, "POST", f"/api/submissions/{sid}/messages", json.dumps(body).encode(),
                 authorization=f"Bearer {token}".encode())


def _gift(room, **changes):
    """A gift wrap to ``room`` as conversation.gift_wrap builds it, with
    ``changes`` signed in by the same ephemeral key."""
    from nostr.standup import conversation
    wrap = conversation.gift_wrap({"kind": 13, "content": "versiegelt", "tags": []}, room,
                                  now=NOW, eph_sk=_EPH)
    return events.sign_event(dict(wrap, **changes), _EPH) if changes else wrap


def _conversation(**changes):
    _clock, server, nam = _setup()
    prepared = submission_for(sk("writer"))
    assert _submit(nam, prepared.body)[0] == 201
    record = server.submissions[prepared.body["submission_id"]]
    body = {"channel": "conversation", "kind_hint": "message", "wrap": _gift(record.p_sub)}
    body.update(changes)
    return server, nam, record, body


def test_an_item_is_taken_once_and_a_repeat_gets_its_seq():
    server, nam, record, body = _conversation()
    status, data, _ = _item(nam, sk("writer"), record.sid, body)
    assert (status, data["seq"]) == (201, 3)                  # after submitted and named
    status, data, _ = _item(nam, sk("writer"), record.sid, body)
    assert (status, data["seq"], len(record.timeline)) == (200, 3, 3)


@pytest.mark.parametrize("change, status, code", [
    (lambda body, room: body.pop("kind_hint"), 422, "invalid"),
    (lambda body, room: body.update(client="x"), 422, "invalid"),
    (lambda body, room: body.update(channel="public"), 422, "invalid"),
    (lambda body, room: body.update(kind_hint="vote"), 422, "invalid"),
    (lambda body, room: body.update(wrap=_gift(room, kind=1)), 422, "invalid"),
    (lambda body, room: body.update(wrap=_gift(room, tags=[["p", room], ["p", room]])),
     422, "invalid"),
    (lambda body, room: body.update(wrap=_gift(room, created_at=NOW - 4 * 86400)),
     422, "invalid"),
    (lambda body, room: body.update(wrap=_gift(room, created_at=NOW + 3600)), 422, "invalid"),
    (lambda body, room: body.update(wrap=_gift(room, content="AgAA")), 422, "invalid"),
    (lambda body, room: body.update(wrap=_gift(room, content="A" * 100_000)),
     413, "too_large"),
], ids=["a key missing", "a key unknown", "channel", "kind_hint", "not a wrap", "two rooms",
        "too old", "ahead", "not nip-44", "too large"])
def test_a_body_is_checked_as_parse_message_checks_it(change, status, code):
    server, nam, record, body = _conversation()
    change(body, record.p_sub)
    before = len(record.timeline)
    got, data, _ = _item(nam, sk("writer"), record.sid, body)
    assert (got, data["error"]["code"]) == (status, code) and len(record.timeline) == before


def test_the_body_is_checked_before_who_may_see_it_and_a_stranger_learns_nothing():
    server, nam, record, body = _conversation()
    status, data, _ = _item(nam, sk("contributor"), record.sid, body)
    assert (status, data["error"]["code"]) == (404, "not_found")      # not 403
    status, data, _ = _item(nam, sk("contributor"), record.sid, dict(body, channel="x"))
    assert (status, data["error"]["code"]) == (422, "invalid")


def test_what_goes_into_the_timeline_follows_the_backends_rows():
    server, nam, record, body = _conversation()
    status, data, _ = _item(nam, sk("writer"), record.sid,
                            dict(body, channel="internal",
                                 wrap=_gift(server.publications[record.publication]
                                            .content["pubkey"])))
    assert (status, data["error"]["code"]) == (403, "role")           # no row for a writer
    status, _data, _ = _item(nam, sk("editor"), record.sid,
                             dict(body, kind_hint="edit", wrap=_gift(record.p_sub)))
    assert status == 201 and record.state == "submitted"              # an edit moves nothing
    assert _item(nam, sk("editor"), record.sid, dict(body, wrap=_gift(record.p_sub)))[0] == 201
    assert record.state == "in_review"                                # a message does
    record.state = "withdrawn"
    status, data, _ = _item(nam, sk("writer"), record.sid, dict(body, wrap=_gift(record.p_sub)))
    assert (status, data["error"]["code"]) == (409, "invalid_transition")


def test_an_item_goes_to_its_room_and_a_note_to_the_current_epoch():
    server, nam, record, body = _conversation()
    status, data, _ = _item(nam, sk("writer"), record.sid,
                            dict(body, wrap=_gift(pk("contributor"))))
    assert (status, data["error"]["code"]) == (422, "invalid")
    publication = server.publications[record.publication]
    publication.epoch_keys.append({"pubkey": pk("contributor")})      # an earlier epoch's
    note = dict(body, channel="internal")
    status, data, _ = _item(nam, sk("editor"), record.sid,
                            dict(note, wrap=_gift(pk("contributor"))))
    assert (status, data["error"]["code"]) == (409, "epoch")
    status, data, _ = _item(nam, sk("editor"), record.sid, dict(note, wrap=_gift(pk("editor"))))
    assert (status, data["error"]["code"]) == (422, "invalid")
    status, data, _ = _item(nam, sk("editor"), record.sid,
                            dict(note, wrap=_gift(publication.content["pubkey"])))
    assert (status, data["seq"], record.internal_seq) == (201, 1, 1)
