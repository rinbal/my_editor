# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The STANDUP client (nostr/standup/client.py, views.py) against the
fake server that re-checks every signature (tests/standup_fakes.py).

Pinned here: where requests may go; how each call is authorized (a
signed action always with a payload tag, a session for reading, nothing
for the lookup); that a receipt counts only when every field matches
(criterion 2 of the plan); that the same body twice is one submission
with one receipt (criterion 5); what each failure says, and whether the
request may have been stored; and that a stored signed action is
verified before it is believed (contract 10)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nostr import crypto, events
from nostr.standup import answers, conversation, formats, views
from nostr.standup import crypto as scrypto
from nostr.standup.client import StandupApi
from nostr.standup.errors import Code, StandupError
from tests.membership_fakes import FakeClock
from tests.standup_fakes import (
    ADDRESS,
    DROP_AFTER,
    DROP_BEFORE,
    KEY_EVENT,
    SERVER,
    FakeStandupServer,
    Fault,
    SignAs,
    StandupNam,
    submission_for,
)
from tests.standup_vectors import NOW, SID, VECTORS, pk, sk


class World:
    """A fake server, its transport and one client per person."""

    def __init__(self, *, takes=0.0) -> None:
        self.clock = FakeClock(NOW)
        self.server = FakeStandupServer(self.clock)
        self.nam = StandupNam(responder=self.server)
        self.signers = {}
        self.apis = {}
        self.takes = takes

    def api(self, who: str = "writer", **kw) -> StandupApi:
        if who not in self.apis:
            self.signers[who] = SignAs(sk(who), clock=self.clock, takes=kw.pop("takes", self.takes))
            self.apis[who] = StandupApi(self.signers[who], server=SERVER, nam=self.nam,
                                        clock=self.clock, **kw)
        return self.apis[who]

    def run(self, start):
        """Start a call with ``start(on_success, on_failure)``; its outcome."""
        outcome = []
        start(lambda value: outcome.append(("ok", value)),
              lambda error: outcome.append(("err", error)))
        self.nam.settle()
        assert len(outcome) == 1, outcome
        return outcome[0]

    def ok(self, start):
        kind, value = self.run(start)
        assert kind == "ok", value
        return value

    def err(self, start) -> StandupError:
        kind, value = self.run(start)
        assert kind == "err", value
        return value


def _authorizations(world):
    return [headers["Authorization"] for _verb, _path, _body, headers in world.server.requests]


# -- where requests go -------------------------------------------------------------

def test_without_a_usable_server_nothing_is_sent():
    world = World()
    api = StandupApi(SignAs(sk("writer")), server="http://standup.example", nam=world.nam,
                     clock=world.clock)
    error = world.err(api.instance)
    assert error.code == Code.UNAVAILABLE and not error.maybe_stored
    assert world.server.requests == []


# -- the public endpoints carry nothing ----------------------------------------------

def test_instance_destinations_and_the_notice_account_need_no_signature():
    world = World()
    api = world.api()
    instance = world.ok(api.instance)
    assert instance.operator == "Einundzwanzig e.V." and instance.server == SERVER
    assert instance.bote.pubkey == pk("bote")
    listed = world.ok(api.destinations)
    assert [d.address for d in listed] == [ADDRESS]
    assert listed[0].key_event == KEY_EVENT
    assert world.ok(api.bote_pubkey) == pk("bote")
    settings = world.ok(lambda ok, fail: api.site_settings(ADDRESS, ok, fail))
    assert settings.key_event == KEY_EVENT and settings.members_count >= 1
    assert _authorizations(world) == [None, None, None, None]
    assert world.signers["writer"].requests == []


# -- sessions ---------------------------------------------------------------------

def test_reading_opens_one_session_and_then_needs_no_signature():
    world = World()
    api = world.api()
    assert world.ok(api.my_submissions) == ()
    assert world.ok(api.my_submissions) == ()
    assert len(world.signers["writer"].requests) == 1      # the session, once
    auths = _authorizations(world)
    assert auths[0].startswith(b"Nostr ") and all(a.startswith(b"Bearer ") for a in auths[1:])
    assert api.has_session


