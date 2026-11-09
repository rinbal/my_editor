# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The encrypted kind 30078 list (nostr/encrypted_lists.py) on its own:
paging, merging, and the rules every such list keeps (never write over a
list that could not be read, or one nobody answered for).

The feed-sources list runs on the same code; its own tests
(test_imports_subscriptions.py, test_imports_list_safety.py) pin that its
format and behaviour stayed as they were. Here the lists are the ones the
paging is for: a read state (a map, merged by the highest number) and a
submissions list (a list, merged by id, the newest ``updated_at`` wins),
shaped as STANDUP's editorial contract describes them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nostr import crypto, events
from nostr.bunker import SIGNER_NIP44_MAX_PLAINTEXT
from nostr.encrypted_lists import (
    APP_DATA_KIND,
    FAILED,
    NO_ANSWER,
    NOT_ACCEPTED,
    PAGE_PLAINTEXT_MAX,
    SENT,
    UNREADABLE,
    UP_TO_DATE,
    EncryptedList,
    ListWords,
    Paging,
    page_d_tag,
    split_pages,
)
from tests.outbox_fakes import OTHER_SK, PK, SK, FakeRelayDirectory, settle
from tests.test_imports_subscriptions import FakeSessionPool, FakeSigner

PROFILE = SimpleNamespace(user_pubkey=PK, bunker_relays=[])
NOW = 1_800_000_000
D = "test:read-state"
WORDS = ListWords(unreadable="unreadable words", signer_silent="silent words",
                  old_encryption="old words")


class Relay:
    """One relay holding the newest event per ``d`` tag, answering a filter
    for several ``d`` tags at once, as relays do."""

    def __init__(self):
        self.stored = {}
        self.published = []
        self.queries = []
        self.answers = True
        self.refuse = set()          # d tags it will not take

    def events(self, relays, filters, on_done):
        self.queries.append(filters)
        if not self.answers:
            on_done([], set())
            return
        found = [self.stored[d] for d in filters[0]["#d"] if d in self.stored]
        on_done(found, set(relays))

    def __call__(self, relays, signed, *, on_done):
        d_tag = next(t[1] for t in signed["tags"] if t[0] == "d")
        self.published.append(d_tag)
        if d_tag in self.refuse:
            on_done(0, 1)
            return
        self.stored[d_tag] = signed
        on_done(1, 1)

    def put(self, d_tag, payload, *, created_at=NOW - 100, sk=SK, content=None):
        text = content if content is not None else "ENC[" + json.dumps(payload) + "]"
        self.stored[d_tag] = events.sign_event(
            {"kind": APP_DATA_KIND, "content": text, "created_at": created_at,
             "tags": [["d", d_tag], ["encrypted", "nip44"]]}, sk)

    def plaintext(self, d_tag) -> str:
        return self.stored[d_tag]["content"][4:-1]

    def page(self, d_tag) -> dict:
        return json.loads(self.plaintext(d_tag))


# -- the two lists of the contract ---------------------------------------------

def max_merge(base, local, remote):
    """Read state: the highest number per submission, from either side."""
    merged = dict(remote or {})
    for sid, seq in local.items():
        merged[sid] = max(seq, merged.get(sid, seq))
    return merged


def read_state_list(relay, *, signer=None, clock=lambda: NOW, session_pool=None):
    return EncryptedList(
        d_tag=D, decode=lambda payload: dict(payload["seen"]),
        encode=lambda value, now: {"seen": dict(value)}, merge=max_merge, words=WORDS,
        session_pool=session_pool or FakeSessionPool(signer or FakeSigner()),
        relay_directory=FakeRelayDirectory(), query=relay, publisher=relay, clock=clock,
        paging=Paging(field="seen"), name="read state")


def by_sid(base, local, remote):
    """Submissions: by id, the newest ``updated_at`` wins."""
    merged = {item["sid"]: item for item in (remote or [])}
    for item in local:
        if item["sid"] not in merged or item["updated_at"] > merged[item["sid"]]["updated_at"]:
            merged[item["sid"]] = item
    return list(merged.values())


def submission(n: int, updated_at: int = 1) -> dict:
    # The fields of the contract's submissions list, at realistic sizes.
    return {"sid": f"sub_{n:026d}", "publication": "ab" * 32, "server": "https://standup.example",
            "role": "writer", "s_sub": "cd" * 32, "status_key": "ef" * 16,
            "title": f"Ein Titel mit Umlauten, äöü, Nummer {n}", "kind": "article",
            "d": f"artikel-{n}", "round": 1, "revision": 2, "revision_hash": "12" * 32,
            "updated_at": updated_at}


