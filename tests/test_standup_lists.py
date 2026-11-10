# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writer's submissions list and read state (nostr/standup/lists.py,
what they hold in nostr/standup/entries.py, editorial contract 13), on
the shared encrypted list store.

Pinned here: an entry is on the relays before its submission may be
sent; entries merge by ``sid`` with the newest ``updated_at``; the read
state by the highest ``seq``; secrets never print; everything is per
account. And criterion 22 of the plan: a list of 500 submissions, a read
state of 500, and a message at the largest size the contract allows go
through a NIP-46 signer app whose answers must each fit one NIP-44
message."""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from nostr import crypto, events
from nostr.signer_limits import SIGNER_NIP44_MAX_PLAINTEXT
from nostr.list_results import PublishStatus, ReadStatus
from nostr.local_signer import LocalSigner
from nostr.paged_list import MAX_PAGES, PAGE_PLAINTEXT_MAX, PageError, Paging, digest, parse_page
from nostr.standup import conversation, formats
from nostr.standup import crypto as scrypto
from nostr.standup.entries import (
    ForeignItem,
    RecordStatus,
    SubmissionEntry,
    SubmissionItems,
    Tombstone,
    decode_items,
    encode_items,
    entry_from_json,
    holds_exactly,
    is_tombstone,
    item_from_json,
    merge_items,
    merge_seen,
    record_json,
    record_key,
    shown_for,
)
from nostr.standup.lists import (
    READ_STATE_INTERVAL_SECONDS,
    SIGN_OUT_WAIT_MS,
    ReadState,
    SubmissionsList,
)
from tests.membership_fakes import FakeClock
from tests.outbox_fakes import PK, FakeRelayDirectory, settle
from tests.standup_fakes import paired_bunker
from tests.standup_vectors import ADDRESS, NOW, VECTORS, sk
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
        revision_hash=formats.sha256_hex(f"rev-{n}-{revision}"),
        envelope_sha256=formats.sha256_hex(f"envelope-{n}-{revision}"),
        wraps_sha256={"redaktion": formats.sha256_hex(f"wrap-r-{n}-{revision}"),
                      "writer": formats.sha256_hex(f"wrap-w-{n}-{revision}")},
        updated_at=updated_at)
    return replace(value, **changes)


def make(cls=SubmissionsList, *, relay=None, signer=None, profile=PROFILE, clock=None):
    relay = relay if relay is not None else Relay()
    clock = clock or FakeClock(NOW)
    return cls(profile, session_pool=FakeSessionPool(signer or FakeSigner()),
               relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
               clock=clock), relay, clock


def record(store, *entries):
    """The last entry's RecordResult (all share one write)."""
    results = []
    store.record_many(list(entries), results.append)
    settle(60)
    assert results, "the write never finished"
    assert len(results) == 1 and len(results[0]) == len(entries)
    return results[0][-1]


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
                         "envelope_sha256", "wraps_sha256", "stored", "updated_at"}
    assert entry_from_json(data) == value and data["stored"] is False


def test_an_entry_of_a_list_before_stored_is_read_as_not_known_to_be_stored():
    # 72d5ddc added ``stored``; an older list's entry is still an entry.
    data = entry().to_json()
    del data["stored"]
    read = entry_from_json(data)
    assert isinstance(read, SubmissionEntry) and read.stored is False
    assert read.to_json() == dict(data, stored=False)


@pytest.mark.parametrize("change", [{"stored": "yes"}, {"stored": 1}, {"stored": None}])
def test_a_stored_that_is_no_bool_is_not_understood(change):
    data = dict(entry().to_json(), **change)
    item = item_from_json(data)
    assert isinstance(item, ForeignItem) and item.to_json() == data


def test_a_keyed_tombstone_round_trips_and_one_with_a_bad_key_is_kept_as_it_came():
    from nostr.standup.entries import status_key_hash, tombstone_for
    value = entry()
    gone = tombstone_for(value, 99)
    assert gone.status_key_hash == status_key_hash(value.status_key)
    assert gone.to_json() == {"sid": value.sid, "deleted": True,
                              "status_key_hash": gone.status_key_hash, "updated_at": 99}
    assert item_from_json(gone.to_json()) == gone
    broken = dict(gone.to_json(), status_key_hash="zz")
    assert isinstance(item_from_json(broken), ForeignItem)


def test_a_keyed_tombstone_leaves_an_entry_of_another_key_whatever_its_age():
    from nostr.standup.entries import tombstone_for
    mine, other = entry(), entry(status_key="77" * 32)
    gone = tombstone_for(mine, other.updated_at + 1_000)
    for first, second in ((gone, other), (other, gone)):
        merged = merge_items(SubmissionItems((first,)), SubmissionItems((second,)))
        # Two records (0456312): each of its own identity; the sid shows
        # the entry the tombstone does not name.
        assert set(merged.items) == {gone, other} and merged.entries == (other,)
        assert shown_for(merged, other.sid) == other
    newer = replace(mine, updated_at=gone.updated_at + 5)            # its own key, newer
    assert merge_items(SubmissionItems((gone,)), SubmissionItems((newer,))).items == (newer,)


def test_an_entry_without_what_was_sent_is_kept_as_it_came():
    # Contract 13 (72d688b): a writer entry has every field, the
    # envelope's and the wraps' hashes too, or it is not understood.
    for name in ("envelope_sha256", "wraps_sha256"):
        data = entry().to_json()
        del data[name]
        assert entry_from_json(data) is None
        assert isinstance(item_from_json(data), ForeignItem)


@pytest.mark.parametrize("envelope, wraps, understood", [
    ("ab" * 32, {"redaktion": "cd" * 32, "writer": "ef" * 32}, True),
    (None, None, True),                                     # a work by link
    (None, {"writer": "ef" * 32}, False),
    ("ab" * 32, None, False),
    ("ab" * 32, {}, False),
    ("ab" * 32, {"writer": "EF" * 32}, False),
    ("ab" * 32, {"writer": None}, False),
    ("ab" * 32, {"": "ef" * 32}, False),
    ("ab" * 32, ["ef" * 32], False),
    ("AB" * 32, {"writer": "ef" * 32}, False),
    ("ab" * 31, {"writer": "ef" * 32}, False),
    ("ab" * 32, {f"{n:064x}": "ef" * 32 for n in range(23)}, False),
])
def test_what_was_sent_is_an_envelope_hash_with_each_wraps_or_neither(envelope, wraps,
                                                                       understood):
    # As the web app reads them (list-entries.js sentHashes): null for a
    # work by link, else a 64-hex envelope hash and a non-empty map of
    # 64-hex wrap hashes, for a writer and an opened contributor alike.
    for role in ({}, {"role": "contributor", "status_key": None}):
        data = dict(entry().to_json(), envelope_sha256=envelope, wraps_sha256=wraps, **role)
        assert (entry_from_json(data) is not None) is understood, role


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