def test_calls_waiting_for_a_session_share_one():
    world = World()
    api = world.api()
    outcomes = []
    for _ in range(3):
        api.my_submissions(outcomes.append, outcomes.append)
    world.nam.settle()
    assert outcomes == [(), (), ()]
    assert len(world.signers["writer"].requests) == 1


def test_an_expired_session_is_replaced_once_by_itself():
    world = World()
    api = world.api()
    world.ok(api.my_submissions)
    world.server.expire_sessions()
    assert world.ok(api.my_submissions) == ()
    assert len(world.signers["writer"].requests) == 2
    codes = [r for r in world.server.requests if r[1] == "/api/session"]
    assert len(codes) == 2


def test_a_session_about_to_run_out_is_replaced_before_it_is_used():
    world = World()
    api = world.api()
    world.ok(api.my_submissions)
    world.clock.advance(12 * 3600 - 30)
    world.ok(api.my_submissions)
    assert len(world.signers["writer"].requests) == 2
    # Replaced before it was used: the server never had to refuse it.
    assert [r[1] for r in world.server.requests].count("/api/me/submissions") == 2


def test_a_session_for_someone_else_is_refused(monkeypatch):
    world = World()
    api = world.api()
    original = world.server._session

    def other(who, payload, params, query):
        return original(replace(who, pubkey=pk("editor")), payload, params, query)

    monkeypatch.setattr(world.server, "_session", other)
    error = world.err(api.my_submissions)
    assert error.code == Code.BAD_RESPONSE and "another person" in error.message
    assert not api.has_session


def test_stop_forgets_the_session_and_drops_what_is_in_flight():
    world = World()
    api = world.api()
    world.ok(api.my_submissions)
    outcome = []
    api.my_submissions(outcome.append, outcome.append)
    api.stop()
    world.nam.settle()
    assert outcome == [] and not api.has_session


def test_closing_a_session_revokes_it_on_the_server():
    world = World()
    api = world.api()
    world.ok(api.my_submissions)
    assert len(world.server.sessions) == 1
    done = []
    api.close_session(lambda: done.append(True))
    world.nam.settle()
    assert done == [True] and world.server.sessions == {} and not api.has_session


# -- submitting -------------------------------------------------------------------

def test_a_matching_receipt_is_confirmed():
    world = World()
    prepared = submission_for(sk("writer"))
    result = world.ok(lambda ok, fail: world.api().submit(prepared.body, ok, fail))
    assert isinstance(result, views.Confirmed) and not result.repeat
    assert result.receipt.submission_id == prepared.body["submission_id"]
    signed = world.server.accepted[-1]
    assert ["payload", formats.sha256_hex(world.server.requests[-1][2])] in signed["tags"]


def test_the_same_body_twice_is_one_submission_with_the_same_receipt():
    # Criterion 5.
    world = World()
    prepared = submission_for(sk("writer"))
    first = world.ok(lambda ok, fail: world.api().submit(prepared.body, ok, fail))
    second = world.ok(lambda ok, fail: world.api().submit(prepared.body, ok, fail))
    assert isinstance(second, views.Confirmed) and second.repeat
    assert second.receipt.raw == first.receipt.raw
    assert len(world.server.submissions) == 1
    # Two signatures, never the same event twice.
    ids = [e["id"] for e in world.server.accepted]
    assert len(ids) == len(set(ids)) == 2


def _tamper(name, value):
    def edit(receipt):
        receipt[name] = value(receipt) if callable(value) else value
        return receipt
    return edit


