# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""build_note: the pure unsigned-event builder. The full NotePublishJob
exercises Qt + the network and is covered manually."""

from __future__ import annotations

from nostr import CLIENT_NAME
from nostr.events import verify_event
from nostr.publisher import build_note


PK = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def test_build_note_kind_and_pubkey():
    event = build_note("hello", PK)
    assert event["kind"] == 1
    assert event["pubkey"] == PK
    assert event["content"] == "hello"
    assert "sig" not in event  # unsigned — to be filled by remote signer


def test_build_note_attaches_client_tag():
    event = build_note("x", PK)
    assert ["client", CLIENT_NAME] in event["tags"]


def test_build_note_merges_extra_tags_after_client():
    extra = [["t", "blog"], ["t", "test"]]
    event = build_note("body", PK, extra_tags=extra)
    # Client tag stays first so any de-duping consumer that takes the first
    # ["client", *] tag still sees ours.
    assert event["tags"][0] == ["client", CLIENT_NAME]
    assert event["tags"][1:] == extra


def test_build_note_id_recomputes_after_signing():
    """After a signer fills in ``sig``, verify_event must accept the result."""
    from nostr import crypto, events as events_mod

    sk = bytes(31) + b"\x01"
    event = build_note("hello", PK)
    # Simulate what a signer does: compute the sig over the canonical id.
    event["sig"] = crypto.sign_schnorr(sk, bytes.fromhex(event["id"])).hex()
    # The pubkey in build_note was the BIP-340 G.x, which matches sk=1, so
    # verification should succeed end-to-end.
    assert events_mod.verify_event(event)