def test_a_contributor_has_no_status_key_and_no_key_or_revision_until_the_wrap_opened():
    # 13 (071f757): status_key null; s_sub, revision and revision_hash
    # null until the contributor opened their wrap, then all three.
    # 72d688b: the envelope's and the wraps' hashes are null with them.
    unopened = dict(entry().to_json(), role="contributor", status_key=None, s_sub=None,
                    revision=None, revision_hash=None, envelope_sha256=None, wraps_sha256=None)
    assert entry_from_json(unopened).role == "contributor"
    opened = dict(entry().to_json(), role="contributor", status_key=None)
    assert entry_from_json(opened).s_sub == opened["s_sub"]
    for wrong in (dict(unopened, revision=1), dict(opened, s_sub=None),
                  dict(unopened, envelope_sha256="ab" * 32),
                  dict(unopened, wraps_sha256={"writer": "ab" * 32}),
                  dict(opened, status_key=scrypto.new_status_key()),
                  dict(entry().to_json(), revision=None, revision_hash=None)):
        assert entry_from_json(wrong) is None
        assert isinstance(item_from_json(wrong), ForeignItem)


def test_entries_merge_by_id_and_the_newest_wins_in_place():
    a, b, c = entry(1), entry(2), entry(3)
    newer_b = replace(b, revision=2, updated_at=NOW + 5)
    older_a = replace(a, title="älter", updated_at=NOW - 5)
    merged = merge_items(SubmissionItems((a, b)), SubmissionItems((older_a, newer_b, c)))
    assert merged.entries == (a, newer_b, c)


def test_a_removal_is_a_tombstone_that_wins_over_older_copies():
    from nostr.standup.entries import tombstone_for
    a = entry(1)
    gone = tombstone_for(a, NOW + 1)
    assert item_from_json(gone.to_json()) == gone
    merged = merge_items(SubmissionItems((a,)), SubmissionItems((gone,)))
    assert merged.items == (gone,) and merged.entries == ()
    # An older copy from another device brings nothing back.
    assert merge_items(merged, SubmissionItems((a,))).items == (gone,)


def test_a_tombstone_of_an_older_list_touches_no_entry_with_a_key():
    # 0456312: one naming no key is a record of the identity with none.
    a = entry(1)
    unkeyed = Tombstone(sid=a.sid, updated_at=NOW + 1)
    assert item_from_json(unkeyed.to_json()) == unkeyed
    merged = merge_items(SubmissionItems((a,)), SubmissionItems((unkeyed,)))
    assert set(merged.items) == {a, unkeyed} and merged.entries == (a,)


# -- written before sending ------------------------------------------------------

def test_an_entry_is_on_the_relays_before_the_submission_may_be_sent():
    store, relay, _clock = make()
    value = entry()
    result = record(store, value)
    assert result.publish.status is PublishStatus.SENT and result.stored
    page = relay.page(formats.SUBMISSIONS_LIST_D)
    assert (page["v"], page["page"], page["pages"], page["parts"]) == (1, 0, 1, [])
    [item] = page["items"]
    assert (item["sid"], item["s_sub"], item["status_key"], item["revision"],
            item["revision_hash"]) == (value.sid, value.s_sub, value.status_key,
                                       value.revision, value.revision_hash)


def test_when_no_relay_answers_the_submission_is_not_sent():
    relay = Relay()
    relay.answers = False
    store, _relay, _clock = make(relay=relay)
    result = record(store, entry())
    assert result.publish.status is PublishStatus.NO_ANSWER and not result.stored
    assert relay.published == []


def test_a_list_that_cannot_be_opened_is_never_written_over_and_blocks_sending():
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, None, content="not a ciphertext")
    store, _relay, _clock = make(relay=relay)
    result = record(store, entry())
    assert result.publish.status is PublishStatus.BLOCKED and not result.stored
    assert result.reason == "Your list of submissions couldn’t be read: try again in a moment."
    assert relay.published == []


def test_a_list_in_a_newer_format_is_never_written_over_and_blocks_sending():
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 2, "page": 0, "pages": 1, "items": []})
    store, _relay, _clock = make(relay=relay)
    result = record(store, entry())
    assert result.publish.status is PublishStatus.UNREADABLE and not result.stored
    assert relay.published == []


def test_an_entry_another_device_replaced_meanwhile_is_superseded_not_stored():
    # RG-M1 1: another device (its clock ten minutes ahead) recorded a
    # different revision 2 of the same submission. This device has not
    # read the list since; its own revision 2 loses the merge, so the
    # list does not hold what it would send.
    mine = entry(1)
    other, relay, _clock = make()
    load(other)
    theirs = replace(mine, revision=2, revision_hash="cd" * 32, updated_at=NOW + 600)
    assert record(other, theirs).stored
    here, _relay, _clock = make(relay=relay)
    ours = replace(mine, revision=2, revision_hash="ef" * 32)
    result = record(here, ours)
    assert result.status is RecordStatus.SUPERSEDED and not result.stored
    assert result.on_list.revision_hash == "cd" * 32
    assert here.entry(mine.sid).revision_hash == "cd" * 32
    [kept] = [e for e in load(make(relay=relay)[0]).value.entries if e.sid == mine.sid]
    assert kept.revision_hash == "cd" * 32


def test_the_same_second_on_two_devices_is_superseded_too():
    # A tie goes to what the relays hold (13), so it is not ours.
    mine = entry(1)
    other, relay, _clock = make()
    assert record(other, replace(mine, s_sub="11" * 32)).stored
    here, _relay, _clock = make(relay=relay)
    result = record(here, mine)
    assert result.status is RecordStatus.SUPERSEDED


