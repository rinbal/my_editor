# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.media.assets``.

Covers the three things the rest of the media layer trusts blindly: the
asset key is the only gate between a document string and a lookup, the
index survives every shape of damaged file without losing an image, and
a write either lands atomically or degrades quietly.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.blossom.hashes import hash_from_url
from nostr.media.assets import (
    ASSET_SCHEME,
    CURRENT_INDEX_VERSION,
    AssetIndex,
    AssetState,
    DocumentAsset,
    _acceptable_remote_url,
    asset_key,
    is_asset_key,
    is_sha256,
    parse_asset_key,
)


SHA = "ab" * 32
OTHER_SHA = "cd" * 32


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    # AssetIndex owns a QTimer, so the suite needs an application object.
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _index(tmp_path, name="media_assets.json") -> AssetIndex:
    return AssetIndex(path=tmp_path / name)


def _write(tmp_path, text: str, name="media_assets.json"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Asset keys
# --------------------------------------------------------------------------- #

def test_asset_key_round_trip():
    key = asset_key(SHA)
    assert key == f"{ASSET_SCHEME}:{SHA}"
    assert parse_asset_key(key) == SHA
    assert is_asset_key(key)


def test_asset_key_lowercases_the_hash():
    assert asset_key(SHA.upper()) == f"{ASSET_SCHEME}:{SHA}"


def test_parse_rejects_uppercase_hex():
    assert parse_asset_key(f"{ASSET_SCHEME}:{'AB' * 32}") is None


def test_parse_rejects_short_long_and_non_hex():
    assert parse_asset_key(f"{ASSET_SCHEME}:{'a' * 63}") is None
    assert parse_asset_key(f"{ASSET_SCHEME}:{'a' * 65}") is None
    assert parse_asset_key(f"{ASSET_SCHEME}:{'g' * 64}") is None


def test_parse_is_a_full_match_only():
    key = asset_key(SHA)
    assert parse_asset_key("x" + key) is None
    assert parse_asset_key(key + "x") is None
    assert parse_asset_key(f"  {key}") is None


def test_parse_rejects_traversal_and_non_strings():
    assert parse_asset_key(f"{ASSET_SCHEME}:../../etc/passwd") is None
    assert parse_asset_key(f"{ASSET_SCHEME}:/etc/passwd") is None
    assert parse_asset_key("") is None
    assert parse_asset_key(None) is None
    assert parse_asset_key(1234) is None
    assert not is_asset_key("https://cdn.example/a.png")


def test_is_sha256():
    assert is_sha256(SHA)
    assert not is_sha256(SHA.upper())
    assert not is_sha256("abc")
    assert not is_sha256(None)


# --------------------------------------------------------------------------- #
# Remote URL acceptance
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url, expected", [
    (f"https://cdn.example/{SHA}", True),
    (f"https://cdn.example/{SHA}.png", True),
    ("https://cdn.example/blob", True),                  # no hex run at all
    (f"https://cdn.example/x/{OTHER_SHA}", False),       # last run is not ours
    (f"https://cdn.example/{SHA}/{OTHER_SHA}.png", False),
    (f"https://cdn.example/{OTHER_SHA}/{SHA}", True),    # last run wins
    (f"http://cdn.example/{SHA}", False),                # plain http, not local
    (f"http://localhost:3000/{SHA}", True),
    (f"http://127.0.0.1:3000/{SHA}", True),
    (f"http://[::1]:3000/{SHA}", True),
    (f"https://user:pw@cdn.example/{SHA}", False),
    (f"file:///etc/{SHA}", False),
    ("data:image/png;base64,AAAA", False),
    ("https:///nohost", False),
    ("", False),
    (None, False),
])
def test_acceptable_remote_url_table(url, expected):
    assert _acceptable_remote_url(url, SHA) is expected


def test_acceptable_remote_url_ignores_case_of_the_hash_in_the_url():
    assert _acceptable_remote_url(f"https://cdn.example/{SHA.upper()}", SHA)


