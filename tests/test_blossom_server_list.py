# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""BUD-03 kind 10063: parsing, caching and the AD-13 merge policy.

Everything here is offline. The relay pool is a fake that hands back
whatever the test hands it, so no REQ ever leaves the process, and the
settings store always lives under ``tmp_path``.

The merge matrix is the point of this file. AD-13 says a discovered
server list may be used for retrieval, may be offered, and may be
adopted only when the user has configured nothing at all. Every cell of
"local config x published list" is exercised below, including the one
that matters most: a user who has both, and whose two lists disagree.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from nostr import DEFAULT_RELAYS
from nostr.blossom import server_list
from nostr.blossom.server_list import (
    BLOSSOM_SERVER_LIST_KIND,
    MAX_SERVER_LIST_ENTRIES,
    ServerListCache,
    UserServerList,
    build_server_list_event,
    parse_server_list,
)
from nostr.blossom.servers import DEFAULT_BLOSSOM_SERVERS
from nostr.blossom.settings import (
    CONFIG_ORIGIN_BUD03,
    CONFIG_ORIGIN_DEFAULT,
    CONFIG_ORIGIN_UNSET,
    CONFIG_ORIGIN_USER,
    BlossomSettings,
)
from tests.blossom_fakes import PUBKEY


# The example event from specs/bud-03.md lines 15-28, verbatim.
BUD03_EXAMPLE = {
    "id": "e4bee088334cb5d38cff1616e964369c37b6081be997962ab289d6c671975d71",
    "pubkey": "781208004e09102d7da3b7345e64fd193cd1bc3fce8fdae6008d77f9cabcd036",
    "content": "",
    "kind": 10063,
    "created_at": 1708774162,
    "tags": [
        ["server", "https://cdn.self.hosted"],
        ["server", "https://cdn.satellite.earth"],
    ],
    "sig": "cc5efa74f59e80622c77cacf4dd62076bcb7581b45e9acff471e7963a1f4d8b34"
           "06adab5ee1ac9673487480e57d20e523428e60ffcc7e7a904ac882cfccfc653",
}


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    # ``fetch_latest_event`` arms a QTimer, which needs an application
    # object to attach to even though nothing here ever runs the loop.
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _event(*servers, **extra) -> dict:
    event = {
        "kind": BLOSSOM_SERVER_LIST_KIND,
        "pubkey": PUBKEY,
        "content": "",
        "tags": [["server", s] for s in servers],
    }
    event.update(extra)
    return event


def _settings(tmp_path) -> BlossomSettings:
    return BlossomSettings(path=tmp_path / "blossom_servers.json")


# --------------------------------------------------------------------------- #
# Fake relay pool                                                             #
# --------------------------------------------------------------------------- #

class FakeSub(QObject):
    """The three members ``fetch_latest_event`` uses off a subscription."""

    event = Signal(dict)
    eose = Signal()

    def __init__(self, filters) -> None:
        super().__init__()
        self.filters = filters
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePool(QObject):
    """Records every REQ and settles only when a test says so."""

    def __init__(self) -> None:
        super().__init__()
        self.subs: list = []
        self.relay_sets: list = []
        self.published: list = []

    def subscribe(self, urls, filters, sub_id=None) -> FakeSub:
        sub = FakeSub(filters)
        self.subs.append(sub)
        self.relay_sets.append(list(urls))
        return sub

    def publish(self, urls, event, timeout_ms=8000):
        # Present so that a publish would be recorded rather than raise.
        # Nothing in discovery may ever reach it (AD-13.5).
        self.published.append(event)
        return None

    # -- driving -----------------------------------------------------------

    def deliver(self, event: dict, index: int = -1) -> None:
        sub = self.subs[index]
        sub.event.emit(event)
        sub.eose.emit()

    def deliver_nothing(self, index: int = -1) -> None:
        self.subs[index].eose.emit()


# --------------------------------------------------------------------------- #
# parse_server_list                                                           #
# --------------------------------------------------------------------------- #

def test_parses_the_bud03_example_event_in_order():
    assert parse_server_list(BUD03_EXAMPLE) == [
        "https://cdn.self.hosted",
        "https://cdn.satellite.earth",
    ]