def submissions_list(relay, **kw):
    return EncryptedList(
        d_tag="einundzwanzig:submissions", decode=lambda payload: list(payload["items"]),
        encode=lambda value, now: {"items": list(value)}, merge=by_sid, words=WORDS,
        session_pool=FakeSessionPool(FakeSigner()), relay_directory=FakeRelayDirectory(),
        query=relay, publisher=relay, clock=lambda: NOW, paging=Paging(field="items"), **kw)


def publish(store, local, *, base=None, mark=None):
    results, merges = [], []
    store.publish(PROFILE, state=lambda: (base, local, mark), on_done=results.append,
                  on_merged=lambda merged, remote, event: merges.append(merged))
    settle(40)
    assert results, "the publish never finished"
    return results[-1], merges


def read(store):
    answers = []
    store.relays(PROFILE, lambda relays: store.read(PROFILE, relays,
                                                    lambda *answer: answers.append(answer)),
                 reading=True)
    settle(40)
    assert answers, "the read never finished"
    return answers[-1]


def big_read_state(count: int) -> dict:
    return {f"sub_{n:026d}": n for n in range(count)}


# -- the page size --------------------------------------------------------------

def test_a_page_fits_one_signer_call_even_when_every_byte_is_escaped():
    # A page fits a signer's nip44_encrypt and nip44_decrypt, and twice a
    # page (every quote escaped in the answer) plus the answer's frame
    # still fits one NIP-44 message.
    assert PAGE_PLAINTEXT_MAX <= SIGNER_NIP44_MAX_PLAINTEXT
    frame = len(json.dumps({"id": "0" * 16, "result": ""}, separators=(",", ":")))
    assert 2 * PAGE_PLAINTEXT_MAX + frame <= crypto.NIP44_MAX_PLAINTEXT_LEN


# -- splitting -----------------------------------------------------------------

def test_a_short_list_is_one_page_in_the_contracts_shape():
    assert split_pages({"seen": {"a": 1}}, Paging("seen")) == [
        '{"v":1,"page":0,"pages":1,"seen":{"a":1}}']
    assert split_pages({"items": []}, Paging("items")) == ['{"v":1,"page":0,"pages":1,"items":[]}']


def test_a_long_map_is_split_into_full_pages_that_join_back():
    seen = big_read_state(3_000)
    pages = [json.loads(text) for text in split_pages({"seen": seen}, Paging("seen"))]
    assert len(pages) > 2
    assert [p["page"] for p in pages] == list(range(len(pages)))
    assert {p["pages"] for p in pages} == {len(pages)} and {p["v"] for p in pages} == {1}
    joined = {}
    for p in pages:
        joined.update(p["seen"])
    assert joined == seen and list(joined) == list(seen)
    texts = split_pages({"seen": seen}, Paging("seen"))
    assert all(len(t.encode("utf-8")) <= PAGE_PLAINTEXT_MAX for t in texts)
    # Full, not wasteful: each page but the last has no room for one more.
    entry = len('"sub_00000000000000000000002999":2999,')
    assert all(len(t) > PAGE_PLAINTEXT_MAX - entry for t in texts[:-1])


def test_five_hundred_submissions_fit_a_few_pages():
    # Criterion 22: a submissions list with 500 entries.
    items = [submission(n) for n in range(500)]
    texts = split_pages({"items": items}, Paging("items"))
    assert 4 <= len(texts) <= 12
    assert all(len(t.encode("utf-8")) <= PAGE_PLAINTEXT_MAX for t in texts)
    assert [i for t in texts for i in json.loads(t)["items"]] == items


def test_other_keys_go_on_the_first_page_only():
    texts = split_pages({"seen": big_read_state(3_000), "note": "x"}, Paging("seen"))
    assert json.loads(texts[0])["note"] == "x"
    assert all("note" not in json.loads(t) for t in texts[1:])


def test_an_entry_larger_than_a_page_cannot_be_written():
    with pytest.raises(ValueError, match="does not fit"):
        split_pages({"items": ["x" * PAGE_PLAINTEXT_MAX]}, Paging("items"))


def test_page_d_tags():
    assert page_d_tag(D, 0) == D
    assert page_d_tag(D, 3) == f"{D}:3"


# -- writing and reading pages ---------------------------------------------------

