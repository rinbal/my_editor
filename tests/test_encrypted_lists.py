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

import hashlib
import json
from types import SimpleNamespace

import pytest

from nostr import crypto, events
from nostr.answer_size import SIGNER_ANSWER_MAX, decrypt_answer_size
from nostr.signer_limits import SIGNER_NIP44_MAX_PLAINTEXT
from nostr.encrypted_lists import EncryptedList
from nostr.list_relays import LIST_KIND
from nostr.list_results import ListWords, PublishStatus, ReadStatus
from nostr.paged_list import (
    MAX_PAGES, PAGE_PAYLOAD_MAX, PAGE_PLAINTEXT_MAX, Paging, page_d_tag, split,
)
from tests.outbox_fakes import OTHER_SK, PK, SK, FakeRelayDirectory, settle
from tests.test_imports_subscriptions import FakeSessionPool, FakeSigner

PROFILE = SimpleNamespace(user_pubkey=PK, bunker_relays=[])
NOW = 1_800_000_000
D = "test:read-state"
WORDS = ListWords(unreadable="unreadable words", signer_silent="silent words",
                  old_encryption="old words")


class Relay:
    """The account's relays, holding the newest event per ``d`` tag and
    answering a filter for several ``d`` tags at once, as relays do.
    ``refuse`` names the d tags no relay takes; ``refuse_at`` a relay
    that does not take a d tag (the others do)."""

    def __init__(self):
        self.stored = {}
        self.published = []
        self.sent_to = {}
        self.queries = []
        self.answers = True
        self.refuse = set()
        self.refuse_at = {}

    def events(self, relays, filters, on_done):
        self.queries.append(filters)
        if not self.answers:
            on_done([], set())
            return
        found = [self.stored[d] for d in filters[0]["#d"] if d in self.stored]
        on_done(found, set(relays))

    def __call__(self, relays, signed, *, on_done, every_relay=False):
        d_tag = next(t[1] for t in signed["tags"] if t[0] == "d")
        self.published.append(d_tag)
        self.sent_to[d_tag] = list(relays)
        took = frozenset(url for url in relays if d_tag not in self.refuse_at.get(url, ()))
        if d_tag in self.refuse or not took:
            on_done(frozenset())
            return
        self.stored[d_tag] = signed
        on_done(took)

    def put(self, d_tag, payload, *, created_at=NOW - 100, sk=SK, content=None):
        text = content if content is not None else "ENC[" + json.dumps(payload) + "]"
        self.stored[d_tag] = events.sign_event(
            {"kind": LIST_KIND, "content": text, "created_at": created_at,
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


def read_state_list(relay, *, signer=None, clock=lambda: NOW, session_pool=None, **kw):
    return EncryptedList(
        d_tag=D, decode=lambda payload: dict(payload["seen"]),
        encode=lambda value, now: {"seen": dict(value)}, merge=max_merge, words=WORDS,
        session_pool=session_pool or FakeSessionPool(signer or FakeSigner()),
        relay_directory=FakeRelayDirectory(), query=relay, publisher=relay, clock=clock,
        paging=Paging(field="seen", combine=max), name="read state", **kw)


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


SUBS = "einundzwanzig:submissions"


def submissions_list(relay, *, signer=None, **kw):
    # Read back through the union merge, as the real list is.
    return EncryptedList(
        d_tag=SUBS, decode=lambda payload: by_sid(None, payload["items"], []),
        encode=lambda value, now: {"items": list(value)}, merge=by_sid, words=WORDS,
        session_pool=FakeSessionPool(signer or FakeSigner()),
        relay_directory=FakeRelayDirectory(), query=relay, publisher=relay,
        clock=lambda: NOW, paging=Paging(field="items"), **kw)


def publish(store, local, *, base=None, mark=None):
    results, merges = [], []
    store.publish(PROFILE, state=lambda: (base, local, mark), on_done=results.append,
                  on_merged=lambda merged, remote, event: merges.append(merged))
    settle(40)
    assert results, "the publish never finished"
    return results[-1], merges


def read(store):
    answers = []
    store.relays(PROFILE, lambda relays: store.read(PROFILE, relays, answers.append),
                 reading=True)
    settle(40)
    assert answers, "the read never finished"
    return answers[-1]


def big_read_state(count: int) -> dict:
    return {f"sub_{n:026d}": n for n in range(count)}


def page_count(relay, d_tag=D) -> int:
    return relay.page(d_tag)["pages"]


# -- the page size --------------------------------------------------------------

def test_a_page_fits_one_signer_call_even_when_every_byte_is_escaped():
    # A page fits a signer's nip44_encrypt and nip44_decrypt, and a page
    # of quotes (each two bytes in the answer) plus the answer's frame
    # fits the answer bound of contract 12.5.
    assert PAGE_PLAINTEXT_MAX <= SIGNER_NIP44_MAX_PLAINTEXT
    assert decrypt_answer_size('"' * PAGE_PLAINTEXT_MAX) <= SIGNER_ANSWER_MAX


@pytest.mark.parametrize("ink", ["<", ">", "&"])
def test_pages_are_split_by_the_answer_bound_too(ink):
    # M1d, contract 12.5 and 13: a character one byte in the page and six
    # in the answer makes a 32,000-byte page far too large to be handed
    # back. (Control characters and U+2028 are escaped in the page
    # already, so the 32,000 bytes bound them.)
    payload = {"seen": {f"{ink * 40}{n:06d}": n for n in range(2_000)}}
    pages = split(payload, Paging(field="seen"))
    assert all(len(p.encode("utf-8")) <= PAGE_PLAINTEXT_MAX for p in pages)
    assert all(decrypt_answer_size(p) <= SIGNER_ANSWER_MAX for p in pages)
    by_plaintext = sum(len(p.encode("utf-8")) for p in pages) / PAGE_PLAINTEXT_MAX
    assert len(pages) > by_plaintext + 1           # the answer bound decided


# -- writing and reading pages ---------------------------------------------------

def test_a_long_list_is_written_in_pages_and_read_back_whole():
    relay = Relay()
    store = read_state_list(relay)
    seen = big_read_state(3_000)
    result, _ = publish(store, seen)
    assert result.status is PublishStatus.SENT
    count = page_count(relay)
    assert count > 2
    assert sorted(relay.stored) == sorted(page_d_tag(D, n) for n in range(count))
    for n in range(count):
        event = relay.stored[page_d_tag(D, n)]
        assert event["kind"] == LIST_KIND and events.verify_event(event)
        assert event["tags"] == [["d", page_d_tag(D, n)], ["client", "MyEditor"],
                                 ["encrypted", "nip44"]]
        assert len(relay.plaintext(page_d_tag(D, n)).encode("utf-8")) <= PAGE_PLAINTEXT_MAX
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE and answer.value == seen
    assert answer.event["id"] == relay.stored[D]["id"]
    # The pages after the first are asked for in one query.
    assert relay.queries[-1][0]["#d"] == [page_d_tag(D, n) for n in range(1, count)]


def test_page_0_is_written_last():
    relay = Relay()
    publish(read_state_list(relay), big_read_state(3_000))
    count = page_count(relay)
    assert relay.published == [page_d_tag(D, n) for n in range(count - 1, -1, -1)]


def test_a_list_of_items_round_trips():
    relay = Relay()
    store = submissions_list(relay)
    items = [submission(n) for n in range(500)]
    assert publish(store, items)[0].status is PublishStatus.SENT
    assert read(store).value == items


def test_merging_keeps_the_highest_number_from_either_side():
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"a": 5, "b": 1}})
    result, merges = publish(read_state_list(relay), {"a": 3, "c": 2})
    assert result.status is PublishStatus.SENT
    assert merges == [{"a": 5, "b": 1, "c": 2}]
    assert relay.page(D)["seen"] == {"a": 5, "b": 1, "c": 2}


