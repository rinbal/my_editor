"""Unit tests for ``nostr.blossom.settings.BlossomSettings``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostr.blossom import settings
from nostr.blossom.servers import DEFAULT_BLOSSOM_SERVERS


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
