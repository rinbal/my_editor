# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""select_draft_publish_relays — cross-device sync guarantee.

The contract: a draft published from device A on profile P must land on
at least one relay that device B reading the same profile P will see.
That's only ensured if the publish set is a superset of the read set's
sources. These tests verify that invariant against representative
asymmetric NIP-65 configurations.
"""

from __future__ import annotations

from nostr.outbox import (
    RELAY_CAP,
    RelayList,
    select_draft_publish_relays,
)


def _normalize(url: str) -> str:
    """Mirror the dedup key used internally so tests can compare sets."""
    return url.rstrip("/").lower()


# --------------------------------------------------------------------------- #
# Cross-device overlap                                                        #
# --------------------------------------------------------------------------- #

def test_asymmetric_read_and_write_overlap():
    # Common real-world shape: paid read-only relay + free write relays.
    rl = RelayList(
        write=["wss://paid-write.example/", "wss://shared.example/"],
        read=["wss://paid-read.example/"],
    )
    bunker = ["wss://bunker.example/"]

    publish_set = {_normalize(r) for r in select_draft_publish_relays(rl, bunker_relays=bunker)}
    # The reader's source list, hand-built to match _select_read_relays preference:
    reader_sources = {_normalize(r) for r in rl.read} | {_normalize(r) for r in rl.write} | {_normalize(r) for r in bunker}
    overlap = publish_set & reader_sources
    assert overlap, f"publish set has no overlap with reader sources: pub={publish_set} read={reader_sources}"


def test_overlap_when_only_read_relays_published():
    # A user who only published their read set (NIP-65 oddity but legal).
    rl = RelayList(write=[], read=["wss://only-read.example/"])
    publish = select_draft_publish_relays(rl, bunker_relays=())
    assert any("only-read.example" in r for r in publish)


def test_overlap_when_only_bunker_relays_known():
    # Brand-new profile: no published NIP-65 list yet. The bunker URI's
    # relays are the only user-specific data we have — they must land in
    # both publish and read sets.
    publish = select_draft_publish_relays(RelayList(), bunker_relays=["wss://bunker.example/"])
    assert any("bunker.example" in r for r in publish)


# --------------------------------------------------------------------------- #
# Dedup + cap                                                                 #
# --------------------------------------------------------------------------- #

def test_dedupes_case_and_trailing_slash():
    rl = RelayList(
        write=["wss://X.Example/", "wss://x.example"],
        read=["WSS://x.example/"],
    )
    result = select_draft_publish_relays(rl, bunker_relays=())
    # All three inputs normalize to the same key — only one survives.
    # Output preserves the original casing of the first-seen variant,
    # so compare case-insensitively.
    assert sum(1 for r in result if "x.example" in r.lower()) == 1


def test_respects_cap():
    many_writes = [f"wss://w{i}.example" for i in range(50)]
    rl = RelayList(write=many_writes, read=[])
    result = select_draft_publish_relays(rl, bunker_relays=(), cap=5)
    assert len(result) == 5


def test_default_cap_is_module_constant():
    rl = RelayList(
        write=[f"wss://w{i}.example" for i in range(50)],
        read=[],
    )
    result = select_draft_publish_relays(rl, bunker_relays=())
    assert len(result) == RELAY_CAP


# --------------------------------------------------------------------------- #
# Order semantics                                                             #
# --------------------------------------------------------------------------- #

def test_write_relays_appear_before_read():
    # Write before read matches how the existing publisher pipeline
    # thinks about delivery priority — write set first, then read as a
    # fallback for sync coverage.
    rl = RelayList(write=["wss://w.example"], read=["wss://r.example"])
    result = select_draft_publish_relays(rl, bunker_relays=())
    assert result.index("wss://w.example") < result.index("wss://r.example")


def test_base_falls_in_last():
    # Curated defaults are a backstop, not the primary destination:
    # they appear at the end so user-chosen relays are always tried first.
    rl = RelayList(write=["wss://user.example"], read=[])
    result = select_draft_publish_relays(
        rl,
        bunker_relays=(),
        base=("wss://default-a.example", "wss://default-b.example"),
    )
    assert result[0] == "wss://user.example"
    assert result[1] == "wss://default-a.example"


def test_filters_empty_and_whitespace_urls():
    rl = RelayList(write=["", "   ", "wss://ok.example"], read=[])
    result = select_draft_publish_relays(rl, bunker_relays=())
    assert "wss://ok.example" in result
    assert "" not in result and "   " not in result