@pytest.mark.parametrize("name, value", [
    ("v", 2),
    ("submission_id", "00" * 32),
    ("revision", 2),
    ("revision", True),
    ("revision_hash", "11" * 32),
    ("envelope_sha256", "22" * 32),
    ("wraps_sha256", lambda r: dict(r["wraps_sha256"], redaktion="33" * 32)),
    ("wraps_sha256", lambda r: {"writer": r["wraps_sha256"]["writer"]}),
    ("wraps_sha256", lambda r: dict(r["wraps_sha256"], extra="44" * 32)),
    ("blobs_present", ["55" * 32]),
    ("received_at", NOW + 3600),
    ("received_at", "1762675200"),
    ("state", "accepted_maybe"),
    ("seq", 0),
])
def test_a_receipt_that_differs_in_any_field_is_a_mismatch(name, value):
    # Criterion 2: never "Eingereicht" for something else.
    world = World()
    world.server.tamper_receipt = _tamper(name, value)
    prepared = submission_for(sk("writer"))
    result = world.ok(lambda ok, fail: world.api().submit(prepared.body, ok, fail))
    assert isinstance(result, views.Mismatch), result
    expected = "blobs" if name.startswith("blobs") else name
    assert expected in result.fields


def test_a_receipt_that_is_not_an_object_is_a_mismatch():
    world = World()
    world.server.tamper_receipt = lambda receipt: None
    result = world.ok(lambda ok, fail: world.api().submit(
        submission_for(sk("writer")).body, ok, fail))
    assert isinstance(result, views.Mismatch) and result.fields == ("receipt",)


def test_missing_files_are_named_and_the_same_body_again_confirms():
    world = World()
    blob = {"sha256": "ab" * 32, "size": 10, "servers": ["https://blossom.example"]}
    prepared = submission_for(sk("writer"), blobs=(blob,))
    result = world.ok(lambda ok, fail: world.api().submit(prepared.body, ok, fail))
    assert isinstance(result, views.BlobsMissing) and result.missing == ("ab" * 32,)
    world.server.blobs_present.add("ab" * 32)       # uploaded again
    again = world.ok(lambda ok, fail: world.api().submit(prepared.body, ok, fail))
    assert isinstance(again, views.Confirmed) and again.repeat
    assert len(world.server.submissions) == 1


def test_a_body_with_a_mistake_is_refused_before_any_signature():
    world = World()
    body = dict(submission_for(sk("writer")).body, title="x" * 301)
    error = world.err(lambda ok, fail: world.api().submit(body, ok, fail))
    assert error.code == Code.REFUSED and not error.maybe_stored
    assert world.signers["writer"].requests == [] and world.server.requests == []


def test_a_title_with_a_secret_key_is_refused_before_anything_is_sent():
    # The title travels in clear.
    from nostr import bech32
    world = World()
    nsec = bech32.encode_nsec(VECTORS["keys"]["submission"]["sk"])
    body = dict(submission_for(sk("writer")).body, title=f"Notiz {nsec}")
    error = world.err(lambda ok, fail: world.api().submit(body, ok, fail))
    assert error.code == Code.REFUSED and nsec not in error.message
    assert world.server.requests == []


# -- failures ---------------------------------------------------------------------

def test_a_connection_that_failed_before_sending_stored_nothing():
    world = World()
    world.server.inject("submit", DROP_BEFORE)
    error = world.err(lambda ok, fail: world.api().submit(
        submission_for(sk("writer")).body, ok, fail))
    assert error.code == Code.OFFLINE and not error.maybe_stored
    assert world.server.submissions == {}


def test_no_answer_after_sending_may_have_stored_it():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    error = world.err(lambda ok, fail: world.api().submit(
        submission_for(sk("writer")).body, ok, fail))
    assert error.code == Code.OFFLINE and error.maybe_stored
    assert len(world.server.submissions) == 1


@pytest.mark.parametrize("fault, code, stored", [
    (Fault(403, "paused"), Code.PAUSED, False),
    (Fault(403, "limit_open"), Code.LIMIT_OPEN, False),
    (Fault(403, "something_new"), Code.FORBIDDEN, False),
    (Fault(409, "stale_revision"), Code.STALE_REVISION, False),
    (Fault(422, "wrap_invalid"), Code.WRAP_INVALID, False),
    (Fault(413, "too_large"), Code.TOO_LARGE, False),
    (Fault(405, "method_not_allowed"), Code.METHOD_NOT_ALLOWED, False),
    (Fault(500, "internal"), Code.SERVER, True),
    (Fault(502, "bad_gateway"), Code.SERVER, True),
])
def test_each_refusal_reads_as_its_code(fault, code, stored):
    world = World()
    world.server.inject("submit", fault)
    error = world.err(lambda ok, fail: world.api().submit(
        submission_for(sk("writer")).body, ok, fail))
    assert (error.code, error.status, error.maybe_stored) == (code, fault.status, stored)
    assert error.message == "Nicht möglich."


