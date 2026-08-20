# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
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
    """STANDUP normalizes hex to lowercase, same here so the server's
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


def _decode_token(header: str) -> dict:
    """Decode a header body that carries no padding of its own."""
    body = header[len("Nostr "):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


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
    assert _decode_token(header) == signed


def test_auth_header_is_base64url_without_padding():
    """BUD-11: "MUST be encoded as Base64 URL-safe without padding".

    The content is chosen so the two alphabets genuinely differ: this
    payload yields both a ``+`` and a ``/`` under the standard alphabet,
    plus two characters of padding, so a revert to ``b64encode`` cannot
    pass this test by coincidence.
    """
    signed = {"kind": 24242, "content": "~?>~~?>", "tags": [["t", "upload"]]}
    header = auth.to_auth_header(signed)
    body = header[len("Nostr "):]
    standard = base64.b64encode(
        json.dumps(signed, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    assert "+" in standard and "/" in standard and standard.endswith("=")

    assert "=" not in body
    assert "+" not in body
    assert "/" not in body
    assert "-" in body
    assert "_" in body
    assert _decode_token(header) == signed


def test_header_survives_non_ascii_content():
    signed = {"kind": 24242, "content": "Autorisér upload", "tags": []}
    assert _decode_token(auth.to_auth_header(signed)) == signed


def test_pubkey_hex_normalized_lowercase():
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64, pubkey_hex="DEADBEEF" * 8)
    assert e["pubkey"] == "deadbeef" * 8


# --------------------------------------------------------------------------- #
# The BUD-11 ``server`` tag
# --------------------------------------------------------------------------- #

def _server_tag(**kwargs):
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64, **kwargs)
    values = [t[1] for t in e["tags"] if t[0] == "server"]
    return values[0] if values else None


@pytest.mark.parametrize("value", [
    "https://CDN.Example.com/",
    "https://cdn.example.com",
    "https://cdn.example.com:8443/upload",
    "cdn.example.com",
    "CDN.Example.com",
])
def test_server_tag_is_always_the_bare_lowercase_domain(value):
    assert _server_tag(server=value) == "cdn.example.com"


def test_server_tag_is_idempotent():
    once = auth.auth_server_domain("https://cdn.example.com:8443/upload")
    assert auth.auth_server_domain(once) == once


def test_server_tag_omitted_when_no_host_can_be_read():
    assert _server_tag(server="not a url") is None
    assert auth.auth_server_domain("") == ""


def test_loopback_and_ip_literals_still_scope_the_token():
    """An unscoped token is the worse failure (BUD-11 security notes),
    so an IP literal is emitted rather than dropped."""
    assert _server_tag(server="http://127.0.0.1:3000") == "127.0.0.1"
    assert _server_tag(server="http://[::1]:3000") == "::1"
    assert auth.auth_server_domain("[::1]:3000") == "::1"


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #

def test_created_at_is_in_the_past():
    """BUD-11 validation rule 2. A server whose clock trails ours reads
    an exactly-now timestamp as the future and answers 401."""
    now = int(time.time())
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64)
    assert e["created_at"] <= now
    assert e["created_at"] >= now - 120


def test_expiration_is_after_created_at():
    e = auth.build_blossom_auth_event("upload", file_hash="a" * 64)
    exp = int([t for t in e["tags"] if t[0] == "expiration"][0][1])
    assert exp > e["created_at"]