def test_schemes_survive_parsing():
    # BUD-03 line 9 requires the full URL including the scheme, and the
    # retrieval path builds request URLs straight out of these values.
    for entry in parse_server_list(BUD03_EXAMPLE):
        assert entry.startswith("https://")


def test_a_bare_domain_entry_is_dropped_not_repaired():
    # ADR AD-11. ``cdn.example.com`` is what a kind 24242 authorization
    # tag MUST carry and is exactly what a kind 10063 tag MUST NOT. The
    # two tags share a name and nothing else, so prefixing a scheme here
    # to "fix" the entry would be inventing data the author never
    # published, in service of harmonizing two specs that disagree on
    # purpose.
    parsed = parse_server_list(_event("cdn.example.com", "https://cdn.other.example"))
    assert parsed == ["https://cdn.other.example"]


@pytest.mark.parametrize("value", [
    "cdn.example.com",          # no scheme at all
    "//cdn.example.com",        # protocol relative
    "cdn.example.com:443",      # host and port, still no scheme
    "ftp://cdn.example.com",    # wrong scheme
    "  ",
    "",
])
def test_entries_without_a_usable_scheme_are_dropped(value):
    assert parse_server_list(_event(value)) == []


def test_order_is_preference_order():
    # bud-03.md line 11: the order is arranged most trusted first.
    servers = ["https://first.example", "https://second.example", "https://third.example"]
    assert parse_server_list(_event(*servers)) == servers


def test_duplicate_origins_collapse_to_the_first_occurrence():
    parsed = parse_server_list(_event(
        "https://cdn.example.com",
        "https://CDN.Example.com/",
        "https://cdn.example.com/path/ignored",
        "https://other.example",
    ))
    assert parsed == ["https://cdn.example.com", "https://other.example"]


def test_entry_count_is_capped():
    many = [f"https://s{i}.example" for i in range(MAX_SERVER_LIST_ENTRIES + 5)]
    parsed = parse_server_list(_event(*many))
    assert parsed == many[:MAX_SERVER_LIST_ENTRIES]


@pytest.mark.parametrize("event", [
    None,
    "not an event",
    {},
    {"tags": "not a list"},
    {"tags": [["server"], ["server", None], ["server", 7], "nope", 5]},
    {"tags": [["r", "wss://relay.example"], ["p", PUBKEY]]},
])
def test_a_malformed_event_yields_an_empty_list(event):
    assert parse_server_list(event) == []


def test_content_is_ignored():
    # bud-03.md line 13: "The .content field is not used."
    event = _event("https://cdn.example.com", content="https://sneaky.example")
    assert parse_server_list(event) == ["https://cdn.example.com"]


def test_plain_http_public_servers_are_refused():
    # A restriction on what BUD-03 permits, taken on purpose: every entry
    # here can become a retrieval target and may become an upload target.
    assert parse_server_list(_event("http://cdn.example.com")) == []


def test_loopback_http_still_works_for_local_development():
    assert parse_server_list(_event("http://localhost:3000")) == ["http://localhost:3000"]


# --------------------------------------------------------------------------- #
# build_server_list_event                                                     #
# --------------------------------------------------------------------------- #

def test_built_event_is_kind_10063_with_empty_content():
    event = build_server_list_event(["https://cdn.example.com"], PUBKEY)
    assert event["kind"] == BLOSSOM_SERVER_LIST_KIND
    assert event["content"] == ""
    assert event["pubkey"] == PUBKEY


def test_built_tags_carry_full_urls_in_order():
    servers = ["https://cdn.self.hosted", "https://cdn.satellite.earth"]
    event = build_server_list_event(servers, PUBKEY)
    assert [t for t in event["tags"] if t[0] == "server"] == [
        ["server", servers[0]],
        ["server", servers[1]],
    ]


def test_build_round_trips_through_parse():
    servers = ["https://cdn.self.hosted", "https://cdn.satellite.earth"]
    assert parse_server_list(build_server_list_event(servers, PUBKEY)) == servers


