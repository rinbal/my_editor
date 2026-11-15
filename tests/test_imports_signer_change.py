# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The same account, signing another way (review H2).

Pairing a signer app for an account whose key was kept here, or the
other way round, keeps the account but changes how it signs. Everything
that signs or decrypts for it must follow: the list of sources used to
keep the old signer, so its edits were never sent and the signer app's
session was torn down at every sync.
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from nostr.imports.subscriptions import FeedSubscriptionStore
from nostr.imports_controller import ImportsController
from tests.imports_fakes import FakeCatalogue, FakeFetcher, inline_run_blocking
from tests.outbox_fakes import FakeRelayDirectory, settle
from tests.test_imports_create import Jobs
from tests.test_imports_subscriptions import FakeRelay, FakeScheduler, FakeSigner

PK = "ab" * 32


class RecordingPool:
    """A session pool that says which profile each request came with."""

    def __init__(self):
        self.asked = []
        self.client = FakeSigner()

    def get(self, profile, on_ready=None, on_error=None):
        self.asked.append(profile.signer)
        on_ready(self.client)


class Checker(QObject):
    checking = Signal(str, bool)
    source_checked = Signal(str, int)

    def start(self):
        pass

    def stop(self):
        pass


def account(signer: str):
    return SimpleNamespace(user_pubkey=PK, bunker_relays=[], display_name="Ada",
                           signer=signer)


def make(tmp_path):
    pool, relay, catalogue = RecordingPool(), FakeRelay(), FakeCatalogue()
    store = FeedSubscriptionStore(
        session_pool=pool, relay_pool=None, relay_directory=FakeRelayDirectory(),
        cache_dir=tmp_path / "cache", query=relay, publisher=relay,
        scheduler=FakeScheduler(), clock=lambda: 1_800_000_000)
    jobs = Jobs()
    controller = ImportsController(
        relay_pool=None, relay_directory=FakeRelayDirectory(), session_pool=pool,
        config_dir=tmp_path, subscription_store=store, fetcher=FakeFetcher({}),
        run_blocking=inline_run_blocking, checker_factory=lambda inbox: Checker(),
        catalogue_factory=lambda inbox: catalogue, item_job_factory=jobs.factory)
    return controller, pool, relay, catalogue


def test_the_list_follows_the_new_signer(tmp_path):
    controller, pool, relay, _catalogue = make(tmp_path)
    controller.account_changed(account("local"))
    settle()
    paired = account("remote")
    controller.account_changed(paired)        # a signer app was paired
    assert controller.subscriptions._profile is paired
    pool.asked.clear()
    controller.subscriptions.add_feed("https://blog.example/feed", "Blog")
    controller.subscriptions.flush()
    settle()
    # The edit went out, through the signer app only.
    assert pool.asked and set(pool.asked) == {"remote"}
    assert relay.published
    # Nothing was reloaded: the same account keeps its list.
    assert controller.is_followed("https://blog.example/feed")
    controller.account_changed(None)


def test_imports_ask_through_the_new_signer(tmp_path):
    controller, _pool, _relay, catalogue = make(tmp_path)
    controller.account_changed(account("local"))
    settle()
    paired = account("remote")
    controller.account_changed(paired)
    asked = []
    catalogue.look_up = lambda profile, tags, on_ready, on_unavailable: asked.append(profile)
    job = controller.runner.create(label="x", source_type="inbox", posts=[])
    controller.runner.run(job.id)
    assert asked == [paired]
    controller.account_changed(None)


def test_another_account_is_still_a_switch(tmp_path):
    store = FeedSubscriptionStore(
        session_pool=RecordingPool(), relay_pool=None, relay_directory=FakeRelayDirectory(),
        cache_dir=tmp_path / "cache", query=FakeRelay(), publisher=FakeRelay(),
        scheduler=FakeScheduler(), clock=lambda: 1_800_000_000)
    first = account("local")
    store.bind_profile(first)
    store.add_feed("https://blog.example/feed")
    other = SimpleNamespace(user_pubkey="cd" * 32, bunker_relays=[], signer="local")
    store.update_profile(other)
    assert store._profile is other
    assert store.feeds == []


class Job(QObject):
    first_accept = Signal(str)
    all_done = Signal(list)


def test_one_relay_that_accepts_is_enough(tmp_path):
    """A relay that never answers must not hold up quitting for the whole
    publish timeout: the list is safe once one relay has it."""
    job = Job()
    pool = SimpleNamespace(publish=lambda relays, signed: job)
    store = FeedSubscriptionStore(
        session_pool=RecordingPool(), relay_pool=pool, relay_directory=FakeRelayDirectory(),
        cache_dir=tmp_path / "cache", scheduler=FakeScheduler(), clock=lambda: 1)
    answers = []
    store._default_publisher(["wss://a", "wss://b"], {"id": "x"}, on_done=answers.append)
    job.first_accept.emit("wss://a")
    # Answered at once, not when the last relay times out.
    assert answers == [{"wss://a"}]
    job.all_done.emit([("wss://a", True, ""), ("wss://b", False, "timed out")])
    assert answers == [{"wss://a"}]
    silent = Job()
    pool.publish = lambda relays, signed: silent
    store._default_publisher(["wss://a"], {"id": "y"}, on_done=answers.append)
    silent.all_done.emit([("wss://a", False, "blocked")])
    assert answers[-1] == set()


def test_a_later_page_waits_for_every_relay(tmp_path):
    """A list's later pages count only on the relays that took all of
    them (contract 13): the answer waits for every relay."""
    job = Job()
    pool = SimpleNamespace(publish=lambda relays, signed: job)
    store = FeedSubscriptionStore(
        session_pool=RecordingPool(), relay_pool=pool, relay_directory=FakeRelayDirectory(),
        cache_dir=tmp_path / "cache", scheduler=FakeScheduler(), clock=lambda: 1)
    answers = []
    store._default_publisher(["wss://a", "wss://b", "wss://c"], {"id": "x"},
                             on_done=answers.append, every_relay=True)
    job.first_accept.emit("wss://a")
    assert answers == []
    job.all_done.emit([("wss://a", True, ""), ("wss://b", False, "timed out"),
                       ("wss://c", True, "")])
    assert answers == [{"wss://a", "wss://c"}]