def test_an_entry_that_was_not_stored_never_goes_out_with_a_later_record():
    # RG-M1 1: the caller did not send it, so the list must not hold it.
    store, relay, _clock = make()
    load(store)
    relay.refuse = {formats.SUBMISSIONS_LIST_D}
    first = entry(1)
    result = record(store, first)
    assert result.status is RecordStatus.NOT_STORED and not result.stored
    assert store.entry(first.sid) is None
    relay.refuse = set()
    assert record(store, entry(2)).stored
    on_relays = {e.sid for e in load(make(relay=relay)[0]).value.entries}
    assert first.sid not in on_relays


def test_a_record_is_dated_after_every_item_of_its_sid_a_tombstone_too():
    value = entry(1)
    gone = Tombstone(sid=value.sid, updated_at=NOW + 300)
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [gone.to_json()]})
    store, _relay, _clock = make(relay=relay)
    load(store)
    result = record(store, value)
    assert result.stored and result.entry.updated_at == NOW + 301


def test_an_entry_held_is_exactly_the_one_recorded():
    value = entry(1)
    items = SubmissionItems((value,))
    assert holds_exactly(items, value)
    # 72d688b: what was sent too, so a lookup after a restart compares
    # what this run sent.
    for change in ({"revision": 2}, {"revision_hash": "00" * 32}, {"s_sub": "00" * 32},
                   {"status_key": "00" * 32}, {"role": "contributor"},
                   {"envelope_sha256": "00" * 32},
                   {"wraps_sha256": dict(value.wraps_sha256, writer="00" * 32)},
                   {"wraps_sha256": {"writer": value.wraps_sha256["writer"]}},
                   {"envelope_sha256": None, "wraps_sha256": None}):
        assert not holds_exactly(items, replace(value, **change)), change
    by_link = replace(value, envelope_sha256=None, wraps_sha256=None)
    assert holds_exactly(SubmissionItems((by_link,)), by_link)
    assert not holds_exactly(SubmissionItems((by_link,)), value)
    assert holds_exactly(SubmissionItems(), value) is None
    assert not holds_exactly(SubmissionItems((Tombstone(value.sid, NOW + 1),)), value)


def test_a_malformed_entry_is_refused_after_the_call_returned():
    store, relay, _clock = make()
    order = []
    store.record(replace(entry(1), sid="x"), lambda result: order.append(result.status))
    order.append("returned")
    settle(6)
    assert order == ["returned", RecordStatus.NOT_STORED] and relay.published == []


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
                                           "items": [value.to_json()]})
    store, _relay, _clock = make(relay=relay)
    assert load(store).value.entries == (value,)
    store.stop()
    assert store.entries() == [] and not store.loaded


def test_an_item_this_app_cannot_read_is_written_back_unchanged():
    # A newer app's entry (a kind this one does not know) or a broken
    # one: never shown, never deleted.
    relay = Relay()
    newer = dict(entry(7).to_json(), kind="video", chapters=[1, 2])
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [newer, {"sid": "broken"}]})
    store, _relay, _clock = make(relay=relay)
    loaded = load(store).value
    assert loaded.entries == ()
    assert [item.to_json() for item in loaded.unread] == [newer, {"sid": "broken"}]
    mine = entry(1)
    record(store, mine)
    items = relay.page(formats.SUBMISSIONS_LIST_D)["items"]
    assert items == [newer, {"sid": "broken"}, mine.to_json()]
    assert store.entries() == [mine]


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
    assert load(store).value == {sid_a: 9, sid_b: 2}
    store.mark(sid_a, 4)          # older: ignored
    store.mark(sid_b, 5)
    assert store.seen(sid_a) == 9 and store.seen(sid_b) == 5
    assert store.flush(leaving=True)
    settle(60)
    assert relay.page(d)["seen"] == {sid_a: 9, sid_b: 5}
    assert "einundzwanzig" not in d and "read-state" not in d


def test_keys_this_app_does_not_read_are_kept_through_every_write():
    # Contract 13 (5c11475): STANDUP keeps a member's internal read
    # position under i:<sid>; MyEditor must not drop it.
    relay = Relay()
    d = formats.read_state_d(PK)
    sid = formats.sha256_hex("a")
    relay.put(d, {"v": 1, "page": 0, "pages": 1,
                  "seen": {sid: 3, f"i:{sid}": 7, "future": {"x": 1}}})
    store, _relay, _clock = make(ReadState, relay=relay)
    load(store)
    store.mark(sid, 4)
    assert store.flush(leaving=True)
    settle(60)
    assert relay.page(d)["seen"] == {sid: 4, f"i:{sid}": 7, "future": {"x": 1}}
    assert merge_seen({f"i:{sid}": 7}, {f"i:{sid}": 9}) == {f"i:{sid}": 9}


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


def test_what_was_read_is_written_before_signing_out():
    # Contract 13 (d5cc9a7): before signing out, at most 5 s.
    assert SIGN_OUT_WAIT_MS == 5_000
    store, relay, _clock = make(ReadState)
    load(store)
    sid = formats.sha256_hex("a")
    store.mark(sid, 4)
    store.flush()
    settle(60)
    store.mark(sid, 5)                        # read within the five minutes
    answers = []
    store.before_sign_out(answers.append)
    settle(60)
    assert len(answers) == 1 and answers[0].on_relays
    assert relay.page(formats.read_state_d(PK))["seen"] == {sid: 5}
    settle(60)
    assert len(answers) == 1                  # once, the timer changes nothing


def wait_until(done, seconds: float) -> None:
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def test_signing_out_waits_for_a_slow_signer_at_most_the_bound():
    signer = FakeSigner(hold=True)
    store, relay, _clock = make(ReadState, signer=signer)
    load(store)
    store.mark(formats.sha256_hex("a"), 1)
    answers = []
    store.before_sign_out(answers.append, wait_ms=50)
    settle(10)
    assert answers == []
    wait_until(lambda: answers, 2.0)
    assert answers == [None] and relay.published == []
    signer.release()                          # the phone answers late
    settle(60)
    assert answers == [None]


