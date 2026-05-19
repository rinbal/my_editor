"""Unit tests for the Blossom auth event helpers."""

from __future__ import annotations

import base64
import json
import time

import pytest

from nostr.blossom import auth


def test_build_event_has_kind_24242():
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64, server="https://blossom.band")
    assert e["kind"] == 24242


def test_required_tags_present():
    e = auth.build_blossom_auth_event("upload", file_hash="A" * 64, server="https://blossom.band")
    tag_keys = {t[0] for t in e["tags"]}
    assert {"t", "expiration", "x", "server"}.issubset(tag_keys)


def test_x_tag_is_lowercase_hash():
    """STANDUP normalizes hex to lowercase — same here so the server's
    computed hash matches the auth event's claim."""
    e = auth.build_blossom_auth_event("upload", file_hash="A" * 64, server="https://x")
    x_tag = [t for t in e["tags"] if t[0] == "x"][0]
    assert x_tag[1] == "a" * 64


def test_list_action_omits_x_tag():
    e = auth.build_blossom_auth_event("list", server="https://blossom.band")
    tag_keys = {t[0] for t in e["tags"]}
    assert "x" not in tag_keys


def test_default_expiration_is_five_minutes_ahead():
    before = int(time.time())
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64)
    after = int(time.time())
    exp = int([t for t in e["tags"] if t[0] == "expiration"][0][1])
    # Five minute window ± wall-clock jitter.
    assert before + 295 <= exp <= after + 305


def test_explicit_expiration_passes_through():
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64, expiration=1_700_000_000)
    exp = [t for t in e["tags"] if t[0] == "expiration"][0][1]
    assert exp == "1700000000"


def test_to_auth_header_round_trip():
    signed = {
        "kind": 24242,
        "pubkey": "a" * 64,
        "created_at": 1_700_000_000,
        "tags": [["t", "upload"]],
        "content": "Authorize upload",
        "id": "b" * 64,
        "sig": "c" * 128,
    }
    header = auth.to_auth_header(signed)
    assert header.startswith("Nostr ")
    decoded = json.loads(base64.b64decode(header[len("Nostr "):]).decode("utf-8"))
    assert decoded == signed


def test_pubkey_hex_normalized_lowercase():
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64, pubkey_hex="DEADBEEF" * 8)
    assert e["pubkey"] == "deadbeef" * 8