def test_newest_updated_at_wins_for_submissions():
    relay = Relay()
    store = submissions_list(relay)
    publish(store, [submission(1, updated_at=5), submission(2, updated_at=5)])
    publish(store, [submission(1, updated_at=9), submission(2, updated_at=1)])
    items = {i["sid"]: i["updated_at"] for i in read(store).value}
    assert items == {submission(1)["sid"]: 9, submission(2)["sid"]: 5}


def test_nothing_is_written_when_the_relays_have_it_already():
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"a": 5}})
    result, _ = publish(read_state_list(relay), {"a": 2})
    assert result.status is PublishStatus.UP_TO_DATE and result.remote == {"a": 5}
    assert relay.published == []


def test_a_page_whose_plaintext_is_on_the_relays_is_not_signed_again():
    # Through a signer app each page signed is a request. A page that
    # reads as written already goes out again unsigned (page 0 must only
    # reach relays that hold every later page); page 0 is always signed.
    relay = Relay()
    signer = FakeSigner()
    store = read_state_list(relay, signer=signer)
    seen = big_read_state(3_000)
    publish(store, seen)
    count = page_count(relay)
    last_key = list(json.loads(relay.plaintext(page_d_tag(D, count - 1)))["seen"])[-1]
    before = {d: event["id"] for d, event in relay.stored.items()}
    signer.signed.clear()
    relay.published.clear()
    changed = dict(seen)
    changed[last_key] += 1
    result, _ = publish(store, changed)
    assert result.status is PublishStatus.SENT
    assert relay.published == [page_d_tag(D, n) for n in range(count - 1, -1, -1)]
    assert [e["tags"][0][1] for e in signer.signed] == [page_d_tag(D, count - 1), D]
    assert {d for d in before if relay.stored[d]["id"] != before[d]} == {
        D, page_d_tag(D, count - 1)}
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE and answer.value == changed