def test_with_nothing_read_signing_out_does_not_wait():
    store, relay, _clock = make(ReadState)
    load(store)
    answers = []
    store.before_sign_out(answers.append)
    assert answers == []                        # on a later turn (RG-M2a 6)
    QCoreApplication.processEvents()
    assert answers == [None] and relay.published == []


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
    assert result.stored and result.publish.status is PublishStatus.SENT, result.reason
    sizes = _page_sizes(relay, formats.SUBMISSIONS_LIST_D, app)
    assert len(sizes) >= 10 and max(sizes) <= PAGE_PLAINTEXT_MAX
    assert app.too_large == 0 and app.largest_request <= crypto.NIP44_MAX_PLAINTEXT_LEN
    assert 0 < app.largest_plaintext <= SIGNER_NIP44_MAX_PLAINTEXT

    # Read back on another device, through the same signer app.
    other = SubmissionsList(profile, session_pool=FakeSessionPool(client),
                            relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
                            clock=FakeClock(NOW))
    assert list(load(other).value.entries) == entries

    # One more submission signs the last page and page 0 again, not all.
    before = {d: event["id"] for d, event in relay.stored.items()}
    record(other, entry(500))
    signed = {d for d, event in relay.stored.items() if before.get(d) != event["id"]}
    assert formats.SUBMISSIONS_LIST_D in signed and len(signed) <= 3
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
    assert app.too_large == 0 and app.largest_plaintext <= SIGNER_NIP44_MAX_PLAINTEXT
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
    # The seal is encrypted with the room key here; the signer app only
    # signs it, and that request, the largest one, still fits one message.
    assert app.largest_plaintext == 0
    assert len(text.encode("utf-8")) < app.largest_request <= crypto.NIP44_MAX_PLAINTEXT_LEN
    opened = conversation.open_item(outcome[0][1], room_sk, sid)
    assert opened.rumor["content"] == text and opened.sender == app.user_pk
    # One character more is refused before the signer app is asked.
    seen = len(app.seen)
    with pytest.raises(conversation.MessageRefused):
        conversation.message_rumor(room=room, submission_id=sid, text=text + "€",
                                   sender=app.user_pk, created_at=NOW)
    assert len(app.seen) == seen
    client.close()



# -- the contract's vectors (paged_list, contract 13) ----------------------------------

PL = VECTORS["paged_list"]
WRITER = SimpleNamespace(user_pubkey=PL["writer"], bunker_relays=[])


def by_sid(items):
    return sorted(items, key=lambda item: item["sid"])


def writers_list(cls, relay):
    """The vector writer's list, decrypted with the writer's own key."""
    return cls(WRITER, session_pool=FakeSessionPool(LocalSigner(sk("writer"))),
               relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
               clock=FakeClock(NOW))


def test_the_vectors_name_these_limits():
    assert (PL["max_pages"], PL["max_page_plaintext_bytes"]) == (MAX_PAGES, PAGE_PLAINTEXT_MAX)


def test_the_vector_pages_are_bound_by_their_parts():
    first = parse_page(PL["plaintexts"][0], index=0, paging=Paging("items"))
    assert first.parts == tuple(PL["parts"])
    assert first.parts == tuple(digest(text) for text in PL["plaintexts"][1:])


@pytest.mark.parametrize("name", sorted(PL["cases"]))
def test_the_vector_reads(name):
    # The events carry no "encrypted" tag: contract 13 says NIP-44 to
    # self, so STANDUP's web app writes none.
    case = PL["cases"][name]
    relay = Relay()
    for ref in case["pages"]:
        event = case["old_1"] if ref == "old_1" else PL["events"][ref]
        relay.stored[event["tags"][0][1]] = event
    answer = load(writers_list(SubmissionsList, relay))
    assert answer.status.value == case["status"]
    assert by_sid(item.to_json() for item in answer.value.items) == case["items"]
    # 72d688b: every entry the vectors hold with what was sent is read as
    # an entry, its hashes as they came, not kept as one not understood.
    for item in case["items"]:
        if "envelope_sha256" in item:
            [read] = [e for e in answer.value.entries if e.sid == item["sid"]]
            assert (read.envelope_sha256, read.wraps_sha256) \
                == (item["envelope_sha256"], item["wraps_sha256"])


def test_the_vector_merge_is_a_union():
    def items(name):
        return SubmissionItems(tuple(item_from_json(i) for i in PL["merge"][name]))

    merged = merge_items(items("remote"), items("local"))
    assert by_sid(item.to_json() for item in merged.items) == PL["merge"]["merged"]
    assert any(isinstance(item, Tombstone) for item in merged.items)


def _by_identity(items):
    """Records as the vectors list them: by sid, then by record key."""
    return sorted((item.to_json() for item in items),
                  key=lambda value: (value["sid"], record_key(item_from_json(value))))


@pytest.mark.parametrize("case", PL["merge_variants"], ids=lambda case: case["name"])
def test_every_order_of_the_vector_records_merges_to_one_list(case):
    # 0456312: a maximum under one order, so every order and grouping of
    # the records gives the same list, through the one merge of the list
    # and through one read across generations (decode_items); and the
    # sid shows what the vectors say (shown_for).
    sid = case["records"][0]["sid"]
    for order in itertools.permutations(case["records"]):
        records = [item_from_json(value) for value in order]
        one_by_one = SubmissionItems()
        for record in records:
            one_by_one = merge_items(one_by_one, SubmissionItems((record,)))
        halves = merge_items(SubmissionItems(tuple(records[:1])),
                             SubmissionItems(tuple(records[1:])))
        decoded = decode_items({"items": list(order)})
        for merged in (one_by_one, halves, decoded):
            assert _by_identity(merged.items) == case["merged"]
            shown = shown_for(merged, sid)
            assert (shown.to_json() if shown is not None else None) == case["shown"]


def test_the_vector_merge_variants_are_the_contracts_ten():
    # c2aa466 added the tenth: equal in stored, time and tombstone, so only
    # the fourth step, the canonical hash of the record as read, decides.
    names = {case["name"] for case in PL["merge_variants"]}
    assert names == {
        "stored_beats_a_newer_unstored", "keyed_tombstone_removes_only_its_key",
        "keyed_tombstone_never_removes_another_key", "same_key_newest_wins",
        "two_unstored_keys_show_the_newest", "stored_beats_a_tombstone_of_its_own_key",
        "unkeyed_tombstone_touches_no_keyed_record", "no_cycle_between_keys",
        "tombstone_wins_at_equal_time", "equal_rank_the_greater_hash_wins"}