def test_too_many_requests_say_how_long_to_wait():
    world = World()
    world.server.inject("submit", Fault(429, "rate_limited", retry_after=120))
    error = world.err(lambda ok, fail: world.api().submit(
        submission_for(sk("writer")).body, ok, fail))
    assert (error.code, error.retry_after) == (Code.RATE_LIMITED, 120)


def test_the_real_limits_of_the_fake_reach_the_client():
    world = World()
    api = world.api()
    for n in range(3):
        world.ok(lambda ok, fail, n=n: api.submit(
            submission_for(sk("writer"), d=f"text-{n}").body, ok, fail))
    error = world.err(lambda ok, fail: api.submit(
        submission_for(sk("writer"), d="text-3").body, ok, fail))
    assert error.code == Code.LIMIT_OPEN
    first = submission_for(sk("writer"), d="text-0", revision=3)
    error = world.err(lambda ok, fail: api.submit(first.body, ok, fail))
    assert error.code == Code.STALE_REVISION


def test_a_signature_that_expired_on_the_way_is_signed_once_more():
    # The person took 61 seconds to approve: the server's window is 60.
    world = World(takes=[61, 0])
    result = world.ok(lambda ok, fail: world.api().submit(
        submission_for(sk("writer")).body, ok, fail))
    assert isinstance(result, views.Confirmed)
    assert len(world.signers["writer"].requests) == 2
    assert world.server.refusals == ["time window"]


def test_a_signer_that_says_no_sends_nothing():
    world = World()
    api = StandupApi(SignAs(sk("writer"), failure="rejected by user"), server=SERVER,
                     nam=world.nam, clock=world.clock)
    error = world.err(lambda ok, fail: api.submit(submission_for(sk("writer")).body, ok, fail))
    assert error.code == Code.SIGNER_DECLINED and not error.maybe_stored
    assert world.server.requests == []


# -- the lookup --------------------------------------------------------------------

def test_the_lookup_carries_no_signature_and_answers_by_status_key():
    world = World()
    api = world.api()
    prepared = submission_for(sk("writer"))
    world.ok(lambda ok, fail: api.submit(prepared.body, ok, fail))
    signatures = len(world.signers["writer"].requests)
    found = world.ok(lambda ok, fail: api.lookup([(prepared.status_key, 0),
                                                  ("ee" * 32, 0)], ok, fail))
    assert list(found.found) == [prepared.status_key] and not found.unreadable
    item = found.found[prepared.status_key]
    assert views.lookup_confirms(item.receipt, submission_id=prepared.body["submission_id"],
                                 revision=1, revision_hash=prepared.body["revision_hash"])
    assert [i.event.name for i in item.timeline] == ["submitted"]
    assert len(world.signers["writer"].requests) == signatures
    assert world.server.requests[-1][3]["Authorization"] is None


def test_the_lookup_returns_the_latest_receipt():
    world = World()
    api = world.api()
    first = submission_for(sk("writer"))
    world.ok(lambda ok, fail: api.submit(first.body, ok, fail))
    second = submission_for(sk("writer"), revision=2, content="Neu.", s_sub=first.s_sub,
                            status_key=first.status_key)
    world.ok(lambda ok, fail: api.submit(second.body, ok, fail))
    found = world.ok(lambda ok, fail: api.lookup([(first.status_key, 0)], ok, fail))
    receipt = found.found[first.status_key].receipt
    assert (receipt.revision, receipt.revision_hash) == (2, second.body["revision_hash"])