def test_every_new_page_is_dated_after_the_one_it_replaces_even_with_a_slow_clock():
    relay = Relay()
    publish(read_state_list(relay), big_read_state(3_000))
    newest = max(e["created_at"] for e in relay.stored.values())
    before = {d: event["id"] for d, event in relay.stored.items()}
    behind = read_state_list(relay, clock=lambda: NOW - 10_000)
    assert publish(behind, {**big_read_state(3_000), "new": 1})[0].status is PublishStatus.SENT
    signed = [d for d in relay.stored if relay.stored[d]["id"] != before.get(d)]
    assert D in signed
    assert all(relay.stored[d]["created_at"] > newest for d in signed)


@pytest.mark.parametrize("content", [
    "ENC[" + "x" * PAGE_PAYLOAD_MAX + "]",                                  # too long
    "ENC[" + json.dumps({"v": 1, "page": 1, "pages": 9, "seen": {}}) + "]",  # not this list's
    "",                                                                     # emptied
])
def test_a_write_is_dated_after_pages_that_do_not_count_too(content):
    # RG-M1 11: a relay keeps the newer of two, so a page dated before a
    # (future-dated) page that did not count would never replace it, and
    # every later read would stay incomplete.
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    relay.put(page_d_tag(D, 1), None, created_at=NOW + 500, content=content)
    before = {d: event["id"] for d, event in relay.stored.items()}
    assert publish(store, {**big_read_state(3_000), "new": 1})[0].status is PublishStatus.SENT
    signed = [d for d in relay.stored if relay.stored[d]["id"] != before.get(d)]
    assert page_d_tag(D, 1) in signed
    assert all(relay.stored[d]["created_at"] > NOW + 500 for d in signed)


def test_a_shorter_list_leaves_old_pages_unread():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    assert page_count(relay) > 1
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "parts": [], "seen": {"only": 1}},
              created_at=NOW + 50)
    assert read(store).value == {"only": 1}


# -- generations (contract 13; review RG-M0 findings 1 and 2) ---------------------

def test_page_0_binds_the_later_pages_by_their_hashes():
    relay = Relay()
    publish(read_state_list(relay), big_read_state(3_000))
    count = page_count(relay)
    parts = relay.page(D)["parts"]
    assert parts == [hashlib.sha256(relay.plaintext(page_d_tag(D, n)).encode()).hexdigest()
                     for n in range(1, count)]


def test_a_write_cut_off_before_page_0_mixes_nothing_and_loses_nothing():
    # RG-M0 finding 1: page 0 did not get through (a timeout, a declined
    # prompt, a relay refusal) over an existing list. The old page 0 and
    # the new later pages are of two generations: the read is incomplete,
    # keeps every entry, and holds none twice.
    relay = Relay()
    store = submissions_list(relay)
    items = [submission(k) for k in range(245)]
    publish(store, items)
    # A longer first entry moves an entry of every page onto the next.
    grown = [dict(items[0], title="Ein viel laengerer Titel " * 40, updated_at=2)] + items[1:]
    relay.refuse.add(SUBS)
    assert publish(store, grown, base=items)[0].status is PublishStatus.NOT_ACCEPTED
    relay.refuse.clear()
    answer = read(store)
    assert answer.status is ReadStatus.INCOMPLETE and answer.stale
    sids = [i["sid"] for i in answer.value]
    assert set(sids) == {i["sid"] for i in items} and len(sids) == len(set(sids))
    # The next write is a complete generation again, with the newer title.
    assert publish(store, grown)[0].status is PublishStatus.SENT
    healed = read(store)
    assert healed.status is ReadStatus.COMPLETE
    assert sorted(healed.value, key=lambda i: i["sid"]) == sorted(grown, key=lambda i: i["sid"])