def test_the_vector_read_state_merge():
    merge = PL["read_state_merge"]
    assert merge_seen(merge["remote"], merge["local"]) == merge["merged"]


def test_the_vector_list_of_65_pages_is_unreadable():
    with pytest.raises(PageError):
        parse_page(PL["too_many_pages"]["plaintext"], index=0, paging=Paging("items"))
    writer_sk = sk("writer")
    content = crypto.encrypt(PL["too_many_pages"]["plaintext"], crypto.conversation_key(
        writer_sk, bytes.fromhex(PL["writer"])))
    relay = Relay()
    relay.stored[formats.SUBMISSIONS_LIST_D] = events.sign_event(
        {"kind": 30078, "content": content, "created_at": NOW,
         "tags": [["d", formats.SUBMISSIONS_LIST_D]]}, writer_sk)
    store = writers_list(SubmissionsList, relay)
    assert load(store).status is ReadStatus.UNREADABLE
    assert not record(store, entry()).stored


def test_the_vector_entry_of_a_newer_app_is_kept_by_sid_and_written_back_unchanged():
    newer = next(i for i in PL["cases"]["complete"]["items"] if i.get("v") == 2)
    item = item_from_json(newer)
    assert isinstance(item, ForeignItem) and item.sid == newer["sid"]
    assert item.to_json() == newer


@pytest.mark.parametrize("change", [
    {"kind": "video"}, {"kind": 30023}, {"role": "translator"}, {"v": 2}, {"v": "1"},
    {"round": "1"}, {"updated_at": True},
])
def test_an_entry_this_app_cannot_interpret_is_kept_as_it_came(change):
    data = dict(entry().to_json(), **change)
    item = item_from_json(data)
    assert isinstance(item, ForeignItem) and item.to_json() == data


def test_a_field_this_app_does_not_know_is_kept_with_the_entry():
    future = next(i for i in PL["cases"]["complete"]["items"] if "x_future_field" in i)
    item = item_from_json(future)
    assert isinstance(item, SubmissionEntry) and item.to_json() == future
    newer = replace(item, title="Neu", updated_at=item.updated_at + 1)
    assert newer.to_json()["x_future_field"] == [1, 2]
    relay = Relay()
    store = writers_list(SubmissionsList, relay)
    load(store)
    assert record(store, newer).stored
    again = load(writers_list(SubmissionsList, relay)).value.entries[0]
    assert again.title == "Neu" and again.to_json()["x_future_field"] == [1, 2]


def test_an_item_without_sid_or_updated_at_is_kept_once_by_its_canonical_text():
    one = {"note": "kaputt", "n": [1, 2]}
    same = {"n": [1, 2], "note": "kaputt"}
    floats = {"x": 1.5}
    merged = merge_items(SubmissionItems((item_from_json(one), item_from_json(floats))),
                         SubmissionItems((item_from_json(same), item_from_json(dict(floats)))))
    assert [item.to_json() for item in merged.items] == [one, floats]
    keyless = dict(entry().to_json(), updated_at="gestern")
    assert item_from_json(keyless).sid is None


def test_an_incomplete_list_is_made_whole_by_the_next_write():
    relay = Relay()
    for ref in PL["cases"]["missing_page_2"]["pages"]:
        event = PL["events"][ref]
        relay.stored[event["tags"][0][1]] = event
    store = writers_list(SubmissionsList, relay)
    assert load(store).status is ReadStatus.INCOMPLETE and not store.complete
    assert record(store, entry()).stored
    assert store.complete
    again = load(writers_list(SubmissionsList, relay))
    assert again.status is ReadStatus.COMPLETE
    assert len(again.value.items) == len(PL["cases"]["missing_page_2"]["items"]) + 1


def test_an_incomplete_read_state_is_written_whole_at_the_next_flush():
    relay = Relay()
    d = formats.read_state_d(PK)
    relay.put(d, {"v": 1, "page": 0, "pages": 2, "parts": ["ab" * 32], "seen": {}})
    store, _relay, _clock = make(ReadState, relay=relay)
    assert load(store).status is ReadStatus.INCOMPLETE and store.dirty
    assert store.flush(leaving=True)
    settle(60)
    assert relay.page(d)["pages"] == 1 and store.complete and not store.dirty


# -- one run per submission: its keys are never replaced silently (RG-M2b 1) -----

def _done(call):
    results = []
    call(results.append)
    settle(60)
    assert results, "the write never finished"
    return results[0]


def test_an_entry_with_other_keys_never_replaces_the_one_on_the_list():
    store, relay, _clock = make()
    load(store)
    first = entry(1)
    assert record(store, first).stored
    second = entry(1)                               # the same sid, another run's keys
    result = _done(lambda answer: store.record(second, answer))
    assert result.status is RecordStatus.NOT_STORED
    assert store.entry(first.sid).s_sub == first.s_sub
    other, _relay, _clock = make(relay=relay)
    assert [e.s_sub for e in load(other).value.entries] == [first.s_sub]


def test_a_later_entry_of_a_sid_keeps_its_keys_and_takes_its_place():
    store, _relay, _clock = make()
    load(store)
    first = entry(1)
    assert record(store, first).stored
    later = replace(first, revision=2, revision_hash=formats.sha256_hex("rev-1-2"))
    assert record(store, later).stored
    assert store.entry(first.sid).revision == 2


def test_an_entry_this_app_cannot_read_is_never_replaced_under_its_sid():
    # It may hold the keys of a stored revision (a newer app's entry).
    mine = entry(1)
    newer = dict(mine.to_json(), kind="video", s_sub="77" * 32)
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1, "items": [newer]})
    store, _relay, _clock = make(relay=relay)
    load(store)
    result = _done(lambda answer: store.record(mine, answer))
    assert result.status is RecordStatus.NOT_STORED
    assert relay.page(formats.SUBMISSIONS_LIST_D)["items"] == [newer]


def test_another_devices_entry_with_other_keys_stays_and_this_write_is_superseded():
    store, relay, _clock = make()
    load(store)
    elsewhere, _relay, _clock = make(relay=relay)
    load(elsewhere)
    theirs = entry(1)
    assert record(elsewhere, theirs).stored
    mine = entry(1, updated_at=NOW + 100)           # newer, but another run's keys
    result = _done(lambda answer: store.record(mine, answer))
    assert result.status is RecordStatus.SUPERSEDED
    check, _relay, _clock = make(relay=relay)
    assert [e.s_sub for e in load(check).value.entries] == [theirs.s_sub]