def test_a_tokenised_url_is_accepted():
    # A spec-compliant server may hand back a signed URL whose token is
    # itself 64 hex characters. Reading that as the blob's name reported
    # a successful upload as a failure.
    url = f"https://cdn.example/{SHA}.png?token={OTHER_SHA}"
    assert _acceptable_remote_url(url, SHA) is True


def test_a_fragment_does_not_rename_the_blob_either():
    assert _acceptable_remote_url(f"https://cdn.example/{SHA}#{OTHER_SHA}", SHA)


def test_a_same_origin_url_naming_a_different_blob_is_still_rejected():
    assert _acceptable_remote_url(f"https://cdn.example/{OTHER_SHA}", SHA) is False
    assert _acceptable_remote_url(
        f"https://cdn.example/{SHA}/x/{OTHER_SHA}?t={SHA}", SHA) is False


# The six URL shapes from specs/bud-03.md lines 45 to 51, and the hash
# the spec says each one must select.
BUD03_HASH = "b1674191a88ec5cdd733e4240a81803105dc412d6c6708d53ab94fc248f4f553"
BUD03_PUBKEY = "ec4425ff5e9446080d2f70440188e3ca5d6da8713db7bdeef73d0ed54d9093f0"
BUD03_URLS = [
    f"https://blossom.example.com/{BUD03_HASH}.pdf",
    f"https://cdn.example.com/{BUD03_HASH}",
    f"https://cdn.example.com/user/{BUD03_PUBKEY}/media/{BUD03_HASH}.pdf",
    f"https://cdn.example.com/media/user-name/documents/{BUD03_HASH}.pdf",
    f"http://download.example.com/downloads/{BUD03_HASH}",
    f"http://media.example.com/documents/b1/67/{BUD03_HASH}.pdf",
]


@pytest.mark.parametrize("url", BUD03_URLS)
def test_the_two_copies_of_the_bud03_hash_rule_agree(url):
    """The AD-12 rule lives in two files on purpose. This pins them.

    ``nostr/blossom/hashes.py`` owns the rule; ``nostr/media/assets.py``
    repeats it so the asset layer keeps its promise to import nothing.
    Feeding both the spec's own six URL shapes is what stops the copies
    drifting apart, and drift here is what silently rewrote one host's
    URL to another's.
    """
    assert hash_from_url(url) == BUD03_HASH
    # Plain http is refused by the transport half of the policy, not the
    # hash half, so all six shapes are compared over https.
    over_https = url.replace("http://", "https://", 1)
    assert _acceptable_remote_url(over_https, BUD03_HASH) is True
    assert _acceptable_remote_url(over_https, OTHER_SHA) is False


# --------------------------------------------------------------------------- #
# Index round trip
# --------------------------------------------------------------------------- #

def _full_asset() -> DocumentAsset:
    return DocumentAsset(
        sha256=SHA,
        mime="image/png",
        size=99,
        alt="a small picture",
        caption="taken last week",
        width=3,
        height=2,
        upload_state=AssetState.COMPLETE,
        remote_url=f"https://cdn.example/{SHA}.png",
        servers=["https://cdn.example", "https://mirror.example"],
        created_at=1700000000,
        updated_at=1700000123,
        attempts=2,
        failure_code="",
        failure_reason="",
        local_path="/home/someone/.config/my_editor/blossom_cache/" + SHA,
    )


def test_round_trip_preserves_every_field_but_local_path(tmp_path):
    index = _index(tmp_path)
    index.put(_full_asset())
    assert index.save() is True

    reloaded = _index(tmp_path).get(SHA)
    original = _full_asset()
    assert reloaded is not None
    for name in ("sha256", "mime", "size", "alt", "caption", "width", "height",
                 "upload_state", "remote_url", "servers", "created_at",
                 "updated_at", "attempts", "failure_code", "failure_reason"):
        assert getattr(reloaded, name) == getattr(original, name), name
    assert reloaded.local_path == ""
    assert reloaded.key == asset_key(SHA)
    assert reloaded.is_uploaded