def test_internal_notes_never_reach_the_writer_even_when_a_server_sends_them():
    # Criterion 8, from the writer's side.
    world = World()
    api = world.api()
    prepared = submission_for(sk("writer"))
    world.ok(lambda ok, fail: api.submit(prepared.body, ok, fail))
    record = world.server.submissions[prepared.body["submission_id"]]
    original = world.server._lookup

    def leaky(who, body, params, query):
        reply = original(who, body, params, query)
        data = json.loads(reply._body)
        data["items"][0]["timeline"].append({"seq": 99, "at": NOW, "type": "internal",
                                             "wrap": {"kind": 1059}, "event": None})
        reply._body = json.dumps(data).encode("utf-8")
        return reply

    world.server._lookup = leaky
    found = world.ok(lambda ok, fail: api.lookup([(prepared.status_key, 0)], ok, fail))
    assert all(i.type != "internal" for i in found.found[prepared.status_key].timeline)
    assert record.seq == 1


def test_an_answer_that_cannot_be_read_is_never_taken_for_not_stored():
    # 12.4: "nothing stored" may be concluded only from a key the server
    # does not know, never from an answer this app could not read.
    world = World()
    api = world.api()
    prepared = submission_for(sk("writer"))
    world.ok(lambda ok, fail: api.submit(prepared.body, ok, fail))
    original = world.server._lookup

    def broken(who, body, params, query):
        reply = original(who, body, params, query)
        data = json.loads(reply._body)
        data["items"][0]["receipt"]["seq"] = "eins"
        reply._body = json.dumps(data).encode("utf-8")
        return reply

    world.server._lookup = broken
    found = world.ok(lambda ok, fail: api.lookup([(prepared.status_key, 0),
                                                  ("ee" * 32, 0)], ok, fail))
    assert found.found == {} and found.unreadable == {prepared.status_key}


@pytest.mark.parametrize("items", [[], [("ab" * 32, 0)] * 51, [("xyz", 0)], [("ab" * 32, -1)]])
def test_a_lookup_that_the_server_would_refuse_is_not_sent(items):
    world = World()
    error = world.err(lambda ok, fail: world.api().lookup(items, ok, fail))
    assert error.code == Code.REFUSED and world.server.requests == []


# -- the conversation ----------------------------------------------------------------

def _room_message(prepared, text, *, sender_sk=None, forge_author=None):
    room_sk = prepared.s_sub
    room = scrypto.pubkey_of(room_sk)
    sender_sk = sender_sk or sk("writer")
    sender = crypto.get_public_key(sender_sk).hex()
    rumor = conversation.message_rumor(room=room, submission_id=prepared.body["submission_id"],
                                       text=text, sender=forge_author or sender,
                                       created_at=NOW)
    seal = conversation.seal_unsigned(rumor, room_sk, now=NOW)
    if forge_author:
        # A seal signed by the sender around a rumor naming someone else.
        seal["content"] = crypto.encrypt(json.dumps(rumor), crypto.conversation_key(
            sender_sk, bytes.fromhex(room)))
    seal["pubkey"] = sender
    signed = events.sign_event(seal, sender_sk)
    return conversation.gift_wrap(signed, room, now=NOW), rumor


def test_a_message_is_sent_with_its_kind_and_only_a_seq_confirms_it():
    world = World()
    api = world.api()
    prepared = submission_for(sk("writer"))
    world.ok(lambda ok, fail: api.submit(prepared.body, ok, fail))
    wrap, _rumor = _room_message(prepared, "Hallo Redaktion")
    seq = world.ok(lambda ok, fail: api.send_message(prepared.body["submission_id"], wrap,
                                                     kind_hint="message", on_success=ok,
                                                     on_failure=fail))
    assert seq == 2
    sent = json.loads(world.server.requests[-1][2])
    assert sent == {"channel": "conversation", "wrap": wrap, "kind_hint": "message"}
    assert world.server.requests[-1][3]["Authorization"].startswith(b"Bearer ")