def test_an_entry_taken_back_leaves_a_tombstone_and_another_runs_entry_is_kept():
    store, relay, _clock = make()
    load(store)
    taken = entry(1)
    assert record(store, taken).stored
    assert _done(lambda answer: store.forget(taken, answer)).stored
    assert store.entry(taken.sid) is None
    other, _relay, _clock = make(relay=relay)
    assert load(other).value.entries == ()
    # A run with other keys is never taken back by this one's cancel.
    kept = entry(2)
    assert record(store, kept).stored
    assert _done(lambda answer: store.forget(entry(2), answer)).status is \
        RecordStatus.NOT_STORED
    assert store.entry(kept.sid).s_sub == kept.s_sub


def test_an_entry_a_later_page_let_out_keeps_its_keys_and_nothing_takes_their_place():
    # RG-M2a 4: page 0 refused, the later pages taken: the entry reaches
    # the list though its write failed. Under contract 2 that is
    # harmless: it was never sent, so another device sends it again with
    # its own keys (Erneut) or discards it, and an entry with other keys
    # never takes the place of one it reads.
    store, relay, _clock = make()
    load(store)
    assert record(store, *[entry(n) for n in range(300)]).stored
    relay.refuse = {formats.SUBMISSIONS_LIST_D}     # page 0 only
    let_out = entry(999)
    assert record(store, let_out).status is RecordStatus.NOT_STORED
    relay.refuse = set()
    elsewhere, _relay, _clock = make(relay=relay)
    read = load(elsewhere).value
    assert [e.s_sub for e in read.entries if e.sid == let_out.sid] == [let_out.s_sub]
    other_keys = entry(999)
    assert _done(lambda answer: elsewhere.record(other_keys, answer)).status is \
        RecordStatus.NOT_STORED
    assert record(elsewhere, replace(let_out, title="Erneut")).stored
    # The device that let it out, not knowing: its new keys never win.
    assert _done(lambda answer: store.record(other_keys, answer)).status is \
        RecordStatus.SUPERSEDED
    check, _relay, _clock = make(relay=relay)
    assert [e.s_sub for e in load(check).value.entries if e.sid == let_out.sid] == [
        let_out.s_sub]


# -- partial failure is silent (lessons 36) -------------------------------------------

class SomeAnswer(Relay):
    """Relays of which only the first answers a read; the others are
    down, or slow, as some always are."""

    def events(self, relays, filters, on_done):
        self.queries.append(filters)
        found = [self.stored[d] for d in filters[0]["#d"] if d in self.stored]
        on_done(found, set(list(relays)[:1]))


def test_relays_that_refuse_the_list_while_one_takes_it_say_nothing():
    store, relay, _clock = make()
    load(store)
    record(store, entry(1))
    every = relay.sent_to[formats.SUBMISSIONS_LIST_D]
    for url in every[1:]:                       # all but one refuse it
        relay.refuse_at[url] = {formats.SUBMISSIONS_LIST_D}
    result = record(store, entry(2))
    assert result.stored and result.reason == ""
    assert result.publish.status is PublishStatus.SENT


def test_a_list_read_that_some_relays_did_not_answer_is_whole_and_says_nothing():
    relay = SomeAnswer()
    writer, _relay, _clock = make(relay=relay)
    load(writer)
    assert record(writer, entry(1)).stored
    store, _relay, _clock = make(relay=relay)       # another device of this account
    read = load(store)
    assert read.status is ReadStatus.COMPLETE and read.readable and not read.words
    assert store.complete and [e.sid for e in store.entries()] == [entry(1).sid]


# -- a quiet re-read asks the signer nothing it asked before (follow-up 2, 2) ---------