def test_another_devices_rewrite_of_one_page_makes_the_read_incomplete_not_shorter():
    relay = Relay()
    store = read_state_list(relay)
    seen = big_read_state(3_000)
    publish(store, seen)
    page_1 = relay.page(page_d_tag(D, 1))
    relay.put(page_d_tag(D, 1), dict(page_1, seen={"elsewhere": 7}), created_at=NOW + 10)
    answer = read(store)
    assert answer.status is ReadStatus.INCOMPLETE and answer.stale == (1,)
    # Every page that is the account's own counts, the other generation's too.
    assert answer.value == {**{k: v for k, v in seen.items() if k not in page_1["seen"]},
                            "elsewhere": 7}


def test_a_missing_page_is_reported_and_the_rest_is_read():
    relay = Relay()
    store = read_state_list(relay)
    seen = big_read_state(3_000)
    publish(store, seen)
    lost = relay.page(page_d_tag(D, 1))["seen"]
    del relay.stored[page_d_tag(D, 1)]
    statuses = []
    store._words = ListWords("u", "s", "o", missing_page="a page is missing")
    store._on_status = statuses.append
    answer = read(store)
    assert answer.status is ReadStatus.INCOMPLETE and answer.stale == (1,)
    assert statuses == ["a page is missing"]
    assert answer.value == {k: v for k, v in seen.items() if k not in lost}
    assert answer.event["id"] == relay.stored[D]["id"]


def test_a_missing_page_never_wedges_the_list():
    # RG-M0 finding 2: the next write builds a complete generation.
    relay = Relay()
    store = read_state_list(relay)
    seen = big_read_state(3_000)
    publish(store, seen)
    lost = relay.stored.pop(page_d_tag(D, 1))
    lost_keys = json.loads(lost["content"][4:-1])["seen"]
    result, _ = publish(store, {"new": 1})
    assert result.status is PublishStatus.SENT
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE
    assert answer.value == {**{k: v for k, v in seen.items() if k not in lost_keys}, "new": 1}


def test_an_incomplete_list_is_written_even_when_nothing_changed_here():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    del relay.stored[page_d_tag(D, 1)]
    assert publish(store, {})[0].status is PublishStatus.SENT
    assert read(store).status is ReadStatus.COMPLETE


def test_page_0_goes_only_to_the_relays_that_took_every_later_page():
    relay = Relay()
    store = read_state_list(relay, entitled_relays=["wss://a.example", "wss://b.example"])
    relays = []
    store.relays(PROFILE, relays.append)
    settle(10)
    assert len(relays[0]) >= 2
    refusing = relays[0][0]
    relay.refuse_at[refusing] = {page_d_tag(D, 1)}
    assert publish(store, big_read_state(3_000))[0].status is PublishStatus.SENT
    assert relay.sent_to[page_d_tag(D, 1)] == relays[0]
    assert refusing not in relay.sent_to[D] and relay.sent_to[D]
    assert set(relay.sent_to[D]) == set(relays[0]) - {refusing}


def test_no_relay_that_took_every_later_page_means_no_page_0():
    relay = Relay()
    store = read_state_list(relay, entitled_relays=["wss://a.example", "wss://b.example"])
    relays = []
    store.relays(PROFILE, relays.append)
    settle(10)
    first, second = relays[0][0], relays[0][1]
    for url in relays[0]:
        relay.refuse_at[url] = {page_d_tag(D, 2)}
    relay.refuse_at[second] = {page_d_tag(D, 1)}
    relay.refuse_at[first] = {page_d_tag(D, 2)}
    assert publish(store, big_read_state(3_000))[0].status is PublishStatus.NOT_ACCEPTED
    assert D not in relay.published


