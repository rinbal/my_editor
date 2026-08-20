# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.blossom.settings.BlossomSettings``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostr.blossom import settings
from nostr.blossom.servers import DEFAULT_BLOSSOM_SERVERS
from nostr.blossom.settings import (
    CONFIG_ORIGIN_BUD03,
    CONFIG_ORIGIN_DEFAULT,
    CONFIG_ORIGIN_UNSET,
    CONFIG_ORIGIN_USER,
)


PUBKEY = "ab" * 32
PUBLISHED = ["https://cdn.self.hosted", "https://cdn.satellite.earth"]


def _store(tmp_path: Path) -> settings.BlossomSettings:
    return settings.BlossomSettings(path=tmp_path / "blossom_servers.json")


def test_fresh_install_uses_defaults(tmp_path):
    s = _store(tmp_path)
    assert s.configured_servers == list(DEFAULT_BLOSSOM_SERVERS)
    assert s.primary == DEFAULT_BLOSSOM_SERVERS[0]
    assert s.custom_servers == []


def test_add_server_materializes_defaults(tmp_path):
    s = _store(tmp_path)
    s.add_server("https://example.com")
    assert s.custom_servers[: len(DEFAULT_BLOSSOM_SERVERS)] == list(DEFAULT_BLOSSOM_SERVERS)
    assert s.custom_servers[-1] == "https://example.com"


def test_make_primary_moves_to_index_zero(tmp_path):
    s = _store(tmp_path)
    s.make_primary("https://nostr.download")
    assert s.primary == "https://nostr.download"


def test_remove_server_drops_entry(tmp_path):
    s = _store(tmp_path)
    s.remove_server("https://blossom.primal.net")
    assert "https://blossom.primal.net" not in s.configured_servers


def test_set_custom_servers_normalizes_and_dedupes(tmp_path):
    s = _store(tmp_path)
    persisted = s.set_custom_servers([
        "https://Blossom.Band/",
        "https://blossom.band",        # duplicate after normalization
        "  https://nostr.download  ",
        "ftp://nope.example",          # rejected scheme
        "",
    ])
    assert persisted == ["https://blossom.band", "https://nostr.download"]


def test_reset_to_defaults(tmp_path):
    s = _store(tmp_path)
    s.set_custom_servers(["https://x.example"])
    out = s.reset_to_defaults()
    assert s.custom_servers == []
    assert out == list(DEFAULT_BLOSSOM_SERVERS)


def test_persists_across_instances(tmp_path):
    path = tmp_path / "blossom_servers.json"
    a = settings.BlossomSettings(path=path)
    a.set_custom_servers(["https://blossom.band", "https://nostr.download"])
    b = settings.BlossomSettings(path=path)
    assert b.configured_servers == ["https://blossom.band", "https://nostr.download"]


def test_file_format_is_versioned(tmp_path):
    path = tmp_path / "blossom_servers.json"
    s = settings.BlossomSettings(path=path)
    s.set_custom_servers(["https://blossom.band"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["custom"] == ["https://blossom.band"]


def test_normalize_rejects_invalid_inputs():
    assert settings._normalize("") is None
    assert settings._normalize("not a url") is None
    assert settings._normalize("ftp://x.example") is None
    assert settings._normalize(None) is None  # type: ignore[arg-type]


def test_normalize_strips_path_and_lowercases_host():
    assert settings._normalize("HTTPS://Blossom.Band/foo/bar") == "https://blossom.band"


# --------------------------------------------------------------------------- #
# Scheme policy: https anywhere, http only on loopback
# --------------------------------------------------------------------------- #

def test_plain_http_server_is_rejected(tmp_path):
    # The docstring always promised http was for localhost only; the
    # scheme check did not enforce it, so a plain-http origin could be
    # configured and every upload would go out in the clear.
    s = _store(tmp_path)
    before = s.custom_servers
    assert s.add_server("http://evil.example") == before
    assert "http://evil.example" not in s.configured_servers
    assert settings._normalize("http://evil.example") is None


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:3000", "http://localhost:3000"),
    ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
    ("http://[::1]:3000", "http://[::1]:3000"),
])
def test_loopback_dev_servers_still_work(tmp_path, url, expected):
    s = _store(tmp_path)
    assert settings._normalize(url) == expected
    s.add_server(url)
    assert expected in s.configured_servers


def test_persisted_plain_http_entry_is_dropped_on_load(tmp_path):
    path = tmp_path / "blossom_servers.json"
    path.write_text(json.dumps({
        "version": 1,
        "custom": ["https://blossom.band", "http://evil.example"],
    }), encoding="utf-8")
    s = settings.BlossomSettings(path=path)
    assert s.custom_servers == ["https://blossom.band"]


def test_save_never_writes_outside_the_configured_directory(tmp_path):
    # A temp file created beside the default settings file would both
    # touch the real config directory and break os.replace across
    # filesystems.
    path = tmp_path / "nested" / "blossom_servers.json"
    s = settings.BlossomSettings(path=path)
    s.set_custom_servers(["https://blossom.band"])
    assert path.is_file()
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "blossom_servers.json"]


# --------------------------------------------------------------------------- #
# Discovered server lists: usable, never authoritative (ADR AD-13)            #
# --------------------------------------------------------------------------- #

def test_a_fresh_install_has_decided_nothing(tmp_path):
    s = _store(tmp_path)
    assert s.has_explicit_config is False
    assert s.config_origin == CONFIG_ORIGIN_UNSET
    assert s.discovered_servers == []
    assert s.discovered_pubkey == ""