def test_a_message_whose_seal_and_rumor_name_different_people_is_not_shown():
    # Criterion 9, end to end: both go through the server, one is shown.
    world = World()
    api = world.api()
    prepared = submission_for(sk("writer"))
    world.ok(lambda ok, fail: api.submit(prepared.body, ok, fail))
    good, _ = _room_message(prepared, "Echt")
    forged, _ = _room_message(prepared, "Gefälscht", sender_sk=sk("contributor"),
                              forge_author=pk("editor"))
    sid = prepared.body["submission_id"]
    for wrap in (good, forged):
        world.ok(lambda ok, fail, wrap=wrap: api.send_message(
            sid, wrap, kind_hint="message", on_success=ok, on_failure=fail))
    found = world.ok(lambda ok, fail: api.lookup([(prepared.status_key, 0)], ok, fail))
    timeline = conversation.open_items(found.found[prepared.status_key].timeline,
                                       prepared.s_sub, sid)
    assert [o.rumor["content"] for o in timeline.opened] == ["Echt"]
    assert timeline.not_shown == 1


def test_a_message_the_server_would_refuse_is_not_sent():
    world = World()
    prepared = submission_for(sk("writer"))
    error = world.err(lambda ok, fail: world.api().send_message(
        prepared.body["submission_id"], {"kind": 1059}, kind_hint="message",
        on_success=ok, on_failure=fail))
    assert error.code == Code.REFUSED and world.server.requests == []


@pytest.mark.parametrize("hint, channel", [("vote", "conversation"), ("message", "public")])
def test_a_kind_or_channel_the_contract_does_not_name_is_a_bug_of_the_caller(hint, channel):
    world = World()
    prepared = submission_for(sk("writer"))
    wrap, _ = _room_message(prepared, "x")
    with pytest.raises(ValueError):
        world.api().send_message(prepared.body["submission_id"], wrap, kind_hint=hint,
                                 channel=channel, on_success=print, on_failure=print)
    assert world.server.requests == []


# -- every endpoint ----------------------------------------------------------------

def _after_submit(world):
    prepared = submission_for(sk("writer"))
    world.ok(lambda ok, fail: world.api("writer").submit(prepared.body, ok, fail))
    return prepared