def test_build_drops_entries_it_would_refuse_to_read():
    event = build_server_list_event(
        ["cdn.example.com", "https://good.example", "https://good.example/"], PUBKEY
    )
    assert event["tags"] == [["server", "https://good.example"]]


# --------------------------------------------------------------------------- #
# ServerListCache                                                             #
# --------------------------------------------------------------------------- #

def test_cache_fetches_kind_10063_for_one_author():
    pool = FakePool()
    cache = ServerListCache(pool)
    seen: list = []
    cache.fetch(PUBKEY, ["wss://relay.example"], seen.append)

    assert pool.subs[0].filters == [
        {"kinds": [10063], "authors": [PUBKEY], "limit": 1}
    ]
    pool.deliver(BUD03_EXAMPLE)
    assert seen == [["https://cdn.self.hosted", "https://cdn.satellite.earth"]]


def test_two_concurrent_fetches_share_one_req():
    pool = FakePool()
    cache = ServerListCache(pool)
    first: list = []
    second: list = []
    cache.fetch(PUBKEY, ["wss://relay.example"], first.append)
    cache.fetch(PUBKEY, ["wss://relay.example"], second.append)

    assert len(pool.subs) == 1
    pool.deliver(BUD03_EXAMPLE)
    assert first == second == [["https://cdn.self.hosted", "https://cdn.satellite.earth"]]


def test_a_hit_is_answered_from_cache_without_a_second_req():
    pool = FakePool()
    cache = ServerListCache(pool)
    cache.fetch(PUBKEY, [], lambda _s: None)
    pool.deliver(BUD03_EXAMPLE)

    seen: list = []
    cache.fetch(PUBKEY, [], seen.append)
    assert len(pool.subs) == 1
    assert seen == [["https://cdn.self.hosted", "https://cdn.satellite.earth"]]


def test_a_hit_expires_after_the_long_ttl(monkeypatch):
    pool = FakePool()
    cache = ServerListCache(pool)
    cache.fetch(PUBKEY, [], lambda _s: None)
    pool.deliver(BUD03_EXAMPLE)

    import time as time_module
    later = time_module.time() + server_list._TTL_HIT_S + 1
    monkeypatch.setattr(server_list.time, "time", lambda: later)
    cache.fetch(PUBKEY, [], lambda _s: None)
    assert len(pool.subs) == 2


def test_an_empty_answer_is_retried_sooner_than_a_hit(monkeypatch):
    pool = FakePool()
    cache = ServerListCache(pool)
    seen: list = []
    cache.fetch(PUBKEY, [], seen.append)
    pool.deliver_nothing()
    assert seen == [[]]

    import time as time_module
    later = time_module.time() + server_list._TTL_EMPTY_S + 1
    monkeypatch.setattr(server_list.time, "time", lambda: later)
    cache.fetch(PUBKEY, [], lambda _s: None)
    assert len(pool.subs) == 2


def test_invalidate_forces_a_refetch():
    pool = FakePool()
    cache = ServerListCache(pool)
    cache.fetch(PUBKEY, [], lambda _s: None)
    pool.deliver(BUD03_EXAMPLE)
    cache.invalidate(PUBKEY)
    cache.fetch(PUBKEY, [], lambda _s: None)
    assert len(pool.subs) == 2


def test_the_cache_never_touches_disk(tmp_path, monkeypatch):
    # Whose media a user looked at is not something to leave on disk, and
    # a stale copy of somebody else's list would be invisible.
    monkeypatch.chdir(tmp_path)
    pool = FakePool()
    cache = ServerListCache(pool)
    cache.fetch(PUBKEY, [], lambda _s: None)
    pool.deliver(BUD03_EXAMPLE)
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# UserServerList: the AD-13 merge matrix                                      #
# --------------------------------------------------------------------------- #

