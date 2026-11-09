# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writer's submissions list and read state (nostr/standup/lists.py,
editorial contract 13), on the shared encrypted list store.

Pinned here: an entry is on the relays before its submission may be
sent; entries merge by ``sid`` with the newest ``updated_at``; the read
state by the highest ``seq``; secrets never print; everything is per
account. And criterion 22 of the plan: a list of 500 submissions, a read
state of 500, and a message at the largest size the contract allows go
through a NIP-46 signer app whose answers must each fit one NIP-44
message."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nostr import crypto
from nostr.encrypted_lists import NO_ANSWER, PAGE_PLAINTEXT_MAX, SENT, UNREADABLE
from nostr.standup import conversation, formats
from nostr.standup import crypto as scrypto
from nostr.standup.lists import (
    READ_STATE_INTERVAL_SECONDS,
    ReadState,
    SubmissionEntry,
    SubmissionsList,
    entry_from_json,
    merge_entries,
    stored,
)
from tests.membership_fakes import FakeClock
from tests.outbox_fakes import PK, FakeRelayDirectory, settle
from tests.standup_fakes import paired_bunker
from tests.standup_vectors import ADDRESS, NOW, sk
from tests.test_encrypted_lists import Relay
from tests.test_imports_subscriptions import FakeSessionPool, FakeSigner

PROFILE = SimpleNamespace(user_pubkey=PK, bunker_relays=[])


def entry(n: int = 0, *, updated_at: int = NOW, revision: int = 1, **changes) -> SubmissionEntry:
    """A writer's entry at realistic sizes (64-hex ids and keys)."""
    value = SubmissionEntry(
        sid=formats.sha256_hex(f"sid-{n}"), publication=ADDRESS,
        server="https://standup.example", role="writer",
        s_sub=scrypto.new_secret_key().hex(), status_key=scrypto.new_status_key(),
        title=f"Ein Jahr mit Lightning im Café, Teil {n}", kind="article",
        d=f"ein-jahr-mit-lightning-{n}", round=1, revision=revision,
        revision_hash=formats.sha256_hex(f"rev-{n}-{revision}"), updated_at=updated_at)
    return replace(value, **changes)


def make(cls=SubmissionsList, *, relay=None, signer=None, profile=PROFILE, clock=None):
    relay = relay if relay is not None else Relay()
    clock = clock or FakeClock(NOW)
    return cls(profile, session_pool=FakeSessionPool(signer or FakeSigner()),
               relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
               clock=clock), relay, clock


def record(store, *entries):
    results = []
    store.record_many(list(entries), results.append)
    settle(60)
    assert results, "the write never finished"
    return results[-1]


def load(store):
    answers = []
    store.load(answers.append)
    settle(60)
    assert answers, "the read never finished"
    return answers[-1]


# -- entries -------------------------------------------------------------------

def test_an_entry_has_the_contracts_fields_and_round_trips():
    value = entry()
    data = value.to_json()
    assert set(data) == {"sid", "publication", "server", "role", "s_sub", "status_key",
                         "title", "kind", "d", "round", "revision", "revision_hash",
                         "updated_at"}
    assert entry_from_json(data) == value


def test_an_entry_never_prints_its_secrets():
    value = entry()
    assert value.s_sub not in repr(value) and value.status_key not in repr(value)


@pytest.mark.parametrize("change", [
    {"sid": "x"}, {"publication": "30023:ab:c"}, {"role": "editor"}, {"s_sub": "00" * 32},
    {"status_key": None}, {"s_sub": None}, {"kind": "video"}, {"round": 0}, {"revision": True},
    {"revision_hash": "AB" * 32}, {"title": "x" * 301}, {"updated_at": "1"},
])
def test_a_malformed_entry_is_left_out(change):
    assert entry_from_json(dict(entry().to_json(), **change)) is None


def test_a_contributor_may_have_no_status_key_and_no_key_yet():
    data = dict(entry().to_json(), role="contributor", status_key=None, s_sub=None)
    assert entry_from_json(data).role == "contributor"


def test_entries_merge_by_id_and_the_newest_wins_in_place():
    a, b, c = entry(1), entry(2), entry(3)
    newer_b = replace(b, revision=2, updated_at=NOW + 5)
    older_a = replace(a, title="älter", updated_at=NOW - 5)
    merged = merge_entries([a, b], [older_a, newer_b, c])
    assert merged == [a, newer_b, c]


