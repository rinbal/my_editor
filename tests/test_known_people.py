# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""KnownPeople: roundtrip, merge semantics, local search ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from nostr.known_people import KnownPeople, Person


def _hex(b: int) -> str:
    return (bytes([b]) * 32).hex()


def test_empty_store(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    assert store.all() == []
    assert store.search("anything") == []
    assert len(store) == 0


def test_upsert_persists(tmp_path: Path):
    path = tmp_path / "k.json"
    store = KnownPeople(path)
    alice = Person(pubkey=_hex(1), display_name="Alice", nip05="alice@example.com")
    store.upsert(alice)

    reopened = KnownPeople(path)
    assert reopened.get(_hex(1)) == alice


def test_upsert_merges_filling_empty_fields(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    store.upsert(Person(pubkey=_hex(1), display_name="Alice", source="contact"))
    store.upsert(Person(pubkey=_hex(1), picture="https://example/a.png", source=""))
    merged = store.get(_hex(1))
    assert merged.display_name == "Alice"             # preserved
    assert merged.picture == "https://example/a.png"  # added
    assert merged.source == "contact"                 # preserved (new was empty)


def test_upsert_does_not_overwrite_with_empty(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    store.upsert(Person(pubkey=_hex(1), display_name="Alice", picture="x"))
    store.upsert(Person(pubkey=_hex(1), display_name=""))  # empty doesn't clear
    assert store.get(_hex(1)).display_name == "Alice"


def test_upsert_many_single_write(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    store.upsert_many([
        Person(pubkey=_hex(1), display_name="Alice"),
        Person(pubkey=_hex(2), display_name="Bob"),
        Person(pubkey=_hex(3), display_name="Carol"),
    ])
    assert len(store) == 3
    # No leftover temp files from the atomic-rename path
    assert list(tmp_path.glob(".known_people_*")) == []


def test_remove(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    store.upsert(Person(pubkey=_hex(1), display_name="Alice"))
    assert store.remove(_hex(1))
    assert not store.remove(_hex(1))


# --------------------------------------------------------------------------- #
# Search ranking                                                              #
# --------------------------------------------------------------------------- #

def _seed(store: KnownPeople):
    store.upsert_many([
        Person(pubkey=_hex(1), display_name="Alice",     nip05="alice@nostr.band"),
        Person(pubkey=_hex(2), display_name="Alistair",  nip05="ali@chest.dev"),
        Person(pubkey=_hex(3), display_name="Bob",       nip05="bob@x"),
        Person(pubkey=_hex(4), display_name="Carol",     nip05="al-pals@list"),  # 'al' only in nip05
        Person(pubkey=_hex(5), display_name="Albus",     nip05=""),
    ])


def test_search_ranks_prefix_before_contains(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    _seed(store)
    results = [p.display_name for p in store.search("al")]
    # Prefix matches first (display_name OR nip05 starts with 'al'), then contains.
    # Within a tier, sorted alphabetically by display_name.
    assert results[0:4] == ["Albus", "Alice", "Alistair", "Carol"]  # all four are prefix hits
    # No false-positive on Bob.
    assert "Bob" not in results


def test_search_case_insensitive(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    _seed(store)
    assert [p.display_name for p in store.search("ALICE")] == ["Alice"]


def test_search_empty_query_returns_recent_first(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    store.upsert(Person(pubkey=_hex(1), display_name="Old",   updated_at=100))
    store.upsert(Person(pubkey=_hex(2), display_name="Newer", updated_at=200))
    store.upsert(Person(pubkey=_hex(3), display_name="Newest", updated_at=300))
    assert [p.display_name for p in store.search("", limit=10)] == ["Newest", "Newer", "Old"]


def test_search_respects_limit(tmp_path: Path):
    store = KnownPeople(tmp_path / "k.json")
    _seed(store)
    assert len(store.search("al", limit=2)) == 2


def test_corrupt_file_tolerated(tmp_path: Path):
    path = tmp_path / "k.json"
    path.write_text("{not json", encoding="utf-8")
    store = KnownPeople(path)
    assert store.all() == []