def test_a_list_written_before_parts_is_read_and_upgraded_by_the_next_write():
    relay = Relay()
    store = read_state_list(relay)
    relay.put(D, {"v": 1, "page": 0, "pages": 2, "seen": {"a": 1}})
    relay.put(page_d_tag(D, 1), {"v": 1, "page": 1, "pages": 2, "seen": {"b": 2}})
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE and answer.value == {"a": 1, "b": 2}
    assert publish(store, {"c": 3})[0].status is PublishStatus.SENT
    assert "parts" in relay.page(D)
    assert read(store).value == {"a": 1, "b": 2, "c": 3}


def test_a_page_of_another_count_in_a_list_before_parts_is_of_another_generation():
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 2, "seen": {"a": 1}})
    relay.put(page_d_tag(D, 1), {"v": 1, "page": 1, "pages": 3, "seen": {"b": 2}})
    answer = read(read_state_list(relay))
    assert answer.status is ReadStatus.INCOMPLETE and answer.value == {"a": 1, "b": 2}


@pytest.mark.parametrize("page", [
    {"v": 2, "page": 1, "pages": 2, "seen": {"b": 2}},
    {"v": 1, "page": 2, "pages": 2, "seen": {"b": 2}},
    {"v": 1, "page": 1, "pages": 2, "seen": ["b"]},
    ["not an object"],
])
def test_a_later_page_that_is_not_this_lists_counts_as_missing(page):
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 2, "seen": {"a": 1}})
    relay.put(page_d_tag(D, 1), page)
    answer = read(read_state_list(relay))
    assert answer.status is ReadStatus.INCOMPLETE and answer.stale == (1,)
    assert answer.value == {"a": 1}


def test_a_page_that_turns_up_again_with_older_values_keeps_the_newer():
    relay = Relay()
    relay.put(D, {"v": 1, "page": 0, "pages": 2, "seen": {"a": 9}})
    relay.put(page_d_tag(D, 1), {"v": 1, "page": 1, "pages": 2, "seen": {"a": 3, "b": 1}})
    assert read(read_state_list(relay)).value == {"a": 9, "b": 1}


# -- at most 64 pages (RG-M0 finding 3) --------------------------------------------

def test_a_list_too_long_for_64_pages_is_refused_and_the_old_one_stays_readable():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, {"a": 1})
    result, _ = publish(store, big_read_state(90_000))
    assert result.status is PublishStatus.FAILED and str(MAX_PAGES) in result.reason
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE and answer.value == {"a": 1}


def test_the_longest_list_that_fits_is_written_and_read():
    relay = Relay()
    store = read_state_list(relay)
    seen = big_read_state(50_000)
    assert publish(store, seen)[0].status is PublishStatus.SENT
    assert 40 < page_count(relay) <= MAX_PAGES
    assert read(store).value == seen


# -- what is never written over ------------------------------------------------

def test_a_page_that_cannot_be_decrypted_is_never_written_over():
    relay = Relay()
    store = read_state_list(relay)
    publish(store, big_read_state(3_000))
    relay.put(page_d_tag(D, 1), None, created_at=NOW + 5, content="not ours to read")
    relay.published.clear()
    statuses = []
    store._on_status = statuses.append
    answer = read(store)
    assert answer.status is ReadStatus.BLOCKED and answer.words == "unreadable words"
    assert statuses == ["unreadable words"]
    result = publish(store, {"x": 1})[0]
    assert result.status is PublishStatus.BLOCKED and result.reason == "unreadable words"
    assert relay.published == []


class DecliningSigner(FakeSigner):
    """Declines to decrypt the ciphertexts in ``decline`` (a prompt the
    person said no to, or one that timed out with ``reason``)."""

    def __init__(self, reason="user declined"):
        super().__init__()
        self.decline = set()
        self.reason = reason

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **kw):
        if ciphertext in self.decline:
            on_failure(self.reason)
            return
        super().nip44_decrypt_self(ciphertext, on_success, on_failure, **kw)