def test_a_long_list_is_written_in_pages_and_read_back_whole():
    relay = Relay()
    store = read_state_list(relay)
    seen = big_read_state(3_000)
    result, _ = publish(store, seen)
    assert result.status == SENT
    count = relay.page(D)["pages"]
    assert count > 2
    assert sorted(relay.stored) == sorted(page_d_tag(D, n) for n in range(count))
    for n in range(count):
        event = relay.stored[page_d_tag(D, n)]
        assert event["kind"] == APP_DATA_KIND and events.verify_event(event)
        assert event["tags"] == [["d", page_d_tag(D, n)], ["client", "MyEditor"],
                                 ["encrypted", "nip44"]]
        assert len(relay.plaintext(page_d_tag(D, n)).encode("utf-8")) <= PAGE_PLAINTEXT_MAX
    value, event = read(store)
    assert value == seen
    assert event["id"] == relay.stored[D]["id"]
    # The pages after the first are asked for in one query.
    assert relay.queries[-1][0]["#d"] == [page_d_tag(D, n) for n in range(1, count)]


def test_page_0_is_written_last():
    relay = Relay()
    publish(read_state_list(relay), big_read_state(3_000))
    count = relay.page(D)["pages"]
    assert relay.published == [page_d_tag(D, n) for n in range(count - 1, -1, -1)]


def test_a_list_of_items_round_trips():
    relay = Relay()
    store = submissions_list(relay)
    items = [submission(n) for n in range(500)]
    assert publish(store, items)[0].status == SENT
    assert read(store)[0] == items


def test_merging_keeps_the_highest_number_from_either_side():
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"a": 5, "b": 1}})
    result, merges = publish(read_state_list(relay), {"a": 3, "c": 2})
    assert result.status == SENT
    assert merges == [{"a": 5, "b": 1, "c": 2}]
    assert relay.page(D)["seen"] == {"a": 5, "b": 1, "c": 2}


def test_newest_updated_at_wins_for_submissions():
    relay = Relay()
    store = submissions_list(relay)
    publish(store, [submission(1, updated_at=5), submission(2, updated_at=5)])
    publish(store, [submission(1, updated_at=9), submission(2, updated_at=1)])
    items = {i["sid"]: i["updated_at"] for i in read(store)[0]}
    assert items == {submission(1)["sid"]: 9, submission(2)["sid"]: 5}


def test_nothing_is_written_when_the_relays_have_it_already():
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"a": 5}})
    result, _ = publish(read_state_list(relay), {"a": 2})
    assert result.status == UP_TO_DATE and result.remote == {"a": 5}
    assert relay.published == []


def test_only_changed_pages_are_signed_again_and_page_0_always():
    relay = Relay()
    signer = FakeSigner()
    store = read_state_list(relay, signer=signer)
    seen = big_read_state(3_000)
    publish(store, seen)
    count = relay.page(D)["pages"]
    last_key = list(json.loads(relay.plaintext(page_d_tag(D, count - 1)))["seen"])[-1]
    signer.signed.clear()
    relay.published.clear()
    changed = dict(seen)
    changed[last_key] += 1
    result, _ = publish(store, changed)
    assert result.status == SENT
    assert relay.published == [page_d_tag(D, count - 1), D]
    assert len(signer.signed) == 2
    assert read(store)[0] == changed


def test_every_page_is_dated_after_the_one_it_replaces_even_with_a_slow_clock():
    relay = Relay()
    publish(read_state_list(relay), big_read_state(3_000))
    newest = max(e["created_at"] for e in relay.stored.values())
    relay.published.clear()
    behind = read_state_list(relay, clock=lambda: NOW - 10_000)
    assert publish(behind, {**big_read_state(3_000), "new": 1})[0].status == SENT
    assert relay.published and D in relay.published
    assert all(relay.stored[d]["created_at"] > newest for d in relay.published)


def test_a_shorter_list_leaves_old_pages_unread():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    assert relay.page(D)["pages"] > 1
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"only": 1}}, created_at=NOW + 50)
    assert read(store)[0] == {"only": 1}


# -- what is never written over ------------------------------------------------

def test_a_missing_page_waits_instead_of_losing_what_is_on_it():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    del relay.stored[page_d_tag(D, 1)]
    relay.published.clear()
    assert read(store)[0] == NO_ANSWER
    assert publish(store, {"x": 1})[0].status == NO_ANSWER
    assert relay.published == []