def test_adopt_discovered_fills_an_empty_configuration(tmp_path):
    s = _store(tmp_path)
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == PUBLISHED
    assert s.configured_servers == PUBLISHED
    assert s.primary == PUBLISHED[0]
    assert s.config_origin == CONFIG_ORIGIN_BUD03
    assert s.has_explicit_config is True


def test_adopt_discovered_is_a_no_op_when_the_user_has_a_list(tmp_path):
    # The guard is inside the mutator on purpose: a buggy caller must not
    # be able to overwrite a user's servers (AD-13.1).
    s = _store(tmp_path)
    s.set_custom_servers(["https://mine.example"])
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == []
    assert s.configured_servers == ["https://mine.example"]
    assert s.config_origin == CONFIG_ORIGIN_USER


def test_adopt_discovered_is_a_no_op_after_reset_to_defaults(tmp_path):
    s = _store(tmp_path)
    s.reset_to_defaults()
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == []
    assert s.configured_servers == list(DEFAULT_BLOSSOM_SERVERS)
    assert s.config_origin == CONFIG_ORIGIN_DEFAULT


def test_emptying_the_custom_list_counts_as_a_deliberate_revert(tmp_path):
    s = _store(tmp_path)
    s.set_custom_servers(["https://mine.example"])
    s.set_custom_servers([])
    assert s.config_origin == CONFIG_ORIGIN_DEFAULT
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == []


def test_adopt_discovered_refuses_a_list_with_nothing_usable(tmp_path):
    s = _store(tmp_path)
    assert s.adopt_discovered(["http://public.example", "not a url"], PUBKEY) == []
    assert s.config_origin == CONFIG_ORIGIN_UNSET


def test_record_discovered_never_changes_the_upload_list(tmp_path):
    s = _store(tmp_path)
    s.set_custom_servers(["https://mine.example"])
    s.record_discovered(PUBLISHED, PUBKEY)
    assert s.configured_servers == ["https://mine.example"]
    assert s.discovered_servers == PUBLISHED
    assert s.discovered_pubkey == PUBKEY
    assert s.config_origin == CONFIG_ORIGIN_USER


def test_record_discovered_leaves_adoption_possible_later(tmp_path):
    # Recording is not deciding: a user who has configured nothing can
    # still be adopted into their published list afterwards.
    s = _store(tmp_path)
    s.record_discovered(PUBLISHED, PUBKEY)
    assert settings.BlossomSettings(path=tmp_path / "blossom_servers.json").config_origin == CONFIG_ORIGIN_UNSET
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == PUBLISHED


def test_discovery_persists_across_instances(tmp_path):
    path = tmp_path / "blossom_servers.json"
    a = settings.BlossomSettings(path=path)
    a.set_custom_servers(["https://mine.example"])
    a.record_discovered(PUBLISHED, PUBKEY)

    b = settings.BlossomSettings(path=path)
    assert b.discovered_servers == PUBLISHED
    assert b.discovered_pubkey == PUBKEY
    assert b.configured_servers == ["https://mine.example"]


def test_discovered_entries_are_normalized_and_capped(tmp_path):
    s = _store(tmp_path)
    s.record_discovered(
        ["https://A.example/", "https://a.example", "http://public.example"]
        + [f"https://s{i}.example" for i in range(12)],
        PUBKEY,
    )
    stored = s.discovered_servers
    assert stored[0] == "https://a.example"
    assert "http://public.example" not in stored
    assert len(stored) == 10


# --------------------------------------------------------------------------- #
# Old and damaged files still load (AD-10)                                    #
# --------------------------------------------------------------------------- #

def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_file_without_the_new_keys_infers_an_explicit_choice(tmp_path):
    path = tmp_path / "blossom_servers.json"
    _write(path, {"version": 1, "custom": ["https://mine.example"]})
    s = settings.BlossomSettings(path=path)
    assert s.config_origin == CONFIG_ORIGIN_USER
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == []


def test_an_old_file_with_an_empty_list_reads_as_a_revert(tmp_path):
    # Nothing writes this file except a mutation, so an empty list in an
    # existing file means the user cleared it. A discovery must not undo
    # that.
    path = tmp_path / "blossom_servers.json"
    _write(path, {"version": 1, "custom": []})
    s = settings.BlossomSettings(path=path)
    assert s.config_origin == CONFIG_ORIGIN_DEFAULT
    assert s.adopt_discovered(PUBLISHED, PUBKEY) == []


@pytest.mark.parametrize("payload", [
    {"version": 1, "custom": [], "config_origin": 7},
    {"version": 1, "custom": [], "config_origin": "wat"},
    {"version": 1, "custom": [], "discovered": "not a list"},
    {"version": 1, "custom": [], "discovered": [None, 5, {}]},
    {"version": 1, "custom": [], "discovered_pubkey": 12},
    {"version": 1, "custom": [], "discovered": None, "discovered_pubkey": None,
     "config_origin": None},
])
def test_corrupt_derived_keys_load_without_raising(tmp_path, payload):
    path = tmp_path / "blossom_servers.json"
    _write(path, payload)
    s = settings.BlossomSettings(path=path)
    assert s.configured_servers == list(DEFAULT_BLOSSOM_SERVERS)
    assert s.discovered_servers == []
    assert s.discovered_pubkey == ""


def test_the_derived_keys_are_written_beside_version_1(tmp_path):
    path = tmp_path / "blossom_servers.json"
    s = settings.BlossomSettings(path=path)
    s.adopt_discovered(PUBLISHED, PUBKEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["custom"] == PUBLISHED
    assert data["config_origin"] == CONFIG_ORIGIN_BUD03
    assert data["discovered"] == PUBLISHED
    assert data["discovered_pubkey"] == PUBKEY