class Ctx:
    """A policy object wired to a fake pool and a temp settings file."""

    def __init__(self, tmp_path, profile=None) -> None:
        self.pool = FakePool()
        self.settings = _settings(tmp_path)
        self.policy = UserServerList(self.pool, settings=self.settings)
        self.profile = profile if profile is not None else _profile()
        self.discovered: list = []
        self.adopted: list = []
        self.suggested: list = []
        self.policy.discovered.connect(self.discovered.append)
        self.policy.adopted.connect(self.adopted.append)
        self.policy.suggestions_changed.connect(self.suggested.append)

    def refresh_with(self, servers) -> None:
        self.policy.refresh(self.profile)
        if servers is None:
            self.pool.deliver_nothing()
        else:
            self.pool.deliver(_event(*servers))


def _profile(pubkey: str = PUBKEY, relays=()):
    class _P:
        user_pubkey = pubkey
        bunker_relays = list(relays)
    return _P()


PUBLISHED = ["https://cdn.self.hosted", "https://cdn.satellite.earth"]
CHOSEN = ["https://blossom.band", "https://nostr.download"]


def test_first_run_adopts_the_published_list(tmp_path):
    # AD-13.4: no local configuration, so the published list becomes the
    # upload configuration and the caller is told so it can be shown.
    ctx = Ctx(tmp_path)
    assert not ctx.settings.has_explicit_config

    ctx.refresh_with(PUBLISHED)

    assert ctx.adopted == [PUBLISHED]
    assert ctx.settings.configured_servers == PUBLISHED
    assert ctx.settings.config_origin == CONFIG_ORIGIN_BUD03
    assert ctx.suggested == []


def test_adoption_survives_a_restart(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.refresh_with(PUBLISHED)
    reopened = _settings(tmp_path)
    assert reopened.configured_servers == PUBLISHED
    assert reopened.config_origin == CONFIG_ORIGIN_BUD03


def test_a_local_config_that_disagrees_is_never_overwritten(tmp_path):
    # The case the whole policy exists for: the user has chosen servers
    # AND publishes a different list. The choice wins for uploads, and
    # the published list is offered rather than applied.
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)

    ctx.refresh_with(PUBLISHED)

    assert ctx.settings.configured_servers == CHOSEN
    assert ctx.settings.primary == CHOSEN[0]
    assert ctx.settings.config_origin == CONFIG_ORIGIN_USER
    assert ctx.adopted == []
    assert ctx.suggested == [PUBLISHED]


def test_a_disagreeing_list_is_still_available_for_retrieval(tmp_path):
    # AD-13.2: discovery is always usable for finding a blob again, even
    # when it may not decide where uploads go.
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)
    ctx.refresh_with(PUBLISHED)

    assert ctx.policy.discovered_servers == PUBLISHED
    assert ctx.policy.recovery_servers("a" * 64) == CHOSEN + PUBLISHED