class CountingSigner(FakeSigner):
    """Counts each page sent to be opened."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.opened = []

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **kw):
        self.opened.append(ciphertext)
        super().nip44_decrypt_self(ciphertext, on_success, on_failure, **kw)


def _written_elsewhere(relay, entries):
    """The list as another device wrote it: nothing of it opened here."""
    other, _relay, _clock = make(relay=relay)
    load(other)
    assert record(other, *entries).stored


def test_a_quiet_re_read_of_unchanged_pages_asks_the_signer_nothing():
    relay = Relay()
    _written_elsewhere(relay, [entry(n) for n in range(300)])
    pages = len(relay.stored)
    assert pages > 1
    signer = CountingSigner()
    store, _relay, _clock = make(relay=relay, signer=signer)
    first = load(store)
    assert len(first.value.entries) == 300 and len(signer.opened) == pages
    signer.opened.clear()
    again = load(store)                                 # the same pages, read quietly again
    assert signer.opened == []
    assert again.value.entries == first.value.entries and store.complete


def test_a_re_read_asks_only_for_the_pages_it_has_not_seen():
    relay = Relay()
    _written_elsewhere(relay, [entry(n) for n in range(300)])
    signer = CountingSigner()
    store, _relay, _clock = make(relay=relay, signer=signer)
    load(store)
    before = dict(relay.stored)
    _written_elsewhere(relay, [entry(300)])             # another device adds one
    changed = [d for d, event in relay.stored.items() if before.get(d) != event]
    assert changed and len(changed) < len(relay.stored)
    signer.opened.clear()
    read = load(store)
    assert sorted(signer.opened) == sorted(relay.stored[d]["content"] for d in changed)
    assert len(read.value.entries) == 301


def test_pages_written_here_are_read_back_without_asking():
    signer = CountingSigner()
    store, relay, _clock = make(signer=signer)
    load(store)
    assert record(store, *[entry(n) for n in range(300)]).stored
    signer.opened.clear()
    assert len(load(store).value.entries) == 300
    assert signer.opened == []


def test_the_pages_opened_go_with_the_account():
    # Lesson 31: nothing of an account that was let go stays readable.
    relay = Relay()
    _written_elsewhere(relay, [entry(n) for n in range(3)])
    signer = CountingSigner()
    store, _relay, _clock = make(relay=relay, signer=signer)
    load(store)
    assert len(store._opened) == len(relay.stored)
    store.stop()
    assert len(store._opened) == 0
    again, _relay, _clock = make(relay=relay, signer=signer)   # signed in again
    signer.opened.clear()
    load(again)
    assert len(signer.opened) == len(relay.stored)


def test_a_page_counts_as_seen_only_by_its_id_and_its_content_and_only_for_its_account():
    from nostr.decrypted_pages import DecryptedPages

    pages = DecryptedPages(PK, limit=2)
    event = {"id": "a" * 64, "content": "ENC[x]"}
    pages.put(event, "x")
    assert pages.get(event) == "x"
    assert pages.get(dict(event, content="ENC[y]")) is None    # same id, other content
    assert pages.get(dict(event, id="b" * 64)) is None
    assert pages.belongs_to(PK.upper()) and not pages.belongs_to("cd" * 32)
    pages.put({"id": "b" * 64, "content": "1"}, "1")
    pages.put({"id": "c" * 64, "content": "2"}, "2")           # the oldest goes
    assert pages.get(event) is None and len(pages) == 2
    pages.put({"content": "no id"}, "never kept")               # nothing to know it by
    assert pages.get({"content": "no id"}) is None and pages.get({"id": "c" * 64,
                                                                   "content": "2"}) == "2"


def test_another_accounts_opened_pages_are_never_used():
    from nostr.decrypted_pages import DecryptedPages

    relay = Relay()
    _written_elsewhere(relay, [entry(n) for n in range(3)])
    signer = CountingSigner()
    store, _relay, _clock = make(relay=relay, signer=signer)
    store._list._opened = DecryptedPages("cd" * 32)             # not this account's
    load(store)
    load(store)
    assert len(signer.opened) == 2 * len(relay.stored)


# -- records (contract 13 at 0456312) ------------------------------------------------

def test_a_stored_entry_is_never_forgotten():
    # RG-M2r3 18 (c3): Verwerfen of an entry the server stored writes
    # nothing, and the list keeps it; the merge says so, not a second rule.
    store, relay, _clock = make()
    load(store)
    value = replace(entry(1), stored=True)
    assert record(store, value).stored
    before = relay.page(formats.SUBMISSIONS_LIST_D)
    result = _done(lambda answer: store.forget(value, answer))
    assert result.status is RecordStatus.NOT_STORED
    assert relay.page(formats.SUBMISSIONS_LIST_D) == before         # nothing written
    assert store.entry(value.sid).stored


def test_once_a_sid_shows_a_stored_entry_its_records_left_behind_get_tombstones():
    # 13 at 0456312, Removing: a device's own entry of a round another
    # device stored is never sent; after a complete read it is removed.
    theirs = replace(entry(1), stored=True)
    mine = entry(1, updated_at=NOW + 50)            # same sid, other keys, newer
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [theirs.to_json(), mine.to_json()]})
    store, _relay, _clock = make(relay=relay)
    read = load(store)
    assert read.value.entries == (theirs,)          # the stored one shows
    settle(60)
    items = relay.page(formats.SUBMISSIONS_LIST_D)["items"]
    from nostr.standup.entries import status_key_hash
    assert {"sid": mine.sid, "deleted": True, "status_key_hash": status_key_hash(
        mine.status_key), "updated_at": NOW + 51} in items
    assert theirs.to_json() in items
    again, _relay, _clock = make(relay=relay)
    assert load(again).value.entries == (theirs,)
    settle(60)
    assert relay.page(formats.SUBMISSIONS_LIST_D)["items"] == items     # nothing more


def test_two_devices_records_of_one_sid_both_stay():
    # 0456312: records of different keys never affect each other; the
    # sid shows the newest until one is stored, then the stored one.
    a, b = entry(1), entry(1, updated_at=NOW + 10)
    merged = merge_items(SubmissionItems((a,)), SubmissionItems((b,)))
    assert set(merged.items) == {a, b} and shown_for(merged, a.sid) == b
    stored_a = replace(a, stored=True)
    merged = merge_items(merged, SubmissionItems((stored_a,)))
    assert set(merged.items) == {stored_a, b} and shown_for(merged, a.sid) == stored_a


def _every_order(*records):
    """Each order of ``records``, merged one by one."""
    for order in itertools.permutations(records):
        merged = SubmissionItems()
        for record in order:
            merged = merge_items(merged, SubmissionItems((record,)))
        yield merged


def test_two_records_of_one_identity_at_one_time_keep_the_greater_hash_in_every_order():
    # 13 at 0456312: the last step of the one order, so devices agree.
    a = entry(1)
    b = replace(a, title="Ein anderer Titel")
    greater = max((a, b), key=lambda x: _digest_as_read(x.to_json()))
    for merged in _every_order(a, b):
        assert merged.items == (greater,)


# -- the 4th step: the record as it was read (RG-FMd-a) -----------------------------

def _digest_as_read(record):
    """The contract's 4th step, computed here on its own: the SHA-256 of
    the record exactly as read, its own fields and nothing added."""
    import hashlib
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tie_pairs(*, stored: bool, unknown: bool = False):
    """Two records of one identity, one time and both unstored, that only
    the 4th step tells apart: the contract's fixture entry with another
    title each, 200 times (the gate's probe)."""
    fixtures = json.loads((Path(__file__).parent / "fixtures"
                           / "standup_editorial_fixtures.json").read_text(encoding="utf-8"))
    base = dict(fixtures["paged_list"]["merge_variants"][0]["records"][1])
    assert base["stored"] is False and base["role"] == "writer"
    for n in range(200):
        a, b = dict(base, title=f"Titel A{n}"), dict(base, title=f"Titel B{n}")
        for record in (a, b):
            if not stored:
                record.pop("stored")
            if unknown:
                record["note_of_a_newer_app"] = f"n{n}"
        yield a, b


@pytest.mark.parametrize("stored, unknown", [(True, False), (False, False), (False, True),
                                             (True, True)],
                         ids=["with stored", "no stored", "no stored, unknown field",
                              "unknown field"])
def test_a_tie_keeps_the_greater_hash_of_the_record_as_it_was_read(stored, unknown):
    # RG-FMd-a: an entry without ``stored`` was hashed with "stored": false
    # added, so 93 of 200 ties kept another record than the reference and
    # STANDUP, and the two apps would write the list back and forth.
    for a, b in _tie_pairs(stored=stored, unknown=unknown):
        greater = max((a, b), key=_digest_as_read)
        for order in ((a, b), (b, a)):
            read = decode_items({"items": list(order)})
            merged = merge_items(SubmissionItems(), read)
            (kept,), = [encode_items(merged, NOW)["items"]]
            assert kept == greater                      # written back as read, nothing added
            assert ("stored" in kept) is stored
            assert decode_items({"items": [a]}).items[0] == entry_from_json(a)


def test_the_probes_tie_keeps_what_the_reference_keeps():
    # The gate's vector: the contract's reference (crosscheck.reference.mjs)
    # and STANDUP keep "Titel B1"; MyEditor kept "Titel A1".
    a, b = list(_tie_pairs(stored=False))[1]
    assert (a["title"], b["title"]) == ("Titel A1", "Titel B1")
    for order in ((a, b), (b, a)):
        kept, = encode_items(decode_items({"items": list(order)}), NOW)["items"]
        assert kept["title"] == "Titel B1" and "stored" not in kept


def test_an_entry_changed_here_is_hashed_and_written_as_this_app_writes_it():
    # Stored now, the record is no longer the one read: what is written
    # (and hashed) has every field.
    a, _b = next(_tie_pairs(stored=False))
    read = decode_items({"items": [a]}).items[0]
    assert record_json(read) == a
    stored = replace(read, stored=True)
    assert record_json(stored) == stored.to_json() and record_json(stored)["stored"] is True
    built = entry(1)
    assert built.read_as is None and record_json(built) == built.to_json()


def test_a_record_s_text_is_the_reference_s_for_what_canonical_json_cannot_hold():
    # The reference writes values as JSON.stringify does and sorts keys by
    # UTF-16 code unit (checked against node for 3,500 doubles).
    from nostr.standup.js_values import record_text
    assert record_text({"b": 1.0, "a": [1e21, 1e-7, 1.5, 2 ** 53 + 1, -0.0, 1e20]}) == \
        '{"a":[1e+21,1e-7,1.5,9007199254740992,0,100000000000000000000],"b":1}'
    assert record_text({"\U0001F600": 1, "\uffff": 2, "z": 3}) == \
        '{"z":3,"\U0001F600":1,"\uffff":2}'
    assert record_text(["\ud800", "x\udfff", "\u2028\u0007"]) == \
        '["\\ud800","x\\udfff","\u2028\\u0007"]'
    record = {"sid": "a" * 64, "updated_at": 5, "title": "Grüße", "n": [1, None, True]}
    assert record_text(record) == json.dumps(record, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False)


def test_records_the_other_apps_merge_are_merged_here_too():
    # RG-FMd-a probe: a negative updated_at is an integer (13), so its
    # record is merged by identity, not kept apart; a tombstone's key and
    # "deleted" are read as JavaScript reads them (the reference, STANDUP).
    a, b = next(_tie_pairs(stored=True))
    negative = dict(a, updated_at=-5)
    merged = decode_items({"items": [negative, b]})
    assert [item.to_json()["title"] for item in merged.items] == [b["title"]]
    sid = "c" * 64
    numbered = {"sid": sid, "deleted": True, "status_key_hash": 5, "updated_at": 1}
    assert record_key(item_from_json(numbered)) == "5"
    for deleted, removal in ((1, True), ([], True), ({}, True), ("x", True), (0, False),
                             ("", False), (None, False), (False, False)):
        item = item_from_json({"sid": sid, "deleted": deleted, "updated_at": 1, "x": 1})
        assert is_tombstone(item) is removal, deleted
    # A removal is never a stored entry, whatever else it says: the newer
    # record of its identity is kept.
    removed = {"sid": sid, "deleted": [], "stored": True, "updated_at": 1}
    newer = {"sid": sid, "updated_at": 5, "x": 1}
    for order in ((removed, newer), (newer, removed)):
        assert encode_items(decode_items({"items": list(order)}), NOW)["items"] == [newer]


def test_a_sid_with_two_stored_entries_shows_the_smallest_key_in_every_order():
    a, b = replace(entry(1), stored=True), replace(entry(1, updated_at=NOW + 5), stored=True)
    smallest = min((a, b), key=record_key)
    for merged in _every_order(a, b):
        assert shown_for(merged, a.sid) == smallest


def test_two_unstored_entries_of_one_time_show_the_smallest_key_in_every_order():
    a, b = entry(1), entry(1)
    smallest = min((a, b), key=record_key)
    for merged in _every_order(a, b):
        assert shown_for(merged, a.sid) == smallest


def test_a_read_with_nothing_left_behind_writes_nothing():
    # Only unstored records of a stored sid get tombstones, never the
    # stored one itself (13 at 0456312, Removing).
    theirs = replace(entry(1), stored=True)
    relay = Relay()
    relay.put(formats.SUBMISSIONS_LIST_D, {"v": 1, "page": 0, "pages": 1,
                                           "items": [theirs.to_json()]})
    store, _relay, _clock = make(relay=relay)
    assert load(store).value.entries == (theirs,)
    settle(60)
    assert relay.published == [] and store.left_behind() == []


def test_a_mark_stored_marks_the_later_revision_held_with_the_same_keys():
    # The mark is the status key's: a later revision held keeps its place.
    store, _relay, _clock = make()
    load(store)
    first = entry(1)
    assert record(store, first).stored
    later = replace(first, revision=2, revision_hash=formats.sha256_hex("rev-1-2"))
    assert record(store, later).stored
    results = _done(lambda answer: store.mark_stored([first], answer))
    assert [r.stored for r in results] == [True]
    held = store.entry(first.sid)
    assert held.stored and held.revision == 2


def test_every_list_sentence_is_one_sentence_with_its_cause_and_what_to_do():
    # RG-M2r3 7: said in a failure's place, the words keep their remedy.
    # What was read in an older encryption is only left as it is.
    import re
    import i18n
    from nostr.standup import lists as lists_module
    for words in (lists_module._submissions_words(), lists_module._read_state_words()):
        for text in (words.unreadable, words.signer_silent, words.old_encryption,
                     words.missing_page, words.too_long_for_connection):
            if not text:
                continue
            assert text.endswith(".") and not re.search(r"[.!?] [A-Z]", text), text
            assert ": " in text or text == i18n._(lists_module.READ_STATE_OLD_ENCRYPTION), text
