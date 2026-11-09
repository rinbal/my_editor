# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fake STANDUP server against the backend's own rules, one case each
(RG-M2r 6, lesson 19): feat/redaktion-backend 3229d9d, contract f171b52.

Where the backend has a test for the rule, the case carries its name:
messages per person, per address and per submission (api/
submission_messages.py, core/editorial_quota.py), the stored bytes on the
day of whoever sent them (core/editorial_quota.py charge, api/
editorial_context.py store_action), sessions (api/editorial.py), the
editor named on hand-in (core/editorial_workflow.py name_editor), a
repeat (core/editorial_intake.py repeat), the lookup after a correction
(db/editorial_submissions.py receipts_of), ``no_decider``
(refuse_without_decider) and the status key a submission keeps."""

from __future__ import annotations

import json

import pytest

from nostr.standup import formats
from tests import standup_fakes as fakes
from tests.standup_fakes import _stored_json, submission_for
from tests.standup_vectors import NOW, pk, sk
from tests.test_standup_fakes import _auth, _next, _send, _session, _setup, _submit

ME = sk("writer")
OTHER = "ab" * 32                       # a member's key, for the members' details
ALSO = "cd" * 32


def _counted(server, scope, subject):
    return sum(count for (s, who, _start), count in server._counters.items()
               if (s, who) == (scope, subject))


def _own(secret_or_key, server):
    key = secret_or_key if isinstance(secret_or_key, str) else pk_of(secret_or_key)
    return f"{key}:{server.publication().address}"


def pk_of(secret):
    from nostr import crypto
    return crypto.get_public_key(secret).hex()


def _case(*, participants=()):
    clock, server, nam = _setup()
    prepared = submission_for(ME, participants=participants)
    status, data, _ = _submit(nam, prepared.body)
    assert status == 201, data
    return clock, server, nam, prepared, server.submissions[prepared.body["submission_id"]]


def _item(nam, secret, sid, body):
    """POST a conversation item with one session per person, as an app
    keeps it (a session each would meet the login limit first)."""
    tokens = nam.__dict__.setdefault("parity_tokens", {})
    if secret not in tokens:
        tokens[secret] = _session(nam, secret)
    return _send(nam, "POST", f"/api/submissions/{sid}/messages", json.dumps(body).encode(),
                 authorization=f"Bearer {tokens[secret]}".encode())


def _message(nam, secret, record, *, channel="conversation", room=None, content="versiegelt"):
    from nostr.standup import conversation
    wrap = conversation.gift_wrap({"kind": 13, "content": content, "tags": []},
                                  room or record.p_sub, now=NOW)
    return _item(nam, secret, record.sid,
                 {"channel": channel, "kind_hint": "message", "wrap": wrap})


def _note_room(server):
    return server.publication().content["pubkey"]


# -- stored bytes (backend tests/test_editorial_storage.py) ---------------------

def test_a_submission_is_charged_exactly_what_it_stored():
    _clock, server, _nam, prepared, record = _case()
    signed = record.revisions[1]["signed"]
    stored = sum(len(text.encode()) for text in (
        _stored_json(signed["event"]), signed["body"], _stored_json(prepared.body["wraps"]),
        _stored_json(prepared.body["blobs"]), _stored_json(record.receipts[1])))
    assert _counted(server, "stored_bytes", _own(ME, server)) == stored


def test_a_writer_stores_at_most_the_daily_bytes_at_one_publication(monkeypatch):
    _clock, server, nam, _prepared, record = _case()
    monkeypatch.setattr(fakes, "STORED_BYTES_PER_DAY",
                        _counted(server, "stored_bytes", _own(ME, server)))
    before = len(record.timeline)
    status, data, reply = _message(nam, ME, record)
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert int(bytes(reply.rawHeader("Retry-After"))) >= 1 and len(record.timeline) == before
    # The transaction counted nothing: not the message, not its bytes.
    assert _counted(server, "submission_messages", record.sid) == 0


def test_a_contributors_messages_count_on_their_own_day_and_never_on_the_writers(monkeypatch):
    _clock, server, nam, _prepared, record = _case(participants=(pk("contributor"),))
    writers = _counted(server, "stored_bytes", _own(ME, server))
    monkeypatch.setattr(fakes, "STORED_BYTES_PER_DAY", writers)       # the writer's day is used up
    assert _message(nam, sk("contributor"), record)[0] == 201
    assert _counted(server, "stored_bytes", _own(ME, server)) == writers
    wrap = record.timeline[-1]["wrap"]
    assert _counted(server, "stored_bytes", _own(sk("contributor"), server)) == len(
        _stored_json(wrap).encode())


def test_internal_notes_never_count_toward_the_writers_daily_bytes():
    _clock, server, nam, _prepared, record = _case()
    before = _counted(server, "stored_bytes", _own(ME, server))
    assert _message(nam, sk("editor"), record, channel="internal",
                    room=_note_room(server))[0] == 201
    assert _counted(server, "stored_bytes", _own(ME, server)) == before
    assert _counted(server, "stored_bytes_redaktion", server.publication().address) == len(
        _stored_json(record.timeline[-1]["wrap"]).encode())


def test_a_publication_stores_at_most_the_daily_bytes_of_its_redaktion(monkeypatch):
    _clock, server, nam, _prepared, record = _case()
    assert _message(nam, sk("editor"), record, channel="internal",
                    room=_note_room(server))[0] == 201
    monkeypatch.setattr(fakes, "REDAKTION_STORED_BYTES_PER_DAY", _counted(
        server, "stored_bytes_redaktion", server.publication().address))
    status, data, _ = _message(nam, sk("editor"), record)               # a message too
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert _message(nam, ME, record)[0] == 201                          # the writer's own day


def test_the_signed_body_is_charged_before_hand_ins_refusals(monkeypatch):
    # api/submissions.py submit: store_action charges the signed event and
    # body inside the transaction, before hand_in's checks; refused, it
    # counts nothing (a revision two ahead is refused 429, not 409).
    _clock, server, nam, prepared, _record = _case()
    used = _counted(server, "stored_bytes", _own(ME, server))
    monkeypatch.setattr(fakes, "STORED_BYTES_PER_DAY", used)
    skipping = _next(prepared, content="# Drei", revision=3).body
    status, data, _ = _submit(nam, skipping)
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert _counted(server, "stored_bytes", _own(ME, server)) == used
    monkeypatch.setattr(fakes, "STORED_BYTES_PER_DAY", 64 * 1024 * 1024)
    status, data, _ = _submit(nam, skipping)
    assert (status, data["error"]["code"]) == (409, "stale_revision")


# -- messages (backend tests/test_editorial_limits.py, test_editorial_messages.py) ---

def test_messages_per_person_are_limited(monkeypatch):
    _clock, server, nam, _prepared, record = _case()
    monkeypatch.setattr(fakes, "MESSAGES_PER_MINUTE", 2)
    for n in range(2):
        assert _message(nam, ME, record, content=f"Nachricht {n}")[0] == 201
    status, data, reply = _message(nam, ME, record, content="noch eine")
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert int(bytes(reply.rawHeader("Retry-After"))) >= 1


def test_messages_are_counted_before_the_body_is_read():
    # post_message: the limits come first, a refused body counts too.
    _clock, server, nam, _prepared, record = _case()
    for _ in range(fakes.MESSAGES_PER_MINUTE):
        assert _item(nam, ME, record.sid, {"channel": "x"})[0] == 422
    status, data, _ = _message(nam, ME, record)
    assert (status, data["error"]["code"]) == (429, "rate_limited")


def test_a_message_body_above_its_size_is_refused_before_it_is_read():
    _clock, server, nam, _prepared, record = _case()
    padded = b"{" + b" " * fakes.MESSAGE_BODY_BYTES + b"}"
    tokens = nam.__dict__.setdefault("parity_tokens", {})
    tokens.setdefault(ME, _session(nam, ME))
    status, data, _ = _send(nam, "POST", f"/api/submissions/{record.sid}/messages", padded,
                            authorization=f"Bearer {tokens[ME]}".encode())
    assert (status, data["error"]["code"]) == (413, "too_large")


def test_an_address_posts_at_most_60_messages_a_minute_whatever_keys_it_uses(monkeypatch):
    monkeypatch.setattr(fakes, "MESSAGES_PER_MINUTE", 10 ** 6)
    _clock, server, nam, _prepared, record = _case(participants=(pk("contributor"),))
    senders = [ME, sk("contributor"), sk("editor")]
    for n in range(fakes.MESSAGES_PER_ADDRESS_PER_MINUTE):
        assert _message(nam, senders[n % 3], record, content=f"Nachricht {n}")[0] == 201
    status, data, _ = _message(nam, sk("recipient"), record)           # not even a role
    assert (status, data["error"]["code"]) == (429, "rate_limited")


def test_a_submission_takes_200_messages_a_day_and_internal_notes_count_apart(monkeypatch):
    monkeypatch.setattr(fakes, "MESSAGES_PER_SUBMISSION_PER_DAY", 3)
    _clock, server, nam, _prepared, record = _case(participants=(pk("contributor"),))
    for n, sender in enumerate((ME, sk("contributor"), sk("editor"))):
        assert _message(nam, sender, record, content=f"Nachricht {n}")[0] == 201
    status, data, _ = _message(nam, ME, record, content="eine vierte")
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert _message(nam, sk("editor"), record, channel="internal",
                    room=_note_room(server))[0] == 201


# -- sessions (backend tests/test_editorial_sessions.py) ------------------------

def _login(nam, secret, body=b"{}"):
    url = fakes.SERVER + "/api/session"
    return _send(nam, "POST", "/api/session", body, _auth(secret, url, "POST", body))


@pytest.mark.parametrize("body, status, code", [
    (b"[]", 400, "bad_request"),
    (b'{"a":1}', 422, "invalid"),
    (b'{"a":1,"a":1}', 400, "bad_request"),
    (b"{" + b" " * 2000 + b"}", 413, "too_large"),
])
def test_login_body_must_be_an_empty_object(body, status, code):
    _clock, server, nam = _setup()
    got, data, _ = _login(nam, ME, body)
    assert (got, data["error"]["code"]) == (status, code) and server.sessions == {}


def test_login_is_rate_limited_per_pubkey():
    _clock, server, nam = _setup()
    for _ in range(fakes.SESSIONS_PER_HOUR):
        assert _login(nam, ME)[0] == 200
    status, data, reply = _login(nam, ME)
    assert (status, data["error"]["code"]) == (429, "rate_limited")
    assert 1 <= int(bytes(reply.rawHeader("Retry-After"))) <= 3600
    assert _login(nam, sk("contributor"))[0] == 200


def test_live_sessions_per_pubkey_are_capped():
    clock, server, nam = _setup()
    tokens = []
    for _ in range(fakes.MAX_LIVE_SESSIONS + 1):
        tokens.append(_login(nam, ME)[1]["token"])
        clock.advance(1)
    assert len(server.sessions) == fakes.MAX_LIVE_SESSIONS
    live = [_send(nam, "GET", "/api/me/submissions",
                  authorization=f"Bearer {token}".encode())[0] for token in tokens]
    assert live == [401] + [200] * fakes.MAX_LIVE_SESSIONS             # the oldest went


# -- naming on hand-in (backend core/editorial_workflow.py name_editor) ---------

def _members(server, **extra):
    pub = server.publication()
    pub.members.update({OTHER: "redaktion", ALSO: "redaktion"})
    for name, value in extra.items():
        setattr(pub, name, value)
    return pub


def test_a_hand_in_names_an_editor_and_the_view_has_their_turn():
    _clock, server, nam, prepared, record = _case()
    assert [i["event"]["name"] for i in record.timeline] == ["submitted", "named"]
    named = record.timeline[1]
    assert (named["event"]["actor"], named["event"]["to"]) == (None, pk("editor"))
    view = _send(nam, "GET", f"/api/submissions/{record.sid}",
                 authorization=f"Bearer {_session(nam)}".encode())[1]["submission"]
    assert view["turn"] == {"type": "editor", "pubkey": pk("editor"), "pending": []}
    assert (view["responsible"], view["last_message_seq"], view["last_seq"]) == (
        pk("editor"), 2, 2)


def test_the_editor_with_the_fewest_turns_is_named_then_the_least_recently_named():
    # backend tests/test_editorial_assignment.py: the fewest turns, then a
    # tie to whoever was named least recently, then the pubkey.
    _clock, server, nam = _setup()
    pub = _members(server)
    first = submission_for(ME)
    assert _submit(nam, first.body)[0] == 201
    server.submissions[first.body["submission_id"]].responsible = OTHER    # OTHER has a turn
    pub.last_named_at = {pk("editor"): 5, OTHER: 1, ALSO: 3}

    def named(d):
        prepared = submission_for(ME, d=d)
        assert _submit(nam, prepared.body)[0] == 201
        return server.submissions[prepared.body["submission_id"]].responsible

    assert named("ein-zweiter-text") == ALSO              # no turn, named before the owner
    assert pub.last_named_at[ALSO] == NOW
    assert named("ein-dritter-text") == pk("editor")      # the only one without a turn


@pytest.mark.parametrize("who", ["away", "excluded", "below the gate", "joined later",
                                 "invited", "the writer side"])
def test_who_may_be_named(who):
    _clock, server, nam = _setup()
    pub = server.publication()
    pub.members = {pk("editor"): "owner", OTHER: "redaktion"}
    pub.last_named_at = {pk("editor"): 9}                # OTHER would be first
    participants = ()
    if who == "away":
        pub.away_until[OTHER] = NOW + 3600
    elif who == "excluded":
        pub.assign_exclude = {OTHER}
    elif who == "below the gate":
        server.below_gate = {OTHER}
    elif who == "joined later":
        pub.since_join[OTHER] = pub.epoch + 1
    elif who == "invited":
        pub.members[OTHER] = "invited:redaktion"
    else:
        participants = (OTHER,)
    prepared = submission_for(ME, participants=participants)
    assert _submit(nam, prepared.body)[0] == 201
    assert server.submissions[prepared.body["submission_id"]].responsible == pk("editor")


def test_without_automatic_naming_the_submission_waits_for_the_redaktion():
    _clock, server, nam = _setup()
    server.publication().auto_assign = False
    prepared = submission_for(ME)
    assert _submit(nam, prepared.body)[0] == 201
    record = server.submissions[prepared.body["submission_id"]]
    assert [i["event"]["name"] for i in record.timeline] == ["submitted"]
    assert server.view(record)["turn"]["type"] == "redaktion"


# -- a repeat, and the lookup after a correction ----------------------------------

def test_a_repeat_returns_the_stored_receipt_with_the_blobs_found_since():
    clock, server, nam = _setup()
    files = [{"sha256": c * 64, "size": 10, "servers": ["https://media.example"]}
             for c in "ab"]
    server.blobs_present = {"a" * 64}
    prepared = submission_for(ME, blobs=tuple(files))
    first = _submit(nam, prepared.body)[1]["receipt"]
    assert (first["blobs_present"], first["blobs_missing"]) == (["a" * 64], ["b" * 64])
    record = server.submissions[prepared.body["submission_id"]]
    record.state = "in_review"
    server.blobs_present = {"b" * 64}               # one found since, one gone since
    status, data, _ = _submit(nam, prepared.body)
    assert status == 200
    # Only what it reported missing is looked for again, nothing else changes.
    assert data["receipt"] == dict(first, blobs_present=["a" * 64, "b" * 64], blobs_missing=[])
    assert _submit(nam, prepared.body)[1]["receipt"] == data["receipt"]


def test_after_a_correction_the_lookup_keeps_the_writers_receipt():
    _clock, server, nam, prepared, record = _case()
    sid = record.sid
    before = _lookup(nam, prepared.status_key)["receipt"]
    corrected = _next(prepared, content="# Korrigiert")
    fix = {key: corrected.body[key] for key in ("submission_id", "revision", "revision_hash",
                                                 "envelope", "envelope_sha256", "wraps",
                                                 "blobs")}
    body = json.dumps(fix).encode()
    url = f"{fakes.SERVER}/api/submissions/{sid}/corrections"
    assert _send(nam, "POST", f"/api/submissions/{sid}/corrections", body,
                 _auth(sk("editor"), url, "POST", body))[0] == 200
    item = _lookup(nam, prepared.status_key)
    assert item["submission"]["revision"] == 2
    assert item["receipt"] == before and item["receipt"]["revision"] == 1


def _lookup(nam, status_key):
    body = json.dumps({"items": [{"status_key": status_key, "since": 0}]}).encode()
    return _send(nam, "POST", "/api/submissions/lookup", body)[1]["items"][0]


# -- no_decider and the status key (core/editorial_intake.py) ----------------------

def test_a_member_below_the_gate_does_not_count_as_someone_who_could_decide():
    _clock, server, nam = _setup()
    server.below_gate = {pk("editor")}
    status, data, _ = _submit(nam, submission_for(ME).body)
    assert (status, data["error"]["code"]) == (409, "no_decider")


def test_a_member_who_cannot_read_the_epoch_does_not_count_as_someone_who_could_decide():
    _clock, server, nam = _setup()
    server.publication().since_join[pk("editor")] = server.publication().epoch + 1
    status, data, _ = _submit(nam, submission_for(ME).body)
    assert (status, data["error"]["code"]) == (409, "no_decider")


def test_a_revision_that_keeps_its_participants_is_never_refused_for_no_decider():
    _clock, server, nam, prepared, _record = _case()
    server.below_gate = {pk("editor")}                    # nobody could decide now
    assert _submit(nam, _next(prepared).body)[0] == 201
    status, data, _ = _submit(nam, _next(prepared, content="# Drei", revision=3,
                                         participants=(pk("contributor"),)).body)
    assert (status, data["error"]["code"]) == (409, "no_decider")


def test_a_revision_keeps_the_status_key_and_one_with_another_stores_nothing():
    _clock, server, nam, prepared, record = _case()
    other = _next(prepared)
    body = dict(other.body, status_key_hash=formats.status_key_hash("ee" * 32))
    status, data, _ = _submit(nam, body)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        409, "conflict", {"field": "status_key_hash"})
    assert (record.revision, len(record.timeline), record.status_key_hash) == (
        1, 2, prepared.body["status_key_hash"])


def test_a_member_below_the_gate_reads_nothing_of_a_submission():
    _clock, server, nam, _prepared, record = _case()
    server.publication().members[OTHER] = "redaktion"
    server.below_gate = {pk("editor")}
    status, data, _ = _send(nam, "GET", f"/api/submissions/{record.sid}",
                            authorization=f"Bearer {_session(nam, sk('editor'))}".encode())
    assert (status, data["error"]["code"]) == (404, "not_found")