# -- written before sending ------------------------------------------------------

def test_an_entry_is_on_the_relays_before_the_submission_may_be_sent():
    store, relay, _clock = make()
    value = entry()
    result = record(store, value)
    assert result.status == SENT and stored(result)
    page = relay.page(formats.SUBMISSIONS_LIST_D)
    assert page["v"] == 1 and page["page"] == 0 and page["pages"] == 1
    [item] = page["items"]
    assert (item["sid"], item["s_sub"], item["status_key"], item["revision"],
            item["revision_hash"]) == (value.sid, value.s_sub, value.status_key,
                                       value.revision, value.revision_hash)


def test_when_no_relay_answers_the_submission_is_not_sent():
    relay = Relay()
    relay.answers = False
    store, _relay, _clock = make(relay=relay)
    result = record(store, entry())
    assert result.status == NO_ANSWER and not stored(result)
    assert relay.published == []


def test_an_unreadable_list_is_never_written_over_and_blocks_sending():
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, None, content="not a ciphertext")
    store, _relay, _clock = make(relay=relay)
    result = record(store, entry())
    assert result.status == UNREADABLE and not stored(result)
    assert relay.published == []


def test_a_recorded_entry_wins_over_its_older_self_even_in_the_same_second():
    store, relay, _clock = make()
    first = entry(revision=2)
    record(store, first)
    rebuilt = replace(first, revision_hash="77" * 32)
    record(store, rebuilt)
    [item] = relay.page(formats.SUBMISSIONS_LIST_D)["items"]
    assert item["revision_hash"] == "77" * 32


def test_another_devices_entries_are_kept():
    relay = Relay()
    theirs = entry(9)
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [theirs.to_json()]})
    store, _relay, _clock = make(relay=relay)
    mine = entry(1)
    record(store, mine)
    items = relay.page(formats.SUBMISSIONS_LIST_D)["items"]
    assert [i["sid"] for i in items] == [theirs.sid, mine.sid]
    assert [e.sid for e in store.entries()] == [theirs.sid, mine.sid]


def test_loading_reads_the_entries_and_stop_forgets_them():
    relay = Relay()
    value = entry()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [value.to_json(), {"sid": "broken"}]})
    store, _relay, _clock = make(relay=relay)
    assert load(store) == [value]
    store.stop()
    assert store.entries() == [] and not store.loaded


def test_an_answer_for_an_account_that_was_left_changes_nothing():
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [entry().to_json()]})
    store, _relay, _clock = make(relay=relay)
    answers = []
    store.load(answers.append)
    store.stop()
    settle(60)
    assert answers == [] and store.entries() == []


# -- the read state ----------------------------------------------------------------

def test_the_read_state_has_an_opaque_d_and_keeps_the_highest_seq():
    relay = Relay()
    d = formats.read_state_d(PK)
    sid_a, sid_b = formats.sha256_hex("a"), formats.sha256_hex("b")
    relay.put(d, {"v": 1, "page": 0, "pages": 1, "seen": {sid_a: 9, sid_b: 2}})
    store, _relay, clock = make(ReadState, relay=relay)
    assert load(store) == {sid_a: 9, sid_b: 2}
    store.mark(sid_a, 4)          # older: ignored
    store.mark(sid_b, 5)
    assert store.seen(sid_a) == 9 and store.seen(sid_b) == 5
    assert store.flush(leaving=True)
    settle(60)
    assert relay.page(d)["seen"] == {sid_a: 9, sid_b: 5}
    assert "einundzwanzig" not in d and "read-state" not in d


def test_the_read_state_is_written_at_most_every_five_minutes_and_when_leaving():
    store, relay, clock = make(ReadState)
    load(store)
    sid = formats.sha256_hex("a")
    store.mark(sid, 1)
    assert store.flush()
    settle(60)
    writes = len(relay.published)
    store.mark(sid, 2)
    assert not store.flush()                      # too soon
    clock.advance(READ_STATE_INTERVAL_SECONDS - 1)
    assert not store.flush()
    assert store.flush(leaving=True)              # leaving the app
    settle(60)
    assert len(relay.published) == writes + 1
    store.mark(sid, 3)
    clock.advance(READ_STATE_INTERVAL_SECONDS)
    assert store.flush()
    settle(60)
    assert relay.page(formats.read_state_d(PK))["seen"] == {sid: 3}
    assert not store.flush()                      # nothing new