@pytest.mark.parametrize("reason,words", [
    ("user declined", "unreadable words"),
    ("timed out waiting for signer", "silent words"),
])
def test_a_page_the_signer_declines_blocks_the_list_until_it_opens(reason, words):
    # Contract 13 (d5cc9a7): a page the relays return but that cannot be
    # opened now is not missing. Taking it for missing, the next write
    # would drop the entries it still holds.
    relay, signer = Relay(), DecliningSigner(reason)
    store = read_state_list(relay, signer=signer)
    seen = big_read_state(3_000)
    publish(store, seen)
    signer.decline.add(relay.stored[page_d_tag(D, 1)]["content"])
    relay.published.clear()
    answer = read(store)
    assert answer.status is ReadStatus.BLOCKED and not answer.readable
    assert answer.words == words and answer.stale == ()
    assert publish(store, {"new": 1})[0].status is PublishStatus.BLOCKED
    assert relay.published == []
    # Asked again, the signer opens it: the list is whole, nothing lost.
    signer.decline.clear()
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE and answer.value == seen
    result, _ = publish(store, {"new": 1})
    assert result.status is PublishStatus.SENT
    assert read(store).value == dict(seen, new=1)


def test_a_first_page_the_signer_declines_blocks_the_list():
    relay, signer = Relay(), DecliningSigner()
    store = read_state_list(relay, signer=signer)
    publish(store, {"a": 1})
    signer.decline.add(relay.stored[D]["content"])
    assert read(store).status is ReadStatus.BLOCKED
    assert publish(store, {"b": 1})[0].status is PublishStatus.BLOCKED


def test_a_newer_apps_format_is_never_written_over():
    relay = Relay()
    relay.put(D, {"v": 2, "page": 0, "pages": 1, "seen": {"a": 1}})
    store = read_state_list(relay)
    assert read(store).status is ReadStatus.UNREADABLE
    assert publish(store, {"b": 1})[0].status is PublishStatus.UNREADABLE
    assert relay.published == []


@pytest.mark.parametrize("page", [
    {"v": 1, "page": 0, "pages": 0, "seen": {}},
    {"v": 1, "page": 0, "pages": "2", "seen": {}},
    {"v": 1, "page": 0, "pages": MAX_PAGES + 1, "seen": {}},
    {"v": True, "page": 0, "pages": 1, "seen": {}},
    {"v": 1, "page": 0, "pages": 1, "seen": ["not", "a", "map"]},
    {"v": 1, "page": 0, "pages": 1},
    {"v": 1, "page": 0, "pages": 2, "parts": [], "seen": {}},
    {"v": 1, "page": 0, "pages": 2, "parts": ["AB" * 32], "seen": {}},
    ["not an object"],
])
def test_a_broken_first_page_is_unreadable(page):
    relay = Relay()
    relay.put(D, page)
    assert read(read_state_list(relay)).status is ReadStatus.UNREADABLE


def test_no_answer_from_the_accounts_relays_is_not_an_empty_list():
    relay = Relay()
    relay.answers = False
    store = read_state_list(relay)
    assert read(store).status is ReadStatus.NO_ANSWER
    assert publish(store, {"a": 1})[0].status is PublishStatus.NO_ANSWER
    assert relay.published == []


def test_no_list_yet_is_written_fresh():
    relay = Relay()
    store = read_state_list(relay)
    answer = read(store)
    assert answer.status is ReadStatus.MISSING and answer.value is None
    assert publish(store, {"a": 1})[0].status is PublishStatus.SENT
    assert read(store).value == {"a": 1}


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
    assert read(store).value == {"a": 1}


def test_a_stale_copy_changes_nothing():
    relay = Relay()
    store = read_state_list(relay)
    relay.put(D, {"v": 1, "page": 0, "pages": 1, "seen": {"old": 1}}, created_at=NOW - 500)
    # The last list seen was newer than what this relay kept.
    result, merges = publish(store, {"here": 1}, base={"newer": 2}, mark=(NOW - 100, "f" * 64))
    assert result.status is PublishStatus.SENT
    assert merges == [{"newer": 2, "here": 1}]
    assert relay.page(D)["seen"] == {"newer": 2, "here": 1}
    assert relay.stored[D]["created_at"] >= NOW - 99


# -- failures ------------------------------------------------------------------

def test_a_signer_that_cannot_be_reached_fails_the_publish():
    relay = Relay()
    store = read_state_list(relay, session_pool=FakeSessionPool(error="signer offline"))
    result, _ = publish(store, {"a": 1})
    assert result.status is PublishStatus.FAILED and result.reason == "signer offline"
    assert relay.published == []


def test_a_page_no_relay_takes_stops_before_page_0():
    relay = Relay()
    store = read_state_list(relay)
    relay.refuse.add(page_d_tag(D, 1))
    result, _ = publish(store, big_read_state(3_000))
    assert result.status is PublishStatus.NOT_ACCEPTED
    assert D not in relay.stored          # readers still see the old list (none)