def test_every_endpoint_of_the_contract_goes_through_the_checking_server():
    world = World()
    writer, editor = world.api("writer"), world.api("editor")
    prepared = _after_submit(world)
    sid = prepared.body["submission_id"]
    wrap, _ = _room_message(prepared, "Bitte kürzen", sender_sk=sk("editor"))
    article = events.sign_event({"kind": 30023, "created_at": NOW, "content": "Text",
                                 "tags": [["d", "ein-jahr-mit-lightning"]]}, sk("writer"))

    detail = world.ok(lambda ok, fail: writer.submission(sid, ok, fail))
    assert detail.my_role == "writer" and detail.submission.state == "submitted"
    assert list(detail.revisions[0].wraps) == ["writer"]
    recorded = world.ok(lambda ok, fail: editor.decision(
        sid, outcome="request_changes", expected_revision=1, message_wrap=wrap,
        on_success=ok, on_failure=fail))
    view = recorded.submission
    assert view.state == "changes_requested" and view.turn.type == "writer"
    assert recorded.seq >= 2
    second = submission_for(sk("writer"), revision=2, content="Kürzer.",
                            s_sub=prepared.s_sub, status_key=prepared.status_key)
    assert isinstance(world.ok(lambda ok, fail: writer.submit(second.body, ok, fail)),
                      views.Confirmed)
    view = world.ok(lambda ok, fail: editor.responsible(sid, action="take", on_success=ok,
                                                        on_failure=fail))
    assert view.responsible == pk("editor") and view.state == "in_review"
    view = world.ok(lambda ok, fail: editor.decision(
        sid, outcome="accept", expected_revision=2, on_success=ok, on_failure=fail)).submission
    assert (view.state, view.step) == ("accepted", "writer_publishes")
    view = world.ok(lambda ok, fail: writer.confirm(
        sid, revision_hash=second.body["revision_hash"], confirms=["split"],
        on_success=ok, on_failure=fail))
    assert view.confirmations[pk("writer")] == ("split",)
    view = world.ok(lambda ok, fail: writer.published(
        sid, naddr="naddr1test", event=article, on_success=ok, on_failure=fail))
    assert view.state == "published" and view.published.event_id == article["id"]
    site = events.sign_event({"kind": 30004, "created_at": NOW, "content": "",
                              "tags": [["d", "einundzwanzig-magazin"]]}, sk("publication"))
    view = world.ok(lambda ok, fail: editor.placed(sid, site_event=site, on_success=ok,
                                                   on_failure=fail))
    assert view.placed and view.turn.type == "none"
    correction = submission_for(sk("writer"), revision=3, content="Korrigiert.",
                                s_sub=prepared.s_sub, status_key=prepared.status_key).body
    view = world.ok(lambda ok, fail: editor.correction(
        sid, revision=3, revision_hash=correction["revision_hash"],
        envelope=correction["envelope"], envelope_sha256=correction["envelope_sha256"],
        wraps=correction["wraps"], blobs=correction["blobs"], on_success=ok,
        on_failure=fail)).submission
    assert "changed_since_acceptance" in view.marks
    assert world.ok(lambda ok, fail: editor.request_reupload(
        sid, sha256="ab" * 32, on_success=ok, on_failure=fail)) is None

    # The other writer-side calls.
    other = submission_for(sk("writer"), d="zweiter-text")
    world.ok(lambda ok, fail: writer.submit(other.body, ok, fail))
    view = world.ok(lambda ok, fail: writer.withdraw(other.body["submission_id"], ok, fail))
    assert view.state == "withdrawn"
    mine = world.ok(writer.my_submissions)
    assert {v.submission_id for v in mine} == {sid, other.body["submission_id"]}

    # The member side.
    assert [m.pubkey for m in world.ok(lambda ok, fail: editor.members(ADDRESS, ok, fail))] \
        == [pk("editor")]
    assert world.ok(lambda ok, fail: editor.keys(ADDRESS, ok, fail))[0].epoch == 3
    assert world.ok(lambda ok, fail: editor.invite_member(
        ADDRESS, pubkey=pk("contributor"), role="redaktion", sees="all", wraps=[],
        history=None, on_success=ok, on_failure=fail)).status == "invited"
    contributor = world.api("contributor")
    invited = world.ok(contributor.my_editorial)
    assert [i.address for i in invited.invitations] == [ADDRESS]
    assert invited.publications == ()
    assert world.ok(lambda ok, fail: contributor.accept_membership(
        ADDRESS, ok, fail)).status == "active"
    assert world.ok(lambda ok, fail: contributor.update_my_membership(
        ADDRESS, {"away_until": None}, ok, fail)).role == "redaktion"
    assert json.loads(world.server.requests[-1][2]) == {"away_until": None}
    with pytest.raises(ValueError):
        contributor.update_my_membership(ADDRESS, {"role": "owner"}, print, print)
    assert world.ok(lambda ok, fail: editor.new_epoch(ADDRESS, {"key_event": None}, ok,
                                                      fail)) == 4
    assert world.ok(lambda ok, fail: editor.remove_member(
        ADDRESS, pubkey=pk("contributor"), epoch={}, on_success=ok, on_failure=fail)) == 4
    assert world.ok(lambda ok, fail: editor.put_site_settings(
        ADDRESS, key_event=None, settings_event=None, on_success=ok,
        on_failure=fail)).key_event == KEY_EVENT
    assert json.loads(world.server.requests[-1][2])["owner_wrap"] is None
    assert world.ok(lambda ok, fail: editor.keyring(ok, fail)) == answers.Keyring(0, ())
    assert world.ok(lambda ok, fail: editor.put_keyring(
        expected_version=0, pages=["ct"], on_success=ok, on_failure=fail)) == 1
    assert world.ok(lambda ok, fail: editor.keyring(ok, fail)) == answers.Keyring(1, ("ct",))
    assert world.err(lambda ok, fail: editor.put_keyring(
        expected_version=0, pages=["ct"], on_success=ok, on_failure=fail)).code == Code.CONFLICT
    listed = world.ok(lambda ok, fail: editor.site_submissions(ADDRESS, on_success=ok,
                                                               on_failure=fail))
    assert len(listed.items) == 2 and listed.next is None
    assert world.ok(editor.my_editorial).publications[0].role == "owner"
    assert world.err(lambda ok, fail: editor.propose(
        ADDRESS, naddr="naddr1x", event=article, p_sub=prepared.body["p_sub"], wraps={},
        title="Vorschlag", on_success=ok, on_failure=fail)).code == Code.INVALID

    # Every signed action carried a payload tag and passed the server's
    # checks; nothing was refused along the way.
    assert world.server.refusals == []
    for event in world.server.accepted:
        assert len([t for t in event["tags"] if t[0] == "payload"]) == 1
    signed_routes = {r[1] for r in world.server.requests
                     if (r[3]["Authorization"] or b"").startswith(b"Nostr ")}
    assert "/api/session" in signed_routes and f"/api/submissions/{sid}/decision" in signed_routes