# -- sizes through a signer app (criterion 22) ---------------------------------------

def _through_signer_app(cls, user_sk):
    client, app = paired_bunker(user_sk)
    profile = SimpleNamespace(user_pubkey=app.user_pk, bunker_relays=[])
    relay = Relay()
    store = cls(profile, session_pool=FakeSessionPool(client),
                relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
                clock=FakeClock(NOW))
    return store, relay, app, client, profile


def _page_sizes(relay, d, app):
    sizes = []
    for tag, event in relay.stored.items():
        if tag == d or tag.startswith(d + ":"):
            text = crypto.decrypt_from(event["content"], app.user_sk,
                                       bytes.fromhex(app.user_pk))
            sizes.append(len(text.encode("utf-8")))
    return sizes


def test_a_list_of_500_submissions_goes_through_a_signer_app():
    store, relay, app, client, profile = _through_signer_app(SubmissionsList, sk("writer"))
    entries = [entry(n) for n in range(500)]
    result = record(store, *entries)
    assert result.status == SENT, result.reason
    sizes = _page_sizes(relay, formats.SUBMISSIONS_LIST_D, app)
    assert len(sizes) >= 10 and max(sizes) <= PAGE_PLAINTEXT_MAX
    assert app.too_large == 0 and app.largest_request <= crypto.NIP44_MAX_PLAINTEXT_LEN

    # Read back on another device, through the same signer app.
    other = SubmissionsList(profile, session_pool=FakeSessionPool(client),
                            relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
                            clock=FakeClock(NOW))
    assert load(other) == entries

    # One more submission rewrites the last page and page 0, not all.
    before = len(relay.published)
    record(other, entry(500))
    written = relay.published[before:]
    assert written[-1] == formats.SUBMISSIONS_LIST_D and len(written) <= 3
    client.close()


def test_a_read_state_of_500_submissions_goes_through_a_signer_app():
    store, relay, app, client, _profile = _through_signer_app(ReadState, sk("writer"))
    load(store)
    for n in range(500):
        store.mark(formats.sha256_hex(f"sid-{n}"), 10_000 + n)
    assert store.flush(leaving=True)
    settle(80)
    sizes = _page_sizes(relay, formats.read_state_d(app.user_pk), app)
    assert len(sizes) >= 2 and max(sizes) <= PAGE_PLAINTEXT_MAX
    assert app.too_large == 0
    client.close()


def _largest_message(sender: str, room: str) -> str:
    """The longest text whose rumor still fits 32,768 bytes, in letters
    that take three bytes each."""
    low, high = 1, formats.MAX_MESSAGE_CHARS
    while low < high:
        middle = (low + high + 1) // 2
        try:
            conversation.message_rumor(room=room, submission_id="ab" * 32,
                                       text="€" * middle, sender=sender, created_at=NOW)
            low = middle
        except conversation.MessageRefused:
            high = middle - 1
    return "€" * low


@pytest.mark.parametrize("shape", ["most characters", "most bytes"])
def test_a_message_at_the_largest_size_goes_through_a_signer_app(shape):
    client, app = paired_bunker(sk("writer"))
    room_sk = scrypto.new_secret_key()
    room = scrypto.pubkey_of(room_sk)
    sid = "ab" * 32
    text = ("x" * formats.MAX_MESSAGE_CHARS if shape == "most characters"
            else _largest_message(app.user_pk, room))
    rumor = conversation.message_rumor(room=room, submission_id=sid, text=text,
                                       sender=app.user_pk, created_at=NOW)
    assert conversation.rumor_size(rumor) <= formats.MAX_RUMOR_BYTES
    outcome = []
    conversation.seal_and_wrap(client, rumor, room_sk, room,
                               on_success=lambda wrap: outcome.append(("ok", wrap)),
                               on_failure=lambda reason: outcome.append(("err", reason)),
                               clock=lambda: NOW)
    settle(40)
    assert outcome and outcome[0][0] == "ok", outcome
    assert app.too_large == 0
    opened = conversation.open_item(outcome[0][1], room_sk, sid)
    assert opened.rumor["content"] == text and opened.sender == app.user_pk
    # One character more is refused before the signer app is asked.
    seen = len(app.seen)
    with pytest.raises(conversation.MessageRefused):
        conversation.message_rumor(room=room, submission_id=sid, text=text + "€",
                                   sender=app.user_pk, created_at=NOW)
    assert len(app.seen) == seen
    client.close()