def test_local_path_never_reaches_the_file(tmp_path):
    index = _index(tmp_path)
    index.put(_full_asset())
    index.save()
    text = (tmp_path / "media_assets.json").read_text(encoding="utf-8")
    assert "local_path" not in text
    assert "blossom_cache" not in text
    payload = json.loads(text)
    assert payload["version"] == CURRENT_INDEX_VERSION
    assert isinstance(payload["assets"], list)


def test_save_leaves_no_temp_file_behind(tmp_path):
    index = _index(tmp_path)
    index.put(_full_asset())
    index.save()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "media_assets.json"]
    assert leftovers == []


def test_flush_writes_pending_changes(tmp_path):
    index = _index(tmp_path)
    index.put(_full_asset())
    index.flush()
    assert (tmp_path / "media_assets.json").is_file()
    # A second flush with nothing pending is a harmless no-op.
    index.flush()
    assert _index(tmp_path).get(SHA) is not None


def test_index_creates_its_directory_lazily(tmp_path):
    nested = tmp_path / "config" / "my_editor"
    index = AssetIndex(path=nested / "media_assets.json")
    assert not nested.exists()
    index.put(_full_asset())
    assert index.save() is True
    assert (nested / "media_assets.json").is_file()


def test_index_membership_and_length(tmp_path):
    index = _index(tmp_path)
    assert SHA not in index
    index.put(_full_asset())
    assert SHA in index
    assert SHA.upper() in index
    assert len(index) == 1
    assert [a.sha256 for a in index.values()] == [SHA]


# --------------------------------------------------------------------------- #
# Failed writes degrade, never raise
# --------------------------------------------------------------------------- #

def test_save_failure_degrades_and_returns_false(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.put(_full_asset())

    def boom(src, dst):
        raise OSError("no room at the inn")

    monkeypatch.setattr(os, "replace", boom)
    assert index.save() is False
    assert index.degraded is True
    assert not (tmp_path / "media_assets.json").exists()
    assert list(tmp_path.iterdir()) == []          # the temp file was cleaned up


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_save_into_an_unwritable_directory_returns_false(tmp_path):
    if os.getuid() == 0:
        pytest.skip("root ignores directory permissions")
    locked = tmp_path / "locked"
    locked.mkdir()
    # The index directory itself cannot be created: the save has to
    # degrade to an in-memory session instead of raising at an insert.
    index = AssetIndex(path=locked / "my_editor" / "media_assets.json")
    index.put(_full_asset())
    os.chmod(locked, 0o500)
    try:
        assert index.save() is False
        assert index.degraded is True
    finally:
        os.chmod(locked, 0o700)


def test_in_memory_state_survives_a_failed_save(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.put(_full_asset())
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError()))
    index.save()
    assert index.get(SHA) is not None


# --------------------------------------------------------------------------- #
# Damaged and missing files
# --------------------------------------------------------------------------- #

def test_missing_file_is_an_empty_index(tmp_path):
    index = _index(tmp_path)
    assert index.values() == []
    assert index.degraded is False
    assert index.read_only is False


def test_garbage_json_starts_empty_and_keeps_the_file(tmp_path):
    path = _write(tmp_path, "{not json at all")
    before = path.read_bytes()
    index = _index(tmp_path)
    assert index.values() == []
    assert index.degraded is True
    assert path.read_bytes() == before


def test_non_dict_top_level_starts_empty(tmp_path):
    _write(tmp_path, json.dumps(["nope"]))
    index = _index(tmp_path)
    assert index.values() == []
    assert index.degraded is True


