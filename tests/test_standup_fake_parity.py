# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fake STANDUP server against the backend's own rules, one case each
(RG-M2r 6, lesson 19): feat/redaktion-backend 3229d9d, contract 5ddb799;
the media servers' check, NIP-98, works by link, proposals and the gate
at 1183930.

Where the backend has a test for the rule, the case carries its name:
messages per person, per address and per submission (api/
submission_messages.py, core/editorial_quota.py), the stored bytes on the
day of whoever sent them (core/editorial_quota.py charge, api/
editorial_context.py store_action), sessions (api/editorial.py), the
editor named on hand-in (core/editorial_workflow.py name_editor), a
repeat (core/editorial_intake.py repeat), the lookup after a correction
(db/editorial_submissions.py receipts_of), ``no_decider``
(refuse_without_decider), the status key a submission keeps, and the
answers of contract 2's round step (a revision 1 at a stored sid is
``stale_revision``)."""

from __future__ import annotations

import base64 as _base64
import hashlib as _hashlib
import json

import pytest

from nostr import bech32
from nostr import events as _events
from nostr.standup import formats
from nostr.standup.watcher import AT_THE_END
from tests import standup_fakes as fakes
from tests.standup_fakes import (
    SERVER, _stored_json, link_submission_for, proposal_for, published_article, submission_for)
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
], ids=["a list", "a key", "a key twice", "too large"])
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

MEDIA_A, MEDIA_B = "https://media.example", "https://media-b.example"


def test_a_repeat_returns_the_stored_receipt_with_the_blobs_found_since():
    # backend tests/test_editorial_submit.py
    # test_a_repeat_returns_the_stored_receipt_with_the_blobs_found_since:
    # the media servers are asked again, for what was missing only.
    clock, server, nam = _setup()
    files = [{"sha256": c * 64, "size": 10, "servers": [MEDIA_A]} for c in "ab"]
    media = server.blob_servers
    media.put(MEDIA_A, "a" * 64, 10)
    prepared = submission_for(ME, blobs=tuple(files))
    first = _submit(nam, prepared.body)[1]["receipt"]
    assert (first["blobs_present"], first["blobs_missing"]) == (["a" * 64], ["b" * 64])
    assert media.asked == [(MEDIA_A, "a" * 64), (MEDIA_A, "b" * 64)]
    record = server.submissions[prepared.body["submission_id"]]
    record.state = "in_review"
    media.held[MEDIA_A] = {"b" * 64: 10}            # one uploaded since, one gone since
    media.asked.clear()
    status, data, _ = _submit(nam, prepared.body)
    assert status == 200
    assert media.asked == [(MEDIA_A, "b" * 64)]     # only what it reported missing
    assert data["receipt"] == dict(first, blobs_present=["a" * 64, "b" * 64], blobs_missing=[])
    media.asked.clear()
    assert _submit(nam, prepared.body)[1]["receipt"] == data["receipt"]
    assert media.asked == []                        # nothing missing: no server is asked


def test_a_repeat_finds_nothing_new_while_the_file_is_still_not_there():
    clock, server, nam = _setup()
    prepared = submission_for(ME, blobs=({"sha256": "c" * 64, "size": 10,
                                          "servers": [MEDIA_A, MEDIA_B]},))
    first = _submit(nam, prepared.body)[1]["receipt"]
    assert first["blobs_missing"] == ["c" * 64]
    server.blob_servers.asked.clear()
    status, data, _ = _submit(nam, prepared.body)
    assert status == 200 and data["receipt"] == first
    assert server.blob_servers.asked == [(MEDIA_A, "c" * 64), (MEDIA_B, "c" * 64)]
    server.blob_servers.put(MEDIA_B, "c" * 64, 10)  # the second server has it now
    assert _submit(nam, prepared.body)[1]["receipt"] == dict(
        first, blobs_present=["c" * 64], blobs_missing=[])


# backend tests/test_editorial_blobs.py, one case each, in the fake's
# media servers: (what the servers hold, the file's servers, found).
BLOB_CHECKS = [
    pytest.param(lambda m: m.put(MEDIA_A, "a" * 64, 5), [MEDIA_A], True,
                 [MEDIA_A], id="a server has it with its size"),
    pytest.param(lambda m: m.put(MEDIA_A, "a" * 64, 6), [MEDIA_A], False,
                 [MEDIA_A], id="another size is not the file"),
    pytest.param(lambda m: m.put(MEDIA_A, "a" * 64, None), [MEDIA_A], True,
                 [MEDIA_A], id="no Content-Length counts as present"),
    pytest.param(lambda m: (m.down.add(MEDIA_A), m.put(MEDIA_B, "a" * 64, 5),
                            m.put(MEDIA_B + "/again", "a" * 64, 5)),
                 [MEDIA_A, MEDIA_B, MEDIA_B + "/again"], True, [MEDIA_A, MEDIA_B],
                 id="the next server when one is down, stopping at the first hit"),
    pytest.param(lambda m: m.put(MEDIA_B, "a" * 64, 5), [MEDIA_A, MEDIA_B], True,
                 [MEDIA_A, MEDIA_B], id="the next server when one has not got it"),
    *[pytest.param(lambda m, at=at: m.put(at, "a" * 64, 5), [at], False, [],
                   id=f"never asked: {at}")
      for at in ("https://127.0.0.1", "https://10.0.0.1", "https://169.254.169.254",
                 "https://[::1]")],
    pytest.param(lambda m: (m.silent.add(MEDIA_A), m.put(MEDIA_B, "a" * 64, 5)),
                 [MEDIA_A, MEDIA_B], False, [MEDIA_A],
                 id="a server that does not answer spends the budget"),
]


@pytest.mark.parametrize("hold, servers, found, asked", BLOB_CHECKS)
def test_the_blob_check_asks_the_media_servers_as_the_backend_does(hold, servers, found, asked):
    clock, server, nam = _setup()
    hold(server.blob_servers)
    blob = {"sha256": "a" * 64, "size": 5, "servers": servers}
    receipt = _submit(nam, submission_for(ME, blobs=(blob,)).body)[1]["receipt"]
    assert receipt["blobs_present"] == (["a" * 64] if found else [])
    assert [at for at, _sha in server.blob_servers.asked] == asked


def test_a_silent_server_leaves_every_file_after_it_missing():
    # "whatever has not answered by then is reported missing"
    clock, server, nam = _setup()
    media = server.blob_servers
    media.silent.add(MEDIA_A)
    media.everywhere.update({"a" * 64, "b" * 64})
    files = ({"sha256": "a" * 64, "size": 5, "servers": [MEDIA_A]},
             {"sha256": "b" * 64, "size": 5, "servers": [MEDIA_B]})
    receipt = _submit(nam, submission_for(ME, blobs=files).body)[1]["receipt"]
    assert (receipt["blobs_present"], receipt["blobs_missing"]) == ([], ["a" * 64, "b" * 64])


@pytest.mark.parametrize("refuse", [
    lambda server: setattr(server.publication(), "paused", True),
    lambda server: server.publication().blocked.add(pk("writer")),
    lambda server: server.publications.clear(),
], ids=["paused", "blocked", "unknown publication"])
def test_no_media_server_is_asked_for_a_body_that_is_refused_first(refuse):
    # backend api/submissions.py: refuse_early before blobs.present, and
    # tests/test_editorial_submit.py: no blob server is asked for an
    # unknown publication.
    clock, server, nam = _setup()
    refuse(server)
    blob = {"sha256": "a" * 64, "size": 5, "servers": [MEDIA_A]}
    status = _submit(nam, submission_for(ME, blobs=(blob,)).body)[0]
    assert status in (403, 404, 409) and server.blob_servers.asked == []


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


# -- the round step (contract 2, bde1274; core/editorial_intake.py _revise) --------

@pytest.mark.parametrize("same_key", [True, False], ids=["its key", "another key"])
def test_a_revision_1_at_a_stored_sid_is_stale_and_stores_nothing(same_key):
    # backend _revise: an existing sid takes revision n + 1 only, checked
    # before its status key, so a second device's revision 1 under its own
    # secrets is stale_revision too, with the revision the server holds.
    _clock, server, nam, prepared, record = _case()
    again = submission_for(ME, content="# Anders", status_key=prepared.status_key
                           if same_key else None)
    status, data, _ = _submit(nam, again.body)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        409, "stale_revision", {"revision": 1})
    assert (record.revision, record.current["revision_hash"], len(record.timeline)) == (
        1, prepared.body["revision_hash"], 2)


def test_an_exact_repeat_of_a_stored_revision_1_is_its_receipt_never_stale():
    # Contract 5ddb799 (backend repeat before _revise): the same
    # revision_hash returns the stored receipt with 200 and stores nothing.
    _clock, server, nam, prepared, record = _case()
    first = record.receipts[1]
    status, data, _ = _submit(nam, prepared.body)
    assert (status, data["receipt"]["seq"], data["receipt"]["revision_hash"]) == (
        200, first["seq"], prepared.body["revision_hash"])
    assert (record.revision, len(record.timeline)) == (1, 2)


def test_the_round_steps_questions_leave_both_timelines_out():
    # Contract 2 step 2: a lookup or a GET asked from the end of the
    # timelines answers the View and nothing of the conversation.
    _clock, server, nam, prepared, record = _case()
    end = AT_THE_END
    body = json.dumps({"items": [{"status_key": prepared.status_key, "since": end}]}).encode()
    status, data, _ = _send(nam, "POST", "/api/submissions/lookup", body)
    (item,) = data["items"]
    assert (status, item["timeline"], item["more"], item["submission"]["later_round"]) == (
        200, [], False, False)
    status, data, _ = _send(nam, "GET", f"/api/submissions/{record.sid}?since={end}"
                            f"&since_internal={end}",
                            authorization=f"Bearer {_session(nam)}".encode())
    assert (status, data["timeline"], data["more"], data["submission"]["round"]) == (
        200, [], False, 1)
    status, data, _ = _send(nam, "GET", f"/api/submissions/{'ef' * 32}",
                            authorization=f"Bearer {_session(nam)}".encode())
    assert (status, data["ok"], data["error"]["code"]) == (404, False, "not_found")


# -- the fake's differences, RG-M2r2a (lesson 19) ------------------------------------

# backend tests/test_editorial_blobs.py test_which_blob_servers_a_submission_may_name,
# its development http setting off, and the control characters is_plain_text refuses.
BLOB_SERVERS = [
    ("https://media.example", True), ("https://media.example:443/files", True),
    ("https://media.example:8443", True), ("https://media.example:0", False),
    ("http://media.example", False), ("https://user@media.example", False),
    ("https://media.example/?q=1", False), ("https://", False), ("ftp://media.example", False),
    ("http://127.0.0.1:3000", False), ("https://media.example/#top", False),
    ("https://media.example:99999", False), ("https://media.example/\x07", False),
    ("https://" + "m" * 512, False),
]


@pytest.mark.parametrize("server_url, ok", BLOB_SERVERS)
def test_a_files_servers_are_checked_as_the_server_checks_them(server_url, ok):
    from nostr.standup.submission import blob_server_ok, submission_body_problem
    blob = {"sha256": "ab" * 32, "size": 10, "servers": [server_url]}
    assert blob_server_ok(server_url) is ok
    prepared = submission_for(ME)
    body = dict(prepared.body, blobs=[blob])
    assert (submission_body_problem(body) is None) is ok
    _clock, _server, nam = _setup()
    status, data, _ = _submit(nam, body)
    assert status == 201 if ok else (status, data["error"]["detail"]) == (
        422, {"field": "blobs.servers"})


ELEVEN = [f"https://m{n}.example" for n in range(11)]


@pytest.mark.parametrize("servers, size, ok", [
    ([], 10, False), (["https://a.example", "https://a.example"], 10, False),
    (ELEVEN, 10, False), (ELEVEN[:10], 10, True), (["https://a.example"], 0, False),
    (["https://a.example"], 1, True),
], ids=["none", "twice", "eleven", "ten", "empty file", "one byte"])
def test_a_file_names_one_to_ten_servers_each_once_and_has_a_byte(servers, size, ok):
    # backend core/editorial_submission.py: servers 1 to 10, unique; size >= 1.
    from nostr.standup.submission import blob_ok, submission_body_problem
    blob = {"sha256": "ab" * 32, "size": size, "servers": servers}
    body = dict(submission_for(ME).body, blobs=[blob])
    assert blob_ok(blob) is ok and (submission_body_problem(body) is None) is ok
    _clock, _server, nam = _setup()
    assert _submit(nam, body)[0] == (201 if ok else 422)


def test_a_submission_lists_only_the_members_who_read_its_epoch():
    # backend api/submission_reads.py: reads_epoch(member, row.epoch).
    _clock, server, nam, _prepared, record = _case()
    pub = server.publication()
    pub.members.update({OTHER: "redaktion", ALSO: "redaktion"})
    pub.since_join[ALSO] = record.epoch + 1             # joined after it, sees since then
    status, data, _ = _send(nam, "GET", f"/api/submissions/{record.sid}",
                            authorization=f"Bearer {_session(nam, ME)}".encode())
    listed = {m["pubkey"] for m in data["members"]}
    assert status == 200 and OTHER in listed and ALSO not in listed


def test_a_read_and_a_message_take_a_signature_as_well_as_a_session():
    # backend api/auth.py require_session_or_auth: a NIP-98 header, no payload
    # tag needed for a read.
    _clock, server, nam, _prepared, record = _case()
    path = f"/api/submissions/{record.sid}"
    signed = _auth(ME, fakes.SERVER + path, "GET", b"")
    assert _send(nam, "GET", path, authorization=signed)[0] == 200
    assert _send(nam, "GET", path, authorization=signed)[1]["error"]["detail"] == {
        "reason": "replay"}
    from nostr.standup import conversation
    wrap = conversation.gift_wrap({"kind": 13, "content": "x", "tags": []}, record.p_sub, now=NOW)
    body = json.dumps({"channel": "conversation", "kind_hint": "message",
                       "wrap": wrap}).encode()
    messages = path + "/messages"
    status, data, _ = _send(nam, "POST", messages, body,
                            authorization=_auth(ME, fakes.SERVER + messages, "POST", body))
    assert status in (200, 201), data


@pytest.mark.parametrize("able, hint", [(True, True), (False, False)],
                         ids=["someone could, named", "nobody could"])
def test_no_decider_says_not_to_name_the_whole_redaktion_only_when_someone_could(able, hint):
    # backend core/editorial_intake.py: the hint when a decider exists but
    # is named on the writer side.
    _clock, server, nam = _setup()
    if able:
        prepared = submission_for(ME, participants=(pk("editor"),))
    else:
        server.below_gate = {pk("editor")}
        prepared = submission_for(ME)
    status, data, _ = _submit(nam, prepared.body)
    assert (status, data["error"]["code"]) == (409, "no_decider")
    assert data["error"]["message"].endswith(
        " Nenne nicht die ganze Redaktion als Mitwirkende.") is hint


# -- NIP-98 as the backend checks it (fake f) ----------------------------------------
#
# backend tests/test_editorial_sessions.py and tests/test_nip98.py, one case
# each, against the fake's own port (tests/standup_backend_checks.py), never
# MyEditor's nostr/nip98.py.


def _signed_header(tags, *, created_at=NOW, kind=27235, secret=ME, edit=None) -> bytes:
    event = _events.sign_event({"kind": kind, "created_at": created_at, "tags": tags,
                                "content": ""}, secret)
    if edit is not None:
        edit(event)
    return b"Nostr " + _base64.b64encode(json.dumps(event).encode("utf-8"))


def _digest(body: bytes) -> str:
    return _hashlib.sha256(body).hexdigest()


SESSION_URL = SERVER + "/api/session"
GOOD = [["u", SESSION_URL], ["method", "POST"], ["payload", _digest(b"{}")]]


def _flip(event):
    event["sig"] = ("0" if event["sig"][0] != "0" else "1") + event["sig"][1:]


@pytest.mark.parametrize("header, reason", [
    (lambda: _signed_header(GOOD[:2]), "payload"),           # contract 10: even over {}
    (lambda: _signed_header(GOOD[:2] + [["payload", _digest(b"{}").upper()]]), "payload"),
    (lambda: _signed_header(GOOD + [GOOD[2]]), "payload"),
    (lambda: _signed_header(GOOD[:1] + GOOD), "malformed"),
    (lambda: _signed_header(GOOD[:2] + GOOD[1:]), "malformed"),
    (lambda: _signed_header(GOOD + [["u"]]), "malformed"),       # a bare u is a second u
    (lambda: _signed_header(GOOD[:2] + [["payload", _digest(b"{ }")]]), "payload"),
    (lambda: _signed_header([["u", SERVER + "/api/other"]] + GOOD[1:]), "url"),
    (lambda: _signed_header([["u", SESSION_URL + "?x=1"]] + GOOD[1:]), "url"),
    (lambda: _signed_header(GOOD[:1] + [["method", "PUT"]] + GOOD[2:]), "method"),
    (lambda: _signed_header(GOOD, created_at=NOW - 61), "expired"),
    (lambda: _signed_header(GOOD, created_at=NOW + 61), "expired"),
    (lambda: _signed_header(GOOD, kind=1), "kind"),
    (lambda: _signed_header(GOOD, edit=_flip), "signature"),
    (lambda: _signed_header(GOOD, edit=lambda e: e.update(content="changed")), "signature"),
    (lambda: _signed_header(GOOD, edit=lambda e: e.update(id=e["id"].upper())), "malformed"),
    (lambda: _signed_header(GOOD, edit=lambda e: e.update(created_at=True)), "malformed"),
    (lambda: b"Token " + _signed_header(GOOD)[6:], "scheme"),
    (lambda: _signed_header(GOOD + [["nonce", "x" * 8200]]), "malformed"),    # 8192 at most
    (lambda: b"Nostr not-base64!", "malformed"),
    (lambda: b"Nostr " + _base64.b64encode(b"[1, 2"), "malformed"),
    (lambda: b"Bearer " + b"a" * 64, "scheme"),
], ids=["no payload", "payload in capitals", "two payloads", "two u", "two methods",
        "a bare u", "payload of another body", "another path", "another query", "another method",
        "too old", "too new", "another kind", "a changed signature", "a changed content",
        "an id in capitals", "a time that is true", "another scheme", "too long",
        "not base64", "not JSON", "a session for a signed action"])
def test_a_signed_action_is_refused_with_the_backends_reason(header, reason):
    _clock, server, nam = _setup()
    status, data, _ = _send(nam, "POST", "/api/session", b"{}", header())
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        401, "auth_invalid", {"reason": reason})
    assert server.sessions == {}


@pytest.mark.parametrize("header", [
    lambda: _signed_header(GOOD),
    lambda: b"nostr " + _signed_header(GOOD)[6:],                     # the scheme in any case
    lambda: _signed_header([["u", "http://behind.proxy/api/session"]] + GOOD[1:]),
    lambda: _signed_header(GOOD[:1] + [["method", "post"]] + GOOD[2:]),
    lambda: _signed_header(GOOD, created_at=NOW - 60),
    lambda: _signed_header(GOOD + [["nonce", "a"], ["client", "x", "y"]]),
], ids=["as the contract spells it", "the scheme in lower case", "another host, same path",
        "the method in lower case", "at the edge of the window", "more tags"])
def test_what_the_backend_lets_through(header):
    # core/nip98.py binds the path and query (proxies change the host),
    # compares the method and scheme without case and reads the first tag
    # of each name that has a value.
    _clock, server, nam = _setup()
    status, data, _ = _send(nam, "POST", "/api/session", b"{}", header())
    assert status == 200, data
    assert len(server.sessions) == 1


def test_a_refused_request_does_not_spend_its_id_and_a_used_one_is_refused():
    # backend api/auth.py: the id is claimed once every check passed.
    _clock, server, nam = _setup()
    header = _signed_header(GOOD)
    refused = _send(nam, "POST", "/api/session", b'{"a":1}', header)[1]
    assert refused["error"]["detail"] == {"reason": "payload"}
    assert _send(nam, "POST", "/api/session", b"{}", header)[0] == 200
    again = _send(nam, "POST", "/api/session", b"{}", header)[1]
    assert again["error"]["detail"] == {"reason": "replay"}


def test_a_body_that_is_not_utf8_is_a_bad_request():
    _clock, _server, nam = _setup()
    body = b"\xff\xfe"
    header = _signed_header([["u", SESSION_URL], ["method", "POST"], ["payload", _digest(body)]])
    status, data, _ = _send(nam, "POST", "/api/session", body, header)
    assert (status, data["error"]["code"]) == (400, "bad_request")


def test_no_header_or_a_blank_one_asks_to_sign_in():
    _clock, _server, nam = _setup()
    for header in (None, b"", b"   "):
        status, data, _ = _send(nam, "POST", "/api/session", b"{}", header)
        assert (status, data["error"]["code"]) == (401, "auth_required"), header


def test_a_signed_action_with_no_body_needs_its_payload_tag_and_then_a_body():
    # require_signed_action: without a payload tag the signature is
    # refused; with one over the empty body the route reads its body, and
    # an empty one is no JSON object (core/editorial.py parse_body).
    _clock, server, nam = _setup()
    bare = _signed_header(GOOD[:2])
    assert _send(nam, "POST", "/api/session", None, bare)[1]["error"]["detail"] == {
        "reason": "payload"}
    over_nothing = _signed_header(GOOD[:2] + [["payload", _digest(b"")]])
    status, data, _ = _send(nam, "POST", "/api/session", None, over_nothing)
    assert (status, data["error"]["code"]) == (400, "bad_request")


def test_a_read_takes_nip98_with_no_payload_tag_and_a_body_still_needs_one():
    # require_session_or_auth signs with require_payload=False: a request
    # with no body needs no payload tag; one with a body needs it, as for
    # any NIP-98 request (core/nip98.py), and a session needs none.
    clock, server, nam = _setup()
    url = SERVER + "/api/me/submissions"
    read = _signed_header([["u", url], ["method", "GET"]])
    assert _send(nam, "GET", "/api/me/submissions", None, read)[0] == 200
    read_with = _signed_header([["u", url], ["method", "GET"], ["payload", _digest(b"")]])
    assert _send(nam, "GET", "/api/me/submissions", None, read_with)[0] == 200
    prepared = submission_for(ME)
    _submit(nam, prepared.body)
    sid = prepared.body["submission_id"]
    path = f"/api/submissions/{sid}/messages"
    body = json.dumps({"submission_id": sid, "wrap": {}}).encode("utf-8")
    without = _signed_header([["u", SERVER + path], ["method", "POST"]])
    status, data, _ = _send(nam, "POST", path, body, without)
    assert (status, data["error"]["detail"]) == (401, {"reason": "payload"})
    with_tag = _signed_header([["u", SERVER + path], ["method", "POST"],
                               ["payload", _digest(body)]])
    status, data, _ = _send(nam, "POST", path, body, with_tag)
    assert status != 401, data                          # past the signature, to the route


@pytest.mark.parametrize("value", [
    lambda t: b"Bearer " + b"b" * 64, lambda t: b"Bearer " + t.upper(),
    lambda t: b"Bearer " + t[:-1], lambda t: b"Bearer", lambda t: b"Bearer " + t + b" x",
], ids=["unknown", "in capitals", "too short", "no token", "more after it"])
def test_a_session_that_is_not_one_is_refused(value):
    # backend tests/test_editorial_sessions.py test_bearer_refusals_and_expiry.
    _clock, _server, nam = _setup()
    token = _session(nam).encode("ascii")
    status, data, _ = _send(nam, "GET", "/api/me/submissions", authorization=value(token))
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        401, "auth_invalid", {"reason": "session"})
    assert _send(nam, "GET", "/api/me/submissions",
                 authorization=b"bearer " + token)[0] == 200        # the scheme in any case


def test_signing_out_ends_an_expired_session_too():
    clock, server, nam = _setup()
    token = _session(nam).encode("ascii")
    clock.advance(13 * 3600)
    assert _send(nam, "DELETE", "/api/session", authorization=b"Bearer " + token)[0] == 200
    status, data, _ = _send(nam, "DELETE", "/api/session")
    assert (status, data["error"]["code"]) == (401, "auth_required")


# -- works by link and members' proposals (fake f) -----------------------------------
#
# backend core/editorial_submission.py parse_submission, link_source,
# proposal_source and parse_proposal, core/editorial_intake.py hand_in and
# api/submissions.py propose (1183930), one case each.

CURATOR = sk("contributor")


def _post(nam, path, payload, secret):
    body = json.dumps(payload).encode("utf-8")
    return _send(nam, "POST", path, body, _auth(secret, SERVER + path, "POST", body))


def _proposals_path(server):
    owner, site_d = formats.parse_address(server.publication().address)
    return f"/api/sites/{formats.route_key(owner, site_d)}/proposals"


def _decide(nam, record, outcome, secret=None):
    return _post(nam, f"/api/submissions/{record.sid}/decision",
                 {"submission_id": record.sid, "expected_revision": record.revision,
                  "outcome": outcome}, secret or sk("editor"))


def _rules(server, **rules):
    pub = server.publication()
    content = dict(pub.content, rules=dict(pub.content["rules"], **rules))
    pub.key_event = dict(pub.key_event, content=json.dumps(content))


def test_a_work_by_link_by_its_author_is_stored_as_already_public():
    _clock, server, nam = _setup()
    event, naddr = published_article(ME)
    prepared = link_submission_for(ME, event, naddr)
    status, data, _ = _submit(nam, prepared.body)
    assert status == 201, data
    receipt = data["receipt"]
    assert (receipt["envelope_sha256"], receipt["blobs_present"], receipt["blobs_missing"]) == (
        None, [], [])
    record = server.submissions[prepared.body["submission_id"]]
    assert (record.origin, record.marks, record.proposal_by) == ("public", ["already_public"], None)
    assert record.current["envelope"] is None and server.blob_servers.asked == []
    # The same body again is an exact repeat.
    assert _submit(nam, prepared.body)[:2] == (200, {"ok": True, "receipt": receipt})


@pytest.mark.parametrize("change, field, status", [
    (lambda b: b.update(envelope="", envelope_sha256="0" * 64, blobs=[]), "fields", 422),
    (lambda b: b.update(source=[]), "source", 422),
    (lambda b: b["source"].update(extra=1), "fields", 422),
    (lambda b: b["source"]["event"].update(content="changed"), "source.event", 422),
    (lambda b: b["source"]["event"].update(kind=1), "source.event", 422),
    (lambda b: b["source"].update(naddr="naddr1nothing"), "source.naddr", 422),
    (lambda b: b.update(coordinate_hash="0" * 64), "coordinate_hash", 422),
    (lambda b: b.update(revision_hash="0" * 64), "revision_hash", 422),
], ids=["an envelope with it", "no source object", "more in the source", "a changed event",
        "another kind", "an address that is not the work's", "another coordinate",
        "another revision hash"])
def test_a_body_by_link_is_checked_as_the_backend_checks_it(change, field, status):
    _clock, server, nam = _setup()
    event, naddr = published_article(ME)
    body = json.loads(json.dumps(link_submission_for(ME, event, naddr).body))
    change(body)
    status_got, data, _ = _submit(nam, body)
    assert status_got == status, data
    detail = data["error"]["detail"]
    assert (field in detail) if field == "fields" else detail.get("field") == field
    assert server.submissions == {}


def test_an_address_of_another_work_or_author_is_not_the_link():
    _clock, server, nam = _setup()
    event, _naddr = published_article(ME)
    for naddr in (bech32.encode_naddr("another-d", event["pubkey"], 30023, []),
                  bech32.encode_naddr("ein-jahr-mit-lightning", pk("editor"), 30023, []),
                  bech32.encode_naddr("ein-jahr-mit-lightning", event["pubkey"], 1, [])):
        data = _submit(nam, link_submission_for(ME, event, naddr).body)[1]
        assert data["error"]["detail"] == {"field": "source.naddr"}, naddr


def test_a_publication_that_takes_no_links_refuses_one():
    _clock, server, nam = _setup()
    _rules(server, by_link=False)
    event, naddr = published_article(ME)
    status, data, _ = _submit(nam, link_submission_for(ME, event, naddr).body)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        403, "forbidden", {"field": "source"})


def test_a_link_to_someone_elses_work_is_their_work_brought_by_the_submitter():
    # core/editorial_intake.py _new_submission: who is not its author
    # proposes it; a co-author ["p", pk, relay, "author"] is an author.
    # (Not the owner's work: the owner is the only one who could decide.)
    _clock, server, nam = _setup()
    event, naddr = published_article(sk("submission"))
    prepared = link_submission_for(ME, event, naddr)
    assert _submit(nam, prepared.body)[0] == 201
    record = server.submissions[prepared.body["submission_id"]]
    assert (record.writer, record.proposal_by, record.marks) == (
        pk("submission"), pk("writer"), ["already_public", "proposal"])
    assert record.origin == "proposal" and record.submitter == pk("writer")
    _clock, server, nam = _setup()
    shared, naddr = published_article(sk("submission"), co_authors=(pk("writer"),))
    prepared = link_submission_for(ME, shared, naddr)
    assert _submit(nam, prepared.body)[0] == 201
    record = server.submissions[prepared.body["submission_id"]]
    assert (record.writer, record.proposal_by, record.origin) == (pk("writer"), None, "public")


def test_a_work_someone_else_handed_in_is_taken():
    _clock, server, nam = _setup()
    event, naddr = published_article(ME)
    server.publication().members[pk("contributor")] = "curator"
    assert _post(nam, _proposals_path(server), proposal_for(CURATOR, event, naddr)[0],
                 CURATOR)[0] == 201
    status, data, _ = _submit(nam, link_submission_for(ME, event, naddr).body)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        409, "conflict", {"taken": True})


def test_a_work_by_link_is_revised_by_its_newer_version_and_keeps_its_origin():
    _clock, server, nam = _setup()
    event, naddr = published_article(ME)
    first = link_submission_for(ME, event, naddr)
    assert _submit(nam, first.body)[0] == 201
    newer, _ = published_article(ME, title="Ein Jahr mit Lightning, neu", created_at=1_700_000_100)
    second = link_submission_for(ME, newer, naddr, revision=2, s_sub=first.s_sub,
                                 status_key=first.status_key)
    status, data, _ = _submit(nam, second.body)
    assert status == 201, data
    record = server.submissions[first.body["submission_id"]]
    assert (record.revision, record.source_event_id) == (2, newer["id"])
    # An encrypted revision of a work by link keeps nothing the same.
    private = submission_for(ME, revision=3, s_sub=first.s_sub, status_key=first.status_key)
    private.body.update(coordinate_hash=first.body["coordinate_hash"],
                        submission_id=first.body["submission_id"])
    status, data, _ = _submit(nam, private.body)
    assert (status, data["error"]["code"]) == (409, "conflict")


def test_accepting_a_public_work_takes_it_as_published_and_a_proposal_takes_no_changes():
    _clock, server, nam = _setup()
    event, naddr = published_article(ME)
    prepared = link_submission_for(ME, event, naddr)
    _submit(nam, prepared.body)
    record = server.submissions[prepared.body["submission_id"]]
    status, data, _ = _decide(nam, record, "accept")
    assert status == 200, data
    assert data["submission"]["state"] == "published"
    assert data["submission"]["published"] == {"naddr": naddr, "event_id": event["id"]}
    _clock, server, nam = _setup()
    server.publication().members[pk("contributor")] = "curator"
    other, naddr = published_article(sk("submission"), d="ein-anderes-werk")
    assert _post(nam, _proposals_path(server), proposal_for(CURATOR, other, naddr)[0],
                 CURATOR)[0] == 201
    (record,) = server.submissions.values()
    status, data, _ = _post(nam, f"/api/submissions/{record.sid}/decision",
                            {"submission_id": record.sid, "expected_revision": 1,
                             "outcome": "request_changes", "message_wrap": {"x": 1}},
                            sk("editor"))
    assert (status, data["error"]["code"]) == (409, "invalid_transition")


def test_a_member_proposes_a_published_work():
    _clock, server, nam = _setup()
    server.publication().members[pk("contributor")] = "curator"
    event, naddr = published_article(sk("submission"))
    payload, _s_sub = proposal_for(CURATOR, event, naddr)
    status, data, _ = _post(nam, _proposals_path(server), payload, CURATOR)
    assert status == 201, data
    view = data["submission"]
    assert (view["writer"], view["proposal_by"], view["marks"], view["signer"]) == (
        pk("submission"), pk("contributor"), ["already_public", "proposal"], "writer")
    assert "last_internal_seq" in view                          # a member's View
    record = server.submissions[payload["submission_id"]]
    assert record.status_key_hash is None and set(record.current["wraps"]) == {
        "redaktion", "proposer"}
    # The same proposal again: 200 and its View, nothing stored twice.
    status, data, _ = _post(nam, _proposals_path(server), payload, CURATOR)
    assert status == 200 and data["submission"]["submission_id"] == record.sid
    assert len(server.submissions) == 1 and len(record.timeline) >= 1
    # The proposer reads it as its submitter, with the proposer's wrap.
    token = _session(nam, CURATOR).encode("ascii")
    status, data, _ = _send(nam, "GET", f"/api/submissions/{record.sid}",
                            authorization=b"Bearer " + token)
    assert status == 200 and data["my_role"] == "writer"
    assert set(data["revisions"][0]["wraps"]) == {"proposer"}
    listed = _send(nam, "GET", "/api/me/submissions", authorization=b"Bearer " + token)[1]
    assert [v["submission_id"] for v in listed["items"]] == [record.sid]


def test_a_publication_that_publishes_articles_itself_signs_a_proposal_too():
    _clock, server, nam = _setup()
    _rules(server, publish_by={"article": "publication", "audio": "publication"})
    server.publication().members[pk("contributor")] = "curator"
    event, naddr = published_article(sk("submission"))
    data = _post(nam, _proposals_path(server), proposal_for(CURATOR, event, naddr)[0],
                 CURATOR)[1]
    assert data["submission"]["signer"] == "publication"


@pytest.mark.parametrize("change, status, code, detail", [
    (lambda p, s: p.update(extra=1), 422, "invalid", {"fields": ["extra"]}),
    (lambda p, s: p.pop("title"), 422, "invalid", {"fields": ["title"]}),
    (lambda p, s: p["event"].update(content="changed"), 422, "invalid", None),
    (lambda p, s: p.update(submission_id="0" * 64), 422, "invalid", {"field": "submission_id"}),
    (lambda p, s: p.update(wraps={"redaktion": p["wraps"]["redaktion"]}), 422, "invalid",
     {"field": "wraps"}),
    (lambda p, s: p.update(epoch=p["epoch"] + 1), 409, "epoch", None),
    (lambda p, s: s.publication().members.pop(pk("contributor")), 403, "not_member", None),
    (lambda p, s: s.publication().members.update({pk("contributor"): "invited:curator"}),
     403, "not_member", None),
    (lambda p, s: s.below_gate.add(pk("contributor")), 403, "forbidden",
     {"gate": "redaktion", "required_role": "member"}),
    (lambda p, s: _rules(s, by_link=False), 403, "forbidden", {"field": "source"}),
    (lambda p, s: s.publication().blocked.add(pk("submission")), 403, "blocked", None),
], ids=["an unknown field", "a missing field", "a changed event", "another id",
        "no proposer wrap", "an old key", "no member", "only invited", "below the gate",
        "no links taken", "the author is blocked"])
def test_a_proposal_is_refused_as_the_backend_refuses_it(change, status, code, detail):
    _clock, server, nam = _setup()
    server.publication().members[pk("contributor")] = "curator"
    event, naddr = published_article(sk("submission"))
    payload = json.loads(json.dumps(proposal_for(CURATOR, event, naddr)[0]))
    change(payload, server)
    status_got, data, _ = _post(nam, _proposals_path(server), payload, CURATOR)
    assert (status_got, data["error"]["code"]) == (status, code), data
    if detail is not None:
        assert data["error"]["detail"] == detail
    assert server.submissions == {}


def test_the_gate_holds_the_redaktions_routes_and_never_a_writers():
    # Contract 12.2 "Instance gate": 403 forbidden on a Redaktion route;
    # GET /api/me/editorial answers with the member parts empty.
    _clock, server, nam = _setup()
    prepared = submission_for(sk("editor"))                  # the owner writes too
    server.below_gate.add(pk("editor"))
    token = _session(nam, sk("editor")).encode("ascii")
    status, data, _ = _send(nam, "GET", "/api/me/keyring", authorization=b"Bearer " + token)
    assert (status, data["error"]["code"], data["error"]["detail"]) == (
        403, "forbidden", {"gate": "redaktion", "required_role": "member"})
    mine = _send(nam, "GET", "/api/me/editorial", authorization=b"Bearer " + token)[1]
    assert (mine["publications"], mine["invitations"]) == ([], [])
    assert _send(nam, "GET", "/api/me/submissions",
                 authorization=b"Bearer " + token)[0] == 200
    assert prepared.body["publication"] == server.publication().address


def test_a_proposer_is_the_submitter_for_rounds_limits_and_naming():
    # db/editorial_submissions.py handed_in_by: the proposer of a
    # proposal; core/editorial_workflow.py name_editor never names who
    # handed it in.
    _clock, server, nam = _setup()
    proposer = sk("epoch2")                       # sorts before the owner: named first if allowed
    server.publication().members[pk("epoch2")] = "redaktion"
    path = _proposals_path(server)
    event, naddr = published_article(sk("submission"))
    payload, _ = proposal_for(proposer, event, naddr)
    assert _post(nam, path, payload, proposer)[0] == 201
    (record,) = server.submissions.values()
    assert record.responsible == pk("editor")
    again, _ = proposal_for(proposer, event, naddr, round_=2)
    status, data, _ = _post(nam, path, again, proposer)
    assert (status, data["error"]["detail"]) == (409, {"round": 1, "open": True})
    for d in ("zwei", "drei"):
        work, link = published_article(sk("submission"), d=d)
        assert _post(nam, path, proposal_for(proposer, work, link)[0], proposer)[0] == 201
    work, link = published_article(sk("submission"), d="vier")
    status, data, _ = _post(nam, path, proposal_for(proposer, work, link)[0], proposer)
    assert (status, data["error"]["code"]) == (403, "limit_open")