def test_an_entry_too_large_for_a_page_fails_cleanly():
    relay = Relay()
    store = submissions_list(relay)
    result, _ = publish(store, [{"sid": "x", "updated_at": 1, "title": "x" * 40_000}])
    assert result.status is PublishStatus.FAILED and "does not fit" in result.reason
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
    assert publish(store, ["ä"])[0].status is PublishStatus.SENT
    assert list(relay.stored) == ["test:flat"]
    assert relay.plaintext("test:flat") == '{"rows":["\\u00e4"],"updatedAt":%d}' % NOW
    answer = read(store)
    assert answer.status is ReadStatus.COMPLETE and answer.value["rows"] == ["ä"]


def test_writing_over_the_list_last_seen_signs_only_the_pages_that_change():
    # The relays hold exactly the list this device last saw; one entry is
    # added at the end. Only the last page and page 0 are signed again.
    relay = Relay()
    signer = FakeSigner()
    store = submissions_list(relay, signer=signer)
    items = [submission(n) for n in range(300)]
    first, _ = publish(store, items)
    assert first.status is PublishStatus.SENT and first.signed
    pages = page_count(relay, SUBS)
    assert pages >= 3
    mark = (first.signed["created_at"], first.signed["id"])
    signer.signed.clear()
    second, _ = publish(store, items + [submission(300)], base=first.value, mark=mark)
    assert second.status is PublishStatus.SENT
    assert [e["tags"][0][1] for e in signer.signed] == [f"{SUBS}:{pages - 1}", SUBS]


# -- one write at a time (RG-M0 finding 4) ------------------------------------------

def test_one_write_runs_at_a_time_and_what_comes_meanwhile_goes_next_as_one():
    # Two writes at once dated their pages alike, and the NIP-01 tie
    # break (the lower id) mixed their pages.
    relay = Relay()
    signer = FakeSigner(hold=True)
    store = read_state_list(relay, signer=signer)
    local = {"a": 1}
    answers = {"first": [], "second": [], "third": []}

    def ask(name):
        store.publish(PROFILE, state=lambda: (None, dict(local), None),
                      on_done=answers[name].append)

    ask("first")
    settle(20)
    assert signer.waiting and store.is_writing(PROFILE)
    local["b"] = 2
    ask("second")
    local["c"] = 3
    ask("third")
    settle(20)
    reads = len(relay.queries)
    signer.release()
    settle(20)
    assert [len(a) for a in answers.values()] == [1, 0, 0]
    assert len(relay.queries) == reads + 1          # the next write, as one
    signer.release()
    settle(20)
    assert [len(a) for a in answers.values()] == [1, 1, 1]
    assert answers["second"][0] is answers["third"][0]
    assert answers["third"][0].status is PublishStatus.SENT
    assert relay.published.count(D) == 2
    assert relay.page(D)["seen"] == {"a": 1, "b": 2, "c": 3}
    assert not store.busy and not store.is_writing(PROFILE)


def test_a_write_asked_for_by_a_listener_goes_next_with_the_waiting_one():
    relay = Relay()
    signer = FakeSigner(hold=True)
    store = read_state_list(relay, signer=signer)
    answers = []

    def again(result):
        answers.append(("first", result.status))
        store.publish(PROFILE, state=lambda: (None, {"c": 3}, None),
                      on_done=lambda r: answers.append(("asked", r.status)))

    store.publish(PROFILE, state=lambda: (None, {"a": 1}, None), on_done=again)
    settle(20)
    store.publish(PROFILE, state=lambda: (None, {"b": 2}, None),
                  on_done=lambda r: answers.append(("waiting", r.status)))
    for _ in range(3):
        signer.release()
        settle(20)
    assert answers == [("first", PublishStatus.SENT), ("waiting", PublishStatus.SENT),
                       ("asked", PublishStatus.SENT)]
    assert relay.published.count(D) == 2 and relay.page(D)["seen"] == {"a": 1, "c": 3}