def test_non_list_assets_starts_empty(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": {"a": 1}}))
    index = _index(tmp_path)
    assert index.values() == []
    assert index.degraded is True


def test_unreadable_file_starts_empty(tmp_path, monkeypatch):
    _write(tmp_path, json.dumps({"version": 1, "assets": []}))

    def boom(*args, **kwargs):
        raise OSError("disk is sulking")

    monkeypatch.setattr("pathlib.Path.open", boom)
    index = _index(tmp_path)
    assert index.values() == []
    assert index.degraded is True


def test_bad_records_are_dropped_and_good_ones_kept(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [
        "not a dict",
        {"sha256": "too short"},
        {"sha256": SHA.upper()},          # normalised, not dropped
        {"sha256": OTHER_SHA, "mime": "image/gif"},
    ]}))
    index = _index(tmp_path)
    assert sorted(a.sha256 for a in index.values()) == sorted([SHA, OTHER_SHA])


def test_bad_field_types_fall_back_to_defaults(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [{
        "sha256": SHA,
        "mime": 42,
        "size": "big",
        "alt": None,
        "width": -5,
        "height": True,
        "servers": "https://cdn.example",
        "attempts": 1.5,
        "failure_reason": {"nested": "junk"},
    }]}))
    asset = _index(tmp_path).get(SHA)
    assert asset.mime == "application/octet-stream"
    assert asset.size == 0
    assert asset.alt == ""
    assert asset.width == 0
    assert asset.height == 0
    assert asset.servers == []
    assert asset.attempts == 0
    assert asset.failure_reason == ""


def test_unknown_state_degrades_to_local(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [
        {"sha256": SHA, "upload_state": "teleporting"},
    ]}))
    assert _index(tmp_path).get(SHA).upload_state is AssetState.LOCAL


def test_in_flight_state_degrades_to_local(tmp_path):
    # No upload job survives a restart, so a record saved mid-upload must
    # come back requestable rather than stuck.
    _write(tmp_path, json.dumps({"version": 1, "assets": [
        {"sha256": SHA, "upload_state": "uploading"},
    ]}))
    assert _index(tmp_path).get(SHA).upload_state is AssetState.LOCAL


def test_mismatched_remote_url_is_cleared_and_state_degrades(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [{
        "sha256": SHA,
        "upload_state": "complete",
        "remote_url": f"https://cdn.example/{OTHER_SHA}",
    }]}))
    asset = _index(tmp_path).get(SHA)
    assert asset.remote_url == ""
    assert asset.upload_state is AssetState.LOCAL
    assert asset.is_uploaded is False


def test_hostile_remote_url_is_cleared(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [{
        "sha256": SHA,
        "upload_state": "complete",
        "remote_url": "file:///etc/passwd",
    }]}))
    asset = _index(tmp_path).get(SHA)
    assert asset.remote_url == ""
    assert asset.upload_state is AssetState.LOCAL


def test_complete_without_a_url_degrades(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [
        {"sha256": SHA, "upload_state": "complete"},
    ]}))
    assert _index(tmp_path).get(SHA).upload_state is AssetState.LOCAL


def test_missing_version_is_read_best_effort(tmp_path):
    _write(tmp_path, json.dumps({"assets": [{"sha256": SHA, "alt": "kept"}]}))
    index = _index(tmp_path)
    assert index.get(SHA).alt == "kept"
    assert index.read_only is False


def test_duplicate_records_dedupe_last_wins(tmp_path):
    _write(tmp_path, json.dumps({"version": 1, "assets": [
        {"sha256": SHA, "alt": "first"},
        {"sha256": SHA, "alt": "second"},
    ]}))
    index = _index(tmp_path)
    assert len(index) == 1
    assert index.get(SHA).alt == "second"


# --------------------------------------------------------------------------- #
# Forward compatibility
# --------------------------------------------------------------------------- #

def test_newer_version_is_read_only_and_never_clobbered(tmp_path):
    path = _write(tmp_path, json.dumps({
        "version": CURRENT_INDEX_VERSION + 1,
        "assets": [{"sha256": SHA, "alt": "written by a newer build"}],
    }))
    before = path.read_bytes()

    index = _index(tmp_path)
    assert index.values() == []
    assert index.read_only is True

    index.put(_full_asset())
    assert index.save() is False
    assert index.flush() is None
    assert path.read_bytes() == before