def test_a_page_that_cannot_be_decrypted_is_never_written_over():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    relay.put(page_d_tag(D, 1), None, created_at=NOW + 5, content="not ours to read")
    relay.published.clear()
    statuses = []
    store._on_status = statuses.append
    assert read(store)[0] == UNREADABLE
    assert statuses == ["unreadable words"]
    assert publish(store, {"x": 1})[0].status == UNREADABLE
    assert relay.published == []


def test_a_newer_apps_format_is_never_written_over():
    relay = Relay()
    relay.put(D, {"v": 2, "page": 0, "pages": 1, "seen": {"a": 1}})
    store = read_state_list(relay)
    assert read(store)[0] == UNREADABLE
    assert publish(store, {"b": 1})[0].status == UNREADABLE
    assert relay.published == []


@pytest.mark.parametrize("page", [
    {"v": 1, "page": 0, "pages": 0, "seen": {}},
    {"v": 1, "page": 0, "pages": "2", "seen": {}},
    {"v": 1, "page": 0, "pages": 101, "seen": {}},
    {"v": 1, "page": 0, "pages": 1, "seen": ["not", "a", "map"]},
    {"v": 1, "page": 0, "pages": 1},
    ["not an object"],
])
def test_a_broken_first_page_is_unreadable(page):
    relay = Relay()
    relay.put(D, page)
    assert read(read_state_list(relay))[0] == UNREADABLE


def test_no_answer_from_the_accounts_relays_is_not_an_empty_list():
    relay = Relay()
    relay.answers = False
    store = read_state_list(relay)
    assert read(store)[0] == NO_ANSWER
    assert publish(store, {"a": 1})[0].status == NO_ANSWER
    assert relay.published == []


def test_no_list_yet_is_written_fresh():
    relay = Relay()
    store = read_state_list(relay)
    assert read(store) == (None, None)
    assert publish(store, {"a": 1})[0].status == SENT
    assert read(store)[0] == {"a": 1}


def test_a_page_signed_by_someone_else_does_not_count():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, {"a": 1})
    real = relay.stored[D]
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"planted": 9}},
              created_at=NOW + 100, sk=OTHER_SK)
    forged = relay.stored[D]
    # The relay answers both; only the account's own counts.
    relay.events = lambda relays, filters, on_done: on_done([forged, real], set(relays))
    assert read(store)[0] == {"a": 1}


def test_a_stale_copy_changes_nothing():
    relay = Relay()
    store = read_state_list(relay)
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"old": 1}}, created_at=NOW - 500)
    # The last list seen was newer than what this relay kept.
    result, merges = publish(store, {"here": 1}, base={"newer": 2}, mark=(NOW - 100, "f" * 64))
    assert result.status == SENT
    assert merges == [{"newer": 2, "here": 1}]
    assert relay.page(D)["seen"] == {"newer": 2, "here": 1}
    assert relay.stored[D]["created_at"] >= NOW - 99


# -- failures ------------------------------------------------------------------

def test_a_signer_that_cannot_be_reached_fails_the_publish():
    relay = Relay()
    store = read_state_list(relay, session_pool=FakeSessionPool(error="signer offline"))
    result, _ = publish(store, {"a": 1})
    assert result.status == FAILED and result.reason == "signer offline"
    assert relay.published == []


def test_a_page_no_relay_takes_stops_before_page_0():
    relay = Relay()
    store = read_state_list(relay)
    relay.refuse.add(page_d_tag(D, 1))
    result, _ = publish(store, big_read_state(3_000))
    assert result.status == NOT_ACCEPTED
    assert D not in relay.stored          # readers still see the old list (none)


def test_an_entry_too_large_for_a_page_fails_cleanly():
    relay = Relay()
    store = submissions_list(relay)
    result, _ = publish(store, [{"sid": "x", "updated_at": 1, "title": "x" * 40_000}])
    assert result.status == FAILED and "does not fit" in result.reason
    assert relay.published == []


# -- a list that is not paged -------------------------------------------------

def test_a_list_without_paging_is_one_event_exactly_as_encoded():
    relay = Relay()
    store = EncryptedList(
        d_tag="test:flat", decode=lambda payload: payload, encode=lambda value, now: {
            "rows": value, "updatedAt": now},
        merge=lambda base, local, remote: local, words=WORDS,
        session_pool=FakeSessionPool(FakeSigner()), relay_directory=FakeRelayDirectory(),
        query=relay, publisher=relay, clock=lambda: NOW)
    assert publish(store, ["ä"])[0].status == SENT
    assert list(relay.stored) == ["test:flat"]
    assert relay.plaintext("test:flat") == '{"rows":["\\u00e4"],"updatedAt":%d}' % NOW