def test_another_accounts_write_does_not_wait():
    relay = Relay()
    signer = FakeSigner(hold=True)
    store = read_state_list(relay, signer=signer)
    store.publish(PROFILE, state=lambda: (None, {"a": 1}, None), on_done=print)
    settle(20)
    reads = len(relay.queries)
    other = SimpleNamespace(user_pubkey="ee" * 32, bunker_relays=[])
    store.publish(other, state=lambda: (None, {"b": 1}, None), on_done=print)
    settle(20)
    assert len(relay.queries) == reads + 1 and store.is_writing(other)


# -- what a signer app can hand back (RG-M0 R7) -----------------------------------

class CountingSigner(FakeSigner):
    def __init__(self):
        super().__init__()
        self.encrypted = []
        self.decrypted = []

    def nip44_encrypt_self(self, plaintext, on_success, on_failure, **kw):
        self.encrypted.append(plaintext)
        super().nip44_encrypt_self(plaintext, on_success, on_failure, **kw)

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **kw):
        self.decrypted.append(ciphertext)
        super().nip44_decrypt_self(ciphertext, on_success, on_failure, **kw)


def _flat_list(relay, signer):
    return EncryptedList(
        d_tag="test:flat", decode=lambda payload: payload["rows"],
        encode=lambda value, now: {"rows": value},
        merge=lambda base, local, remote: local, words=WORDS,
        session_pool=FakeSessionPool(signer), relay_directory=FakeRelayDirectory(),
        query=relay, publisher=relay, clock=lambda: NOW)


def test_a_list_the_signer_app_could_not_hand_back_is_never_written():
    # 40,0xx bytes encrypt, but the answer to nip44_decrypt would carry
    # them as 80,0xx: written, the list would be unreadable for good.
    relay, signer = Relay(), CountingSigner()
    rows = ['"' * 20_000]
    result, _ = publish(_flat_list(relay, signer), rows)
    assert result.status is PublishStatus.FAILED and "read back" in result.reason
    assert signer.encrypted == [] and relay.published == []


def test_the_same_list_with_the_key_here_is_written():
    relay, signer = Relay(), CountingSigner()
    store = _flat_list(relay, signer)
    results = []
    local = SimpleNamespace(user_pubkey=PK, bunker_relays=[], signer="local")
    store.publish(local, state=lambda: (None, ['"' * 20_000], None), on_done=results.append,
                  on_merged=lambda *_: None)
    settle(40)
    assert results[-1].status is PublishStatus.SENT and len(signer.encrypted) == 1


def test_a_first_page_longer_than_a_page_is_unreadable_without_asking_the_signer():
    relay, signer = Relay(), CountingSigner()
    relay.put(D, None, content="ENC[" + "x" * PAGE_PAYLOAD_MAX + "]")
    store = read_state_list(relay, signer=signer)
    assert read(store).status is ReadStatus.UNREADABLE
    assert signer.decrypted == []
    assert publish(store, {"a": 1})[0].status is PublishStatus.UNREADABLE


def test_a_later_page_longer_than_a_page_counts_as_missing_without_asking_the_signer():
    relay, signer = Relay(), CountingSigner()
    store = read_state_list(relay, signer=signer)
    publish(store, big_read_state(3_000))
    relay.put(page_d_tag(D, 1), None, created_at=NOW + 5,
              content="ENC[" + "x" * PAGE_PAYLOAD_MAX + "]")
    signer.decrypted.clear()
    answer = read(store)
    assert answer.status is ReadStatus.INCOMPLETE and answer.stale == (1,)
    assert all(len(ciphertext) <= PAGE_PAYLOAD_MAX for ciphertext in signer.decrypted)
    assert len(signer.decrypted) == page_count(relay) - 1


# -- publishing one event (list_relays.publish_once) ------------------------------

class _Pool:
    def __init__(self, error):
        self.error = error

    def publish(self, relays, signed):
        raise self.error


def test_an_event_the_pool_refuses_is_taken_by_no_relay_on_the_next_turn():
    from nostr.list_relays import publish_once
    for error in (ValueError("PublishJob requires a fully signed event"),
                  RuntimeError("Internal C++ object already deleted")):
        took = []
        publish_once(_Pool(error), ["wss://relay.example"], {}, on_done=took.append)
        assert took == []                          # never inside the call
        settle()
        assert took == [frozenset()]


def test_a_bug_in_publishing_is_not_hidden():
    from nostr.list_relays import publish_once
    with pytest.raises(TypeError):
        publish_once(_Pool(TypeError("a bug")), ["wss://relay.example"], {},
                     on_done=lambda took: None)
