# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nostr.imports.subscriptions.FeedSubscriptionStore``.

The list is shared with EINUNDZWANZIG STANDUP, so most of what is pinned
here is that both apps can use it at once: this app reads STANDUP's
list, writes exactly its format, keeps what it does not understand,
merges instead of overwriting (also with a relay answer that arrives
while a publish is on its way), never writes over a list it could not
read, and merges its own older list in once.

Every boundary is faked (signer, relays, timer, clock, cache dir). The
fake relay keeps what is published and answers queries from it, so two
stores on one relay behave like two apps on one account. Events are
really signed, because only the account's own signature counts.
"""

import json
from types import SimpleNamespace

import pytest

from nostr import events
from nostr.imports.constants import (
    FEED_LIST_DTAG,
    LEGACY_FEED_LIST_DTAG,
    SUBSCRIPTIONS_KIND,
)
from nostr.imports.subscriptions import FeedSubscriptionStore
from nostr.outbox import defaults
from tests.outbox_fakes import OTHER_SK, PK, SK, FakeRelayDirectory, settle

PROFILE = SimpleNamespace(user_pubkey=PK, bunker_relays=["wss://bunker.example"])
FEED_URL = "https://blog.example/feed"
NOW = 1_800_000_000


class FakeSigner:
    """Encrypts as ``ENC[...]`` and signs for real. ``hold`` keeps each
    request waiting until :meth:`release` (a phone signer)."""

    def __init__(self, *, nip04=False, hold=False):
        self.signed = []
        self.hold = hold
        self.waiting = []
        if nip04:
            self.nip04_decrypt_self = lambda ct, on_success, on_failure, **_kw: on_success(
                ct[4:-1])

    def nip44_encrypt_self(self, plaintext, on_success, on_failure, **_kw):
        on_success("ENC[" + plaintext + "]")

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **_kw):
        if ciphertext.startswith("ENC[") and ciphertext.endswith("]"):
            on_success(ciphertext[4:-1])
        else:
            on_failure("bad ciphertext")

    def sign_event(self, unsigned, on_success, on_failure, **_kw):
        def answer():
            signed = events.sign_event(dict(unsigned), SK)
            self.signed.append(signed)
            on_success(signed)
        if self.hold:
            self.waiting.append(answer)
        else:
            answer()

    def release(self):
        while self.waiting:
            self.waiting.pop(0)()


class FakeSessionPool:
    def __init__(self, client=None, error=None):
        self.client = client or FakeSigner()
        self.error = error

    def get(self, profile, on_ready=None, on_error=None):
        if self.error:
            on_error(self.error)
        else:
            on_ready(self.client)


class FakeRelay:
    """One relay the stores share: keeps the newest event per d tag."""

    def __init__(self, accepted=True):
        self.stored = {}
        self.published = []
        self.queries = []
        self.accepted = accepted

    # The query surface: every event, and the relays that answered.
    def events(self, relays, filters, on_done):
        self.queries.append((list(relays), filters))
        d_tag = filters[0]["#d"][0]
        stored = self.stored.get(d_tag)
        on_done([stored] if stored else [], set(relays))

    # The publisher seam: the relays that took it.
    def __call__(self, relays, signed, *, on_done, every_relay=False):
        self.published.append((list(relays), signed))
        if self.accepted:
            d_tag = next(t[1] for t in signed["tags"] if t[0] == "d")
            self.stored[d_tag] = signed
        on_done(frozenset(relays) if self.accepted else frozenset())

    def put(self, d_tag, payload, *, tag=("encrypted", "nip44"), created_at=NOW - 100,
            sk=SK, encrypt=True):
        content = json.dumps(payload)
        if encrypt:
            content = "ENC[" + content + "]"
        tags = [["d", d_tag]] + ([list(tag)] if tag else [])
        self.stored[d_tag] = events.sign_event(
            {"kind": SUBSCRIPTIONS_KIND, "content": content, "tags": tags,
             "created_at": created_at}, sk)

    def payload(self, d_tag=FEED_LIST_DTAG):
        content = self.stored[d_tag]["content"]
        return json.loads(content[4:-1])


class FakeScheduler:
    def __init__(self):
        self.scheduled = []
        self.cancelled = 0

    def __call__(self, ms, fn):
        entry = [ms, fn, True]
        self.scheduled.append(entry)

        def _cancel():
            entry[2] = False
            self.cancelled += 1

        return _cancel

    def fire(self):
        live = [e for e in self.scheduled if e[2]]
        assert live, "nothing scheduled"
        live[-1][2] = False
        live[-1][1]()


def make_store(tmp_path, *, relay=None, session_pool=None, directory=None,
               entitled=None, clock=None, scheduler=None):
    relay = relay if relay is not None else FakeRelay()
    scheduler = scheduler or FakeScheduler()
    store = FeedSubscriptionStore(
        session_pool=session_pool or FakeSessionPool(),
        relay_pool=None,
        relay_directory=directory or FakeRelayDirectory(),
        entitled_relays=entitled,
        cache_dir=tmp_path,
        query=relay,
        publisher=relay,
        scheduler=scheduler,
        clock=clock or (lambda: NOW),
    )
    return store, relay, scheduler


def bound(tmp_path, **kw):
    store, relay, scheduler = make_store(tmp_path, **kw)
    store.bind_profile(PROFILE)
    settle()
    return store, relay, scheduler


def publish(store, scheduler):
    scheduler.fire()
    settle()


STANDUP_PAYLOAD = {
    "feeds": [
        {"url": "https://standup.example/feed", "title": "From STANDUP",
         "lastFetchedAt": 1_700_000_000, "pinned": True},
        # Not a source this app can read; it is STANDUP's to keep.
        {"url": "something-standup-reads", "title": "Other"},
    ],
    "sourceOptions": {
        "https://standup.example/feed": {"rehostImages": False, "fetchFullText": True},
        "https://server-checked.example/rss": {"fetchFullText": False},
    },
    "updatedAt": 1_700_000_000,
    "futureKey": {"keep": "me"},
}


class TestMutations:
    def test_add_validates_through_registry(self, tmp_path):
        store, _, _ = bound(tmp_path)
        assert store.add_feed(FEED_URL, "My Blog") == {"added": True}
        assert store.add_feed("not a url <xml>") == {"added": False, "invalid": True}
        assert store.add_feed("<rss>https://x.example/feed") == {
            "added": False, "invalid": True}
        assert store.add_feed(FEED_URL.upper()) == {"added": False, "duplicate": True}
        assert store.has_feed(FEED_URL)
        assert store.get(FEED_URL).title == "My Blog"

    def test_remove(self, tmp_path):
        store, _, _ = bound(tmp_path)
        store.add_feed(FEED_URL)
        assert store.remove_feed(FEED_URL) is True
        assert store.remove_feed(FEED_URL) is False
        assert store.feeds == []

    def test_options_default_on_and_are_remembered(self, tmp_path):
        store, _, _ = bound(tmp_path)
        store.add_feed(FEED_URL)
        assert store.options_for(FEED_URL).rehost_images is True
        store.set_source_options(FEED_URL, rehost_images=False)
        options = store.options_for(FEED_URL)
        assert (options.rehost_images, options.fetch_full_text) == (False, True)

    def test_cache_round_trip(self, tmp_path):
        store, _, _ = bound(tmp_path)
        store.add_feed(FEED_URL, "My Blog")
        store.mark_fetched(FEED_URL, when=99)
        store2, _, _ = bound(tmp_path, relay=FakeRelay(accepted=False))
        assert [f.url for f in store2.feeds] == [FEED_URL]
        assert store2.get(FEED_URL).last_fetched_at == 99

    def test_older_cache_is_read(self, tmp_path):
        (tmp_path / f"feed_sources_{PK}.json").write_text(json.dumps({
            "feeds": [{"url": FEED_URL, "title": "Old", "last_fetched_at": 7}],
            "updated_at": 1}), encoding="utf-8")
        store, _, _ = make_store(tmp_path, relay=FakeRelay(accepted=False))
        store.bind_profile(PROFILE)
        assert store.get(FEED_URL).last_fetched_at == 7


class TestStandupFormat:
    def test_reads_standups_list(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, STANDUP_PAYLOAD)
        store, _, _ = bound(tmp_path, relay=relay)
        assert [f.url for f in store.feeds] == ["https://standup.example/feed"]
        assert store.get("https://standup.example/feed").title == "From STANDUP"
        assert store.options_for("https://standup.example/feed").rehost_images is False
        # Its query asked for the shared list.
        assert relay.queries[0][1][0]["#d"] == [FEED_LIST_DTAG]

    def test_writes_exactly_standups_format(self, tmp_path):
        store, relay, scheduler = bound(tmp_path)
        store.add_feed(FEED_URL, "My Blog")
        store.set_source_options(FEED_URL, fetch_full_text=False)
        publish(store, scheduler)
        _relays, signed = relay.published[-1]
        assert signed["kind"] == SUBSCRIPTIONS_KIND
        assert signed["tags"] == [["d", FEED_LIST_DTAG], ["client", "MyEditor"],
                                  ["encrypted", "nip44"]]
        assert signed["content"].startswith("ENC[")
        payload = relay.payload()
        assert set(payload) == {"feeds", "sourceOptions", "updatedAt"}
        assert payload["feeds"] == [{"url": FEED_URL, "title": "My Blog"}]
        assert payload["sourceOptions"] == {FEED_URL: {"fetchFullText": False}}
        assert payload["updatedAt"] == NOW

    def test_keeps_what_it_does_not_understand(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, STANDUP_PAYLOAD)
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.add_feed(FEED_URL)
        publish(store, scheduler)
        payload = relay.payload()
        assert payload["futureKey"] == {"keep": "me"}
        urls = [f["url"] for f in payload["feeds"]]
        assert urls == ["https://standup.example/feed", "something-standup-reads", FEED_URL]
        assert payload["feeds"][0]["pinned"] is True
        assert payload["feeds"][0]["lastFetchedAt"] == 1_700_000_000
        assert payload["sourceOptions"]["https://server-checked.example/rss"] == {
            "fetchFullText": False}

    def test_reads_a_list_with_the_older_tag_value(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]}, tag=("encrypted", "nip44_v2"))
        store, _, _ = bound(tmp_path, relay=relay)
        assert store.has_feed(FEED_URL)

    def test_reads_a_list_without_encryption(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]}, tag=None, encrypt=False)
        store, _, _ = bound(tmp_path, relay=relay)
        assert store.has_feed(FEED_URL)

    def test_reads_nip04_when_the_signer_can(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]}, tag=("encrypted", "nip04"))
        store, _, _ = bound(tmp_path, relay=relay,
                            session_pool=FakeSessionPool(FakeSigner(nip04=True)))
        assert store.has_feed(FEED_URL)

    def test_never_writes_over_a_list_it_cannot_read(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": "https://standup.example/feed"}]},
                  tag=("encrypted", "nip04"))
        statuses = []
        store, _, scheduler = make_store(tmp_path, relay=relay)
        store.sync_status.connect(statuses.append)
        store.bind_profile(PROFILE)
        settle()
        store.add_feed(FEED_URL)
        publish(store, scheduler)
        assert relay.published == []
        assert store.has_feed(FEED_URL)            # kept here
        assert store._dirty is True               # and still to be sent
        assert any("older kind of encryption" in s for s in statuses)

    def test_a_list_signed_by_someone_else_is_ignored(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": "https://planted.example/feed"}]},
                  sk=OTHER_SK)
        store, _, _ = bound(tmp_path, relay=relay)
        assert store.feeds == []

    def test_undecryptable_remote_state_is_ignored(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": []}, encrypt=False)
        relay.stored[FEED_LIST_DTAG] = events.sign_event(
            {"kind": SUBSCRIPTIONS_KIND, "content": "garbage",
             "tags": [["d", FEED_LIST_DTAG], ["encrypted", "nip44"]], "created_at": 1}, SK)
        store, _, _ = bound(tmp_path, relay=relay)
        assert store.feeds == []


class TestMigration:
    def test_the_older_list_is_merged_in_once(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, STANDUP_PAYLOAD)
        relay.put(LEGACY_FEED_LIST_DTAG, {
            "feeds": [{"url": FEED_URL, "title": "Mine", "last_fetched_at": 5},
                      {"url": "https://standup.example/feed"}],
            "updated_at": 1}, tag=("encrypted", "nip44_v2"))
        store, _, scheduler = bound(tmp_path, relay=relay)
        assert store.get(FEED_URL).last_fetched_at == 5
        publish(store, scheduler)
        urls = [f["url"] for f in relay.payload()["feeds"]]
        assert urls == ["https://standup.example/feed", "something-standup-reads", FEED_URL]
        assert relay.payload()["feeds"][2] == {"url": FEED_URL, "title": "Mine",
                                               "lastFetchedAt": 5}
        # The older list is never written again, and not read again.
        assert all(signed["tags"][0] == ["d", FEED_LIST_DTAG]
                   for _relays, signed in relay.published)
        asked = len(relay.queries)
        store2, _, _ = bound(tmp_path, relay=relay)
        assert [q[1][0]["#d"] for q in relay.queries[asked:]] == [[FEED_LIST_DTAG]]


class TestPublish:
    def test_debounced_changes_publish_once(self, tmp_path):
        store, relay, scheduler = bound(tmp_path)
        store.add_feed(FEED_URL, "My Blog")
        store.add_feed("https://other.example/rss.xml")
        assert relay.published == []
        publish(store, scheduler)
        assert len(relay.published) == 1
        assert [f["url"] for f in relay.payload()["feeds"]] == [
            FEED_URL, "https://other.example/rss.xml"]

    def test_flush_publishes_pending_changes_immediately(self, tmp_path):
        store, relay, scheduler = bound(tmp_path)
        store.add_feed(FEED_URL)
        store.flush()
        settle()
        assert len(relay.published) == 1
        assert scheduler.cancelled >= 1

    def test_flush_without_changes_is_a_noop(self, tmp_path):
        store, relay, _ = bound(tmp_path)
        store.flush()
        settle()
        assert relay.published == []

    def test_signer_failure_keeps_changes_queued(self, tmp_path):
        statuses = []
        store, relay, scheduler = make_store(
            tmp_path, session_pool=FakeSessionPool(error="signer offline"))
        store.sync_status.connect(statuses.append)
        store.bind_profile(PROFILE)
        settle()
        store.add_feed(FEED_URL)
        publish(store, scheduler)
        assert relay.published == []
        assert store._dirty is True
        assert any("signer offline" in s for s in statuses)

    def test_zero_relay_acceptance_keeps_dirty(self, tmp_path):
        store, relay, scheduler = bound(tmp_path, relay=FakeRelay(accepted=False))
        store.add_feed(FEED_URL)
        publish(store, scheduler)
        assert store._dirty is True

    def test_dated_after_the_list_it_replaces(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": []}, created_at=NOW + 50)
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.add_feed(FEED_URL)
        publish(store, scheduler)
        assert relay.published[-1][1]["created_at"] == NOW + 51

    def test_the_list_is_read_where_it_is_written(self, tmp_path):
        directory = FakeRelayDirectory({PK: ["wss://home.example"]})
        store, relay, scheduler = bound(tmp_path, directory=directory,
                                        entitled=lambda: ["wss://members.example"])
        store.add_feed(FEED_URL)
        publish(store, scheduler)
        read_relays = relay.queries[0][0]
        written_relays = relay.published[0][0]
        assert written_relays == [
            "wss://home.example", "wss://members.example", "wss://bunker.example"]
        assert read_relays == written_relays + list(defaults.FALLBACK_RELAYS)

    def test_profile_switch_sends_pending_edits_first(self, tmp_path):
        store, relay, _ = bound(tmp_path)
        store.add_feed(FEED_URL)
        other = SimpleNamespace(user_pubkey="cd" * 32, bunker_relays=[])
        store.bind_profile(other)
        settle()
        assert store.feeds == []
        assert [f["url"] for f in relay.payload()["feeds"]] == [FEED_URL]
        cached = json.loads((tmp_path / f"feed_sources_{PK}.json").read_text(encoding="utf-8"))
        assert cached["dirty"] is False


class TestMerging:
    """Two stores on one relay: two apps on one account."""

    def test_edits_in_both_apps_survive(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": "https://shared.example/feed"}]})
        one, _, one_timer = bound(tmp_path / "one", relay=relay)
        two, _, two_timer = bound(tmp_path / "two", relay=relay)
        one.add_feed(FEED_URL)
        two.add_feed("https://two.example/feed")
        two.remove_feed("https://shared.example/feed")
        publish(one, one_timer)
        publish(two, two_timer)
        urls = [f["url"] for f in relay.payload()["feeds"]]
        assert urls == [FEED_URL, "https://two.example/feed"]
        # And the first app picks the second's edits up on its next read.
        one.refresh()
        settle()
        assert [f.url for f in one.feeds] == urls

    def test_a_title_changed_here_wins_otherwise_the_remote_one(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL, "title": "Old"},
                                             {"url": "https://b.example/feed",
                                              "title": "B"}]})
        one, _, one_timer = bound(tmp_path / "one", relay=relay)
        two, _, two_timer = bound(tmp_path / "two", relay=relay)
        two.update_title("https://b.example/feed", "B renamed")
        publish(two, two_timer)
        one.update_title(FEED_URL, "New")
        publish(one, one_timer)
        titles = [f.get("title") for f in relay.payload()["feeds"]]
        assert titles == ["New", "B renamed"]

    def test_a_relay_answer_during_a_publish_does_not_undo_an_edit(self, tmp_path):
        # The relay still has the old list while this publish waits for the
        # signer; a refresh that lands then must not drop the new source.
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": "https://shared.example/feed"}]})
        signer = FakeSigner(hold=True)
        store, _, scheduler = bound(tmp_path, relay=relay,
                                    session_pool=FakeSessionPool(signer))
        store.add_feed(FEED_URL)
        publish(store, scheduler)       # waits for the signer
        store.refresh()                 # the old list arrives meanwhile
        settle()
        assert store.has_feed(FEED_URL)
        signer.release()
        settle()
        assert store.has_feed(FEED_URL)
        assert [f["url"] for f in relay.payload()["feeds"]] == [
            "https://shared.example/feed", FEED_URL]

    def test_an_edit_made_during_a_publish_goes_out_right_after(self, tmp_path):
        signer = FakeSigner(hold=True)
        store, relay, _ = bound(tmp_path, session_pool=FakeSessionPool(signer))
        store.add_feed(FEED_URL)
        store.flush()
        settle()
        store.add_feed("https://later.example/feed")
        store.flush()
        assert store.is_busy
        signer.release()
        settle()
        # One write at a time (the shared list's rule): the second is
        # signed only once the first is done.
        assert len(relay.published) == 1 and len(signer.waiting) == 1
        signer.release()
        settle()
        assert [f["url"] for f in relay.payload()["feeds"]] == [
            FEED_URL, "https://later.example/feed"]
        assert len(relay.published) == 2
        assert not store.is_busy

    def test_nothing_is_published_when_the_relays_have_it_already(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]})
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.add_feed("https://x.example/feed")
        store.remove_feed("https://x.example/feed")
        publish(store, scheduler)
        assert relay.published == []
        assert store._dirty is False


class TestReadTimes:
    def test_a_read_time_alone_is_not_published(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]})
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.mark_fetched(FEED_URL, when=123)
        assert not [e for e in scheduler.scheduled if e[2]]
        assert relay.published == []

    def test_a_read_time_travels_with_a_real_change(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]})
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.mark_fetched(FEED_URL, when=123)
        store.add_feed("https://other.example/feed")
        publish(store, scheduler)
        assert relay.payload()["feeds"][0]["lastFetchedAt"] == 123

    def test_opening_a_manual_source_publishes_its_read_time(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]})
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.mark_fetched(FEED_URL, when=123, publish=True)
        publish(store, scheduler)
        assert relay.payload()["feeds"][0]["lastFetchedAt"] == 123


class TestQuitting:
    def test_waiting_for_a_flush_ends_when_it_is_sent(self, tmp_path):
        # The signer answers on a later turn of the event loop (as a key
        # kept on this computer does); quitting waits for that.
        from PySide6.QtCore import QTimer

        class LaterSigner(FakeSigner):
            def sign_event(self, unsigned, on_success, on_failure, **_kw):
                QTimer.singleShot(0, lambda: FakeSigner.sign_event(
                    self, unsigned, on_success, on_failure))

        store, relay, _ = bound(tmp_path, session_pool=FakeSessionPool(LaterSigner()))
        store.add_feed(FEED_URL)
        store.flush()
        assert store.is_busy
        assert store.wait_until_settled(2_000) is True
        assert [f["url"] for f in relay.payload()["feeds"]] == [FEED_URL]

    def test_waiting_gives_up_after_the_limit(self, tmp_path):
        signer = FakeSigner(hold=True)
        store, _, _ = bound(tmp_path, session_pool=FakeSessionPool(signer))
        store.add_feed(FEED_URL)
        store.flush()
        assert store.wait_until_settled(50) is False
        # What was not sent is sent on the next launch.
        cached = json.loads((tmp_path / f"feed_sources_{PK}.json").read_text(encoding="utf-8"))
        assert cached["dirty"] is True

    def test_nothing_to_wait_for(self, tmp_path):
        store, _, _ = bound(tmp_path)
        assert store.wait_until_settled(10) is True


@pytest.fixture(autouse=True)
def _settled():
    yield
    settle()


class Offline(FakeRelay):
    """Relays that answer nothing until ``online`` is set."""

    def __init__(self):
        super().__init__()
        self.online = False

    def events(self, relays, filters, on_done):
        if self.online:
            super().events(relays, filters, on_done)
        else:
            self.queries.append((list(relays), filters))
            on_done([], set())


class TestSaysWhatHappens:
    """CP2 review N3: nothing heard the list's messages since the Feeds
    page went, and a change that could not be shared waited silently."""

    def test_a_change_that_cannot_be_shared_says_so_and_tries_again(self, tmp_path):
        from nostr.imports.subscriptions import NOT_SHARED, RETRY_DELAYS_MS
        relay = Offline()
        store, _, scheduler = bound(tmp_path, relay=relay)
        said = []
        store.sync_status.connect(said.append)
        store.add_feed(FEED_URL, "My Blog")
        publish(store, scheduler)
        assert said[-1] == NOT_SHARED
        assert relay.published == []
        # Tried again after a pause; nothing to sign while nothing answers.
        assert [e[0] for e in scheduler.scheduled if e[2]] == [RETRY_DELAYS_MS[0]]
        publish(store, scheduler)
        assert [e[0] for e in scheduler.scheduled if e[2]] == [RETRY_DELAYS_MS[1]]
        relay.online = True
        publish(store, scheduler)
        assert [f["url"] for f in relay.payload()["feeds"]] == [FEED_URL]
        assert said[-1] == ""                   # nothing left to say
        assert not [e for e in scheduler.scheduled if e[2]]

    def test_a_read_that_is_answered_sends_what_waits(self, tmp_path):
        relay = Offline()
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.add_feed(FEED_URL, "My Blog")
        publish(store, scheduler)               # not shared: kept, dirty
        store.bind_profile(None)
        relay.online = True
        again, _, scheduler2 = make_store(tmp_path, relay=relay)
        again.bind_profile(PROFILE)             # the next launch
        settle()
        assert [e for e in scheduler2.scheduled if e[2]], "nothing was sent"
        publish(again, scheduler2)
        assert [f["url"] for f in relay.payload()["feeds"]] == [FEED_URL]

    def test_an_answered_read_does_not_wait_out_the_pause(self, tmp_path):
        """CP3 review L3: after a few tries that failed, the change waited
        out a pause of up to half an hour although the relays answered."""
        from nostr.imports.subscriptions import RETRY_DELAYS_MS
        relay = FakeRelay(accepted=False)
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.add_feed(FEED_URL, "Blog")
        for _ in range(4):
            publish(store, scheduler)
        live = lambda: [e[0] for e in scheduler.scheduled if e[2]]  # noqa: E731
        assert live() == [RETRY_DELAYS_MS[-1]]
        relay.accepted = True
        store.refresh()                         # answered
        settle()
        assert live() and live()[-1] < RETRY_DELAYS_MS[0]
        publish(store, scheduler)
        assert [f["url"] for f in relay.payload()["feeds"]] == [FEED_URL]
        assert not store._dirty

    def test_a_list_that_cannot_be_read_online_says_the_copy_here_is_shown(self, tmp_path):
        from nostr.imports.subscriptions import NOT_READ
        store, _, _ = make_store(tmp_path, relay=Offline())
        said = []
        store.sync_status.connect(said.append)
        store.bind_profile(PROFILE)
        settle()
        assert said[-1] == NOT_READ


class TestOldEncryption:
    """A list an older app saved with NIP-04 is read; what MyEditor writes
    back is NIP-44, never NIP-04."""

    def test_it_is_read_and_written_back_with_nip44(self, tmp_path):
        from nostr import crypto
        from nostr.local_signer import LocalSigner
        from tests.test_nip04 import PK1, SK1, TO_SELF
        account = SimpleNamespace(user_pubkey=PK1, bunker_relays=["wss://bunker.example"])
        relay = FakeRelay()
        relay.stored[FEED_LIST_DTAG] = events.sign_event(
            {"kind": SUBSCRIPTIONS_KIND, "content": TO_SELF, "created_at": NOW - 100,
             "tags": [["d", FEED_LIST_DTAG], ["encrypted", "nip04"]]}, bytes.fromhex(SK1))
        signer = LocalSigner(bytes.fromhex(SK1))
        store, _, scheduler = make_store(tmp_path, relay=relay,
                                         session_pool=FakeSessionPool(client=signer))
        store.bind_profile(account)
        settle()
        assert [(f.url, f.title) for f in store.feeds] == [
            ("https://blog.example/feed", "Ein Blog über Bäume")]
        store.add_feed("https://other.example/feed")
        publish(store, scheduler)
        settle()
        (_relays, written), = relay.published
        assert ["encrypted", "nip44"] in written["tags"]
        assert not any(t[:2] == ["encrypted", "nip04"] for t in written["tags"])
        sk = bytes.fromhex(SK1)
        payload = json.loads(crypto.decrypt_from(written["content"], sk, bytes.fromhex(PK1)))
        assert {f["url"] for f in payload["feeds"]} == {
            "https://blog.example/feed", "https://other.example/feed"}
        signer.close()

    def test_a_signer_app_that_does_not_read_it_says_what_to_do(self, tmp_path):
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": "https://standup.example/feed"}]},
                  tag=("encrypted", "nip04"))
        signer = FakeSigner()
        signer.nip04_decrypt_self = (
            lambda ct, on_success, on_failure, **_kw: on_failure("permission denied"))
        statuses = []
        store, _, _ = make_store(tmp_path, relay=relay,
                                 session_pool=FakeSessionPool(client=signer))
        store.sync_status.connect(statuses.append)
        store.bind_profile(PROFILE)
        settle()
        assert store.feeds == []
        assert any("older kind of encryption" in s for s in statuses)
        assert not any("permission denied" in s for s in statuses)

    @pytest.mark.parametrize("algorithm", ["nip44", "nip04"])
    @pytest.mark.parametrize("reason,said", [
        ("invalid MAC", "UNREADABLE"),
        ("timed out waiting for signer", "SIGNER_SILENT"),
        ("not connected", "SIGNER_SILENT")])
    def test_why_it_could_not_be_read_is_said_in_plain_words(self, tmp_path, algorithm,
                                                              reason, said):
        """CP3 review L5: the signer's own English reason stood inside the
        sentence (in German too). L9: a signer app that did not answer got
        the advice for a list saved with the older encryption."""
        from nostr.imports import subscriptions
        relay = FakeRelay()
        relay.put(FEED_LIST_DTAG, {"feeds": [{"url": FEED_URL}]}, tag=("encrypted", algorithm))
        signer = FakeSigner(nip04=True)
        fail = lambda ct, on_success, on_failure, **_kw: on_failure(reason)  # noqa: E731
        signer.nip04_decrypt_self = signer.nip44_decrypt_self = fail
        statuses = []
        store, _, _ = make_store(tmp_path, relay=relay,
                                 session_pool=FakeSessionPool(client=signer))
        store.sync_status.connect(statuses.append)
        store.bind_profile(PROFILE)
        settle()
        expected = getattr(subscriptions, said)
        if algorithm == "nip04" and said == "UNREADABLE":
            expected = subscriptions._OLD_ENCRYPTION      # a refusal: what to do
        assert statuses[-1] == expected
        assert not any(reason in s for s in statuses)


class TestReadingAgain:
    """``reload``: the list as another MyEditor on this computer left it
    (CP1 review L7), never dropping a change still waiting here."""

    def test_it_reads_what_the_other_one_saved(self, tmp_path):
        relay = FakeRelay()
        other, _, other_scheduler = bound(tmp_path, relay=relay)
        this, _, _ = bound(tmp_path, relay=relay)
        other.add_feed(FEED_URL, "Blog")          # saved to this computer's copy
        assert not this.has_feed(FEED_URL)
        this.reload()
        settle()
        assert this.has_feed(FEED_URL) and this.known

    def test_a_change_waiting_here_stays(self, tmp_path):
        relay = FakeRelay(accepted=False)
        store, _, scheduler = bound(tmp_path, relay=relay)
        store.add_feed(FEED_URL, "Blog")
        publish(store, scheduler)                 # refused: still waiting
        assert store._dirty
        store.reload()
        settle()
        assert store.has_feed(FEED_URL) and store._dirty
