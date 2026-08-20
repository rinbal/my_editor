# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nostr.imports.subscriptions.FeedSubscriptionStore``.

Every boundary is faked (bunker crypto/signing, relay publish, queries,
timer, clock, cache dir), so debounce, sync, encryption shape, and the
never-clobber-local-edits rule are all pinned deterministically.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from nostr.imports.constants import (
    FEED_LIST_DTAG,
    SUBSCRIPTIONS_KIND,
)
from nostr.imports.subscriptions import FeedSubscriptionStore, _parse_payload

from tests.imports_fakes import PROFILE, FakeRelayListCache


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


class FakeBunkerClient:
    def __init__(self):
        self.signed = []

    def nip44_encrypt_self(self, plaintext, on_success, on_failure, **_kw):
        on_success("ENC[" + plaintext + "]")

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **_kw):
        if ciphertext.startswith("ENC[") and ciphertext.endswith("]"):
            on_success(ciphertext[4:-1])
        else:
            on_failure("bad ciphertext")

    def sign_event(self, unsigned, on_success, on_failure, **_kw):
        signed = {**unsigned, "id": f"ev{len(self.signed)}", "sig": "s" * 8}
        self.signed.append(signed)
        on_success(signed)


class FakeSessionPool:
    def __init__(self, client=None, error=None):
        self.client = client or FakeBunkerClient()
        self.error = error

    def get(self, profile, on_ready=None, on_error=None):
        if self.error:
            on_error(self.error)
        else:
            on_ready(self.client)


class FakePublisher:
    def __init__(self, accepted=1, total=2):
        self.calls = []
        self.accepted = accepted
        self.total = total

    def __call__(self, relays, signed, *, on_done):
        self.calls.append((list(relays), signed))
        on_done(self.accepted, self.total)


class FakeScheduler:
    def __init__(self):
        self.scheduled = []   # (ms, fn)
        self.cancelled = 0

    def __call__(self, ms, fn):
        self.scheduled.append((ms, fn))

        def _cancel():
            self.cancelled += 1

        return _cancel

    def fire_last(self):
        self.scheduled[-1][1]()


class FakeQuery:
    def __init__(self, event=None):
        self.event = event
        self.calls = []

    def latest(self, relays, filters, on_done):
        self.calls.append((list(relays), list(filters)))
        on_done(self.event)


def make_store(tmp_path, *, session_pool=None, publisher=None,
               scheduler=None, query=None, clock=None):
    publisher = publisher or FakePublisher()
    scheduler = scheduler or FakeScheduler()
    store = FeedSubscriptionStore(
        session_pool=session_pool or FakeSessionPool(),
        relay_pool=None,
        relay_list_cache=FakeRelayListCache(),
        cache_dir=tmp_path,
        query=query,
        publisher=publisher,
        scheduler=scheduler,
        clock=clock or (lambda: 1_700_000_000),
    )
    return store, publisher, scheduler


FEED_URL = "https://blog.example/feed"


class TestMutations:
    def test_add_validates_through_registry(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        store.bind_profile(PROFILE)
        assert store.add_feed(FEED_URL, "My Blog") == {"added": True}
        assert store.add_feed("not a url <xml>") == {
            "added": False, "invalid": True}
        assert store.add_feed(FEED_URL) == {"added": False, "duplicate": True}
        assert store.has_feed(FEED_URL)
        assert store.get(FEED_URL).title == "My Blog"

    def test_remove(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL)
        assert store.remove_feed(FEED_URL) is True
        assert store.remove_feed(FEED_URL) is False
        assert store.feeds == []

    def test_mark_fetched_uses_clock(self, tmp_path):
        store, _, _ = make_store(tmp_path, clock=lambda: 42)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL)
        store.mark_fetched(FEED_URL)
        assert store.get(FEED_URL).last_fetched_at == 42

    def test_cache_round_trip(self, tmp_path):
        store, _, scheduler = make_store(tmp_path)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL, "My Blog")
        store.mark_fetched(FEED_URL, when=99)
        # A second store instance sees the cached list immediately.
        store2, _, _ = make_store(tmp_path)
        store2.bind_profile(PROFILE)
        assert [f.url for f in store2.feeds] == [FEED_URL]
        assert store2.get(FEED_URL).last_fetched_at == 99