def test_the_writer_is_refused_what_only_the_editorial_team_may_do():
    world = World()
    prepared = _after_submit(world)
    sid = prepared.body["submission_id"]
    error = world.err(lambda ok, fail: world.api("writer").decision(
        sid, outcome="accept", expected_revision=1, on_success=ok, on_failure=fail))
    assert (error.code, error.status) == (Code.ROLE, 403)
    error = world.err(lambda ok, fail: world.api("editor").decision(
        sid, outcome="request_changes", expected_revision=1, on_success=ok, on_failure=fail))
    assert error.code == Code.INVALID
    error = world.err(lambda ok, fail: world.api("editor").decision(
        sid, outcome="reopen", expected_revision=1, on_success=ok, on_failure=fail))
    assert error.code == Code.INVALID_TRANSITION


# -- signed actions (10) -------------------------------------------------------------

def _fixture_action():
    action = VECTORS["signed_action"]
    return views.SignedRecord(event=action["event"], body=action["body"])


def test_the_fixtures_signed_action_verifies():
    path = f"/api/submissions/{SID}/decision"
    assert views.signed_action_problem(_fixture_action(), path=path, submission_id=SID,
                                       signers={pk("editor")}) is None
    assert views.action_path("accept", SID) == path


@pytest.mark.parametrize("change, words", [
    (lambda s: views.SignedRecord(event=s.event, body=s.body.replace("1", "2", 1)),
     "another body"),
    (lambda s: views.SignedRecord(event=dict(s.event, sig="00" * 64), body=s.body),
     "does not verify"),
])
def test_a_changed_signed_action_does_not_verify(change, words):
    problem = views.signed_action_problem(change(_fixture_action()),
                                          path=f"/api/submissions/{SID}/decision",
                                          submission_id=SID)
    assert problem and words in problem


def test_a_signed_action_for_another_path_submission_or_signer_does_not_verify():
    action = _fixture_action()
    assert "another action" in views.signed_action_problem(
        action, path=f"/api/submissions/{SID}/withdraw", submission_id=SID)
    assert "another submission" in views.signed_action_problem(
        action, path=f"/api/submissions/{SID}/decision", submission_id="ab" * 32)
    assert "without that role" in views.signed_action_problem(
        action, path=f"/api/submissions/{SID}/decision", submission_id=SID,
        signers={pk("writer")})


def test_the_timeline_carries_signed_actions_that_verify():
    world = World()
    prepared = _after_submit(world)
    sid = prepared.body["submission_id"]
    world.ok(lambda ok, fail: world.api("editor").decision(
        sid, outcome="decline", expected_revision=1, on_success=ok, on_failure=fail))
    found = world.ok(lambda ok, fail: world.api().lookup([(prepared.status_key, 0)], ok, fail))
    events_ = [i.event for i in found.found[prepared.status_key].timeline if i.type == "event"]
    assert [e.name for e in events_] == ["submitted", "decline"]
    for event in events_:
        assert views.signed_action_problem(event.signed, path=views.action_path(event.name, sid),
                                           submission_id=sid) is None


# -- answers this client cannot use -------------------------------------------------

def test_an_answer_with_a_key_twice_is_refused():
    world = World()
    original = world.server._instance

    def doubled(*args):
        reply = original(*args)
        reply._body = b'{"ok":true,"ok":true,"operator":"x"}'
        return reply

    world.server._instance = doubled
    error = world.err(world.api().instance)
    assert error.code == Code.BAD_RESPONSE