def test_an_overlapping_list_only_suggests_what_is_missing(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers([PUBLISHED[0], "https://mine.example"])

    ctx.refresh_with(PUBLISHED)

    assert ctx.suggested == [[PUBLISHED[1]]]


def test_an_identical_list_suggests_nothing(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(PUBLISHED)

    ctx.refresh_with(PUBLISHED)

    assert ctx.suggested == [[]]
    assert ctx.adopted == []
    assert ctx.settings.configured_servers == PUBLISHED


def test_no_published_list_changes_nothing(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.refresh_with(None)

    assert ctx.discovered == [[]]
    assert ctx.adopted == []
    assert ctx.suggested == []
    assert ctx.settings.configured_servers == list(DEFAULT_BLOSSOM_SERVERS)
    assert ctx.settings.config_origin == CONFIG_ORIGIN_UNSET


def test_an_empty_answer_does_not_discard_the_last_list_seen(tmp_path):
    # A relay that does not reply and an author who deleted their list
    # look identical from here, so a timeout must not cost the user the
    # servers recovery would have tried.
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)
    ctx.refresh_with(PUBLISHED)

    ctx.policy.refresh(ctx.profile, force=True)
    ctx.pool.deliver_nothing()

    assert ctx.discovered == [PUBLISHED, []]
    assert ctx.policy.discovered_servers == PUBLISHED


def test_no_published_list_leaves_an_existing_config_alone(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)
    ctx.refresh_with(None)
    assert ctx.settings.configured_servers == CHOSEN
    assert ctx.suggested == []


def test_a_deliberate_revert_is_offered_not_re_adopted(tmp_path):
    # A user who went back to the bundled servers made a choice too. The
    # published list becomes a suggestion again, never a silent restore.
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)
    ctx.settings.reset_to_defaults()
    assert not ctx.settings.has_explicit_config

    ctx.refresh_with(PUBLISHED)

    assert ctx.adopted == []
    assert ctx.suggested == [PUBLISHED]
    assert ctx.settings.configured_servers == list(DEFAULT_BLOSSOM_SERVERS)
    assert ctx.settings.config_origin == CONFIG_ORIGIN_DEFAULT


def test_a_second_refresh_after_adoption_does_not_re_adopt(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.refresh_with(PUBLISHED)
    ctx.pool.subs.clear()

    ctx.policy.refresh(ctx.profile, force=True)
    ctx.pool.deliver(_event("https://brand.new.example"))

    assert ctx.adopted == [PUBLISHED]
    assert ctx.settings.configured_servers == PUBLISHED
    assert ctx.suggested == [["https://brand.new.example"]]


def test_a_published_list_of_only_plain_http_adopts_nothing(tmp_path):
    # Risk R4 made visible: the entries are refused by the media policy,
    # so the user is left on the bundled defaults with no suggestions.
    ctx = Ctx(tmp_path)
    ctx.refresh_with(["http://cdn.example.com", "http://other.example"])

    assert ctx.adopted == []
    assert ctx.suggested == []
    assert ctx.settings.configured_servers == list(DEFAULT_BLOSSOM_SERVERS)


def test_discovery_is_recorded_but_never_enters_the_upload_list(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)
    ctx.refresh_with(PUBLISHED)

    reopened = _settings(tmp_path)
    assert reopened.discovered_servers == PUBLISHED
    assert reopened.discovered_pubkey == PUBKEY
    assert reopened.configured_servers == CHOSEN


def test_refresh_queries_the_default_relays_plus_the_bunker_relays(tmp_path):
    ctx = Ctx(tmp_path, profile=_profile(relays=["wss://bunker.example"]))
    ctx.policy.refresh(ctx.profile)
    assert ctx.pool.relay_sets[0] == list(DEFAULT_RELAYS) + ["wss://bunker.example"]


def test_refresh_without_a_profile_pubkey_asks_nothing(tmp_path):
    ctx = Ctx(tmp_path, profile=_profile(pubkey=""))
    ctx.policy.refresh(ctx.profile)
    assert ctx.pool.subs == []


def test_recovery_servers_prefers_the_users_own_configuration(tmp_path):
    ctx = Ctx(tmp_path)
    ctx.settings.set_custom_servers(CHOSEN)
    ctx.refresh_with([CHOSEN[1]] + PUBLISHED)

    # Deduplicated, user first, published order preserved after it.
    assert ctx.policy.recovery_servers() == CHOSEN + PUBLISHED


def test_a_cache_with_no_pool_answers_empty(tmp_path):
    seen: list = []
    ServerListCache(None).fetch(PUBKEY, [], seen.append)
    assert seen == [[]]


def test_a_recorded_list_is_available_before_the_first_fetch(tmp_path):
    # Recovery has to work at startup, so the last list seen is read back
    # out of the settings file rather than waiting on a relay.
    stored = _settings(tmp_path)
    stored.set_custom_servers(CHOSEN)
    stored.record_discovered(PUBLISHED, PUBKEY)

    policy = UserServerList(FakePool(), settings=_settings(tmp_path))
    assert policy.discovered_servers == PUBLISHED
    assert policy.recovery_servers() == CHOSEN + PUBLISHED


def test_no_discovery_path_publishes_an_event(tmp_path):
    # AD-13.5: publishing our own kind 10063 is a deliberate user action
    # and there is no UI for it yet, so neither discovery nor adoption
    # may sign or send one.
    ctx = Ctx(tmp_path)
    ctx.refresh_with(PUBLISHED)
    assert ctx.pool.published == []
    assert all(sub.closed for sub in ctx.pool.subs)