class TestPublish:
    def test_debounced_changes_publish_once_encrypted(self, tmp_path):
        store, publisher, scheduler = make_store(tmp_path)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL, "My Blog")
        store.add_feed("https://other.example/rss.xml")
        # Two mutations, re-scheduled each time, nothing published yet.
        assert publisher.calls == []
        assert len(scheduler.scheduled) == 2
        scheduler.fire_last()
        assert len(publisher.calls) == 1

        _relays, signed = publisher.calls[0]
        assert signed["kind"] == SUBSCRIPTIONS_KIND
        assert ["d", FEED_LIST_DTAG] in signed["tags"]
        assert ["encrypted", "nip44_v2"] in signed["tags"]
        # Content is ciphertext; the URLs never appear in plaintext.
        assert signed["content"].startswith("ENC[")
        payload = json.loads(signed["content"][4:-1])
        assert [f["url"] for f in payload["feeds"]] == [
            FEED_URL, "https://other.example/rss.xml"]

    def test_flush_publishes_pending_changes_immediately(self, tmp_path):
        store, publisher, scheduler = make_store(tmp_path)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL)
        assert publisher.calls == []
        store.flush()
        assert len(publisher.calls) == 1
        # The parked debounce was cancelled, not left to double-publish.
        assert scheduler.cancelled >= 1

    def test_flush_without_changes_is_a_noop(self, tmp_path):
        store, publisher, _ = make_store(tmp_path)
        store.bind_profile(PROFILE)
        store.flush()
        assert publisher.calls == []

    def test_signer_failure_keeps_changes_queued(self, tmp_path):
        store, publisher, scheduler = make_store(
            tmp_path, session_pool=FakeSessionPool(error="signer offline"))
        statuses = []
        store.sync_status.connect(statuses.append)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL)
        scheduler.fire_last()
        assert publisher.calls == []
        assert store._dirty is True
        assert any("signer offline" in s for s in statuses)

    def test_zero_relay_acceptance_keeps_dirty(self, tmp_path):
        store, publisher, scheduler = make_store(
            tmp_path, publisher=FakePublisher(accepted=0, total=2))
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL)
        scheduler.fire_last()
        assert store._dirty is True


class TestRelaySync:
    def _remote_event(self, urls):
        payload = {"feeds": [{"url": u} for u in urls], "updated_at": 1}
        return {
            "kind": SUBSCRIPTIONS_KIND,
            "content": "ENC[" + json.dumps(payload) + "]",
            "tags": [["d", FEED_LIST_DTAG]],
        }

    def test_relay_list_adopted_on_bind(self, tmp_path):
        query = FakeQuery(self._remote_event(["https://remote.example/feed"]))
        store, _, _ = make_store(tmp_path, query=query)
        store.bind_profile(PROFILE)
        assert [f.url for f in store.feeds] == ["https://remote.example/feed"]
        # The query asked for exactly our namespaced d-tag.
        _relays, filters = query.calls[0]
        assert filters[0]["#d"] == [FEED_LIST_DTAG]

    def test_relay_refresh_never_clobbers_unsynced_edits(self, tmp_path):
        # The remote answer arrives while a local add is still pending.
        class SlowQuery(FakeQuery):
            def __init__(self, event, store_ref):
                super().__init__(event)
                self.store_ref = store_ref

            def latest(self, relays, filters, on_done):
                # A local edit lands before the relay answers.
                self.store_ref["store"].add_feed(FEED_URL, "Local")
                super().latest(relays, filters, on_done)

        ref = {}
        query = SlowQuery(self._remote_event(["https://remote.example/feed"]), ref)
        store, _, _ = make_store(tmp_path, query=query)
        ref["store"] = store
        store.bind_profile(PROFILE)
        # The local, unsynced edit survives; remote state did not clobber.
        assert store.has_feed(FEED_URL)

    def test_undecryptable_remote_state_is_ignored(self, tmp_path):
        query = FakeQuery({
            "kind": SUBSCRIPTIONS_KIND, "content": "garbage", "tags": []})
        store, _, _ = make_store(tmp_path, query=query)
        store.bind_profile(PROFILE)
        assert store.feeds == []

    def test_profile_switch_clears_list(self, tmp_path):
        store, _, _ = make_store(tmp_path)
        store.bind_profile(PROFILE)
        store.add_feed(FEED_URL)
        other = SimpleNamespace(user_pubkey="cd" * 32, bunker_relays=[])
        store.bind_profile(other)
        assert store.feeds == []


class TestPayloadParsing:
    def test_junk_rows_dropped(self):
        payload = {"feeds": [
            {"url": FEED_URL, "title": "ok", "last_fetched_at": 5},
            {"url": "<?xml paste>"},
            {"url": ""},
            "not a dict",
            {"url": FEED_URL},  # duplicate
            {"url": "https://b.example/feed", "last_fetched_at": "bad"},
        ]}
        feeds = _parse_payload(payload)
        assert [f.url for f in feeds] == [FEED_URL, "https://b.example/feed"]
        assert feeds[0].last_fetched_at == 5
        assert feeds[1].last_fetched_at == 0

    def test_non_dict_payload(self):
        assert _parse_payload(None) == []
        assert _parse_payload([1, 2]) == []
