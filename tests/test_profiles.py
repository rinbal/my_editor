# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profile store: round-trip, defaults, atomic write, file permissions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from nostr.profiles import Profile, ProfileStore


def _make_profile(pk_byte: int = 0x01, name: str = "alice") -> Profile:
    pk = bytes([pk_byte]) * 32
    bunker = bytes([pk_byte ^ 0xFF]) * 32
    local = bytes([pk_byte ^ 0x80]) * 32
    return Profile(
        user_pubkey=pk.hex(),
        bunker_pubkey=bunker.hex(),
        bunker_relays=["wss://relay.example/"],
        local_secret_hex=local.hex(),
        display_name=name,
    )


def test_empty_store_returns_no_default(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.json")
    assert store.list() == []
    assert store.default() is None
    assert len(store) == 0


def test_upsert_persists_and_first_becomes_default(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    store = ProfileStore(path)

    alice = _make_profile(0x01, "alice")
    store.upsert(alice)

    # First profile auto-becomes default.
    assert store.default() == alice

    # New store from the same file sees the persisted data.
    reopened = ProfileStore(path)
    assert reopened.get(alice.user_pubkey) == alice
    assert reopened.default() == alice


def test_upsert_overwrites_by_pubkey(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.json")
    a1 = _make_profile(0x01, "alice")
    a2 = _make_profile(0x01, "alice (renamed)")  # same pubkey
    store.upsert(a1)
    store.upsert(a2)
    assert len(store) == 1
    assert store.get(a1.user_pubkey).display_name == "alice (renamed)"


def test_set_default_and_remove(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.json")
    a = _make_profile(0x01, "alice")
    b = _make_profile(0x02, "bob")
    store.upsert(a)
    store.upsert(b)
    assert store.default() == a

    store.set_default(b.user_pubkey)
    assert store.default() == b

    # Removing the default falls back to another profile.
    store.remove(b.user_pubkey)
    assert store.default() == a

    # Removing the last profile clears the default.
    store.remove(a.user_pubkey)
    assert store.default() is None


def test_set_default_rejects_unknown_pubkey(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.json")
    with pytest.raises(KeyError):
        store.set_default("00" * 32)


def test_file_permissions_are_owner_only(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only permission check")
    path = tmp_path / "p.json"
    store = ProfileStore(path)
    store.upsert(_make_profile(0x01))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_save_is_atomic_no_stale_tmp_files_left(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "p.json")
    store.upsert(_make_profile(0x01))
    store.upsert(_make_profile(0x02))
    leftover = list(tmp_path.glob(".nostr_profiles_*"))
    assert leftover == [], f"atomic rename leaked temp files: {leftover}"


def test_corrupt_file_is_tolerated(tmp_path: Path) -> None:
    """A garbled profiles file should not crash the editor on launch — it
    should be treated as empty (and overwritten on the next save)."""
    path = tmp_path / "p.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = ProfileStore(path)
    assert store.list() == []
    # Subsequent writes succeed and replace the corrupt content.
    store.upsert(_make_profile(0x01))
    reopened = ProfileStore(path)
    assert len(reopened) == 1


def test_load_skips_unknown_field_entries(tmp_path: Path) -> None:
    """Forward-compatibility: a profile entry that has unexpected keys
    (e.g. from a newer version) is skipped rather than raising."""
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "default": None,
                "profiles": [
                    {  # missing required fields
                        "user_pubkey": "ab" * 32,
                    },
                    {
                        "user_pubkey": "cd" * 32,
                        "bunker_pubkey": "ef" * 32,
                        "bunker_relays": ["wss://r/"],
                        "local_secret_hex": "12" * 32,
                        "display_name": "valid",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    store = ProfileStore(path)
    assert len(store) == 1
    assert store.list()[0].display_name == "valid"
