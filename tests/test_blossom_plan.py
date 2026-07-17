# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Blossom upload planner."""

from __future__ import annotations

import pytest

from nostr.blossom import plan
from nostr.blossom.servers import DEFAULT_BLOSSOM_SERVERS


_MiB = 1024 * 1024


def test_small_file_fits_everywhere():
    p = plan.plan_upload(5 * _MiB, list(DEFAULT_BLOSSOM_SERVERS))
    assert p.primary == DEFAULT_BLOSSOM_SERVERS[0]
    assert p.eligible == list(DEFAULT_BLOSSOM_SERVERS)
    assert p.skipped == []
    assert p.rerouted is False


def test_file_too_large_for_band_reroutes_to_next_server():
    """blossom.band has a 20 MiB free cap; nostr.download accepts 100 MiB.
    A 50 MiB upload with band as primary must skip band and route to
    nostr.download, marking the upload as rerouted."""
    servers = [
        "https://blossom.band",      # 20 MiB free
        "https://nostr.download",    # 100 MiB free
        "https://blossom.primal.net",
    ]
    p = plan.plan_upload(50 * _MiB, servers)
    assert p.primary == "https://nostr.download"
    assert p.rerouted is True
    assert any(s.server == "https://blossom.band" for s in p.skipped)


def test_no_eligible_server_returns_no_primary():
    p = plan.plan_upload(500 * _MiB, list(DEFAULT_BLOSSOM_SERVERS))
    assert p.primary is None
    assert p.eligible == []


def test_unknown_server_uses_fallback_cap():
    """Custom URLs synthesize an unpublished record. The fallback cap
    matches the global ceiling so the planner doesn't accept files that
    couldn't be uploaded anyway."""
    info = plan.get_server_info("https://my-private.example")
    assert info.confidence == "unpublished"
    # Just at the fallback cap is fine; one byte over is not.
    fits = plan.plan_upload(plan.BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK, ["https://my-private.example"])
    assert fits.primary == "https://my-private.example"
    over = plan.plan_upload(plan.BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK + 1, ["https://my-private.example"])
    assert over.primary is None


def test_empty_servers_returns_empty_plan():
    p = plan.plan_upload(1024, [])
    assert p.primary is None
    assert p.eligible == []
    assert p.rerouted is False


def test_zero_size_returns_empty_plan():
    p = plan.plan_upload(0, list(DEFAULT_BLOSSOM_SERVERS))
    assert p.primary is None
    assert p.eligible == []


def test_clamp_to_app_limit_preserves_value_under_ceiling():
    val, clamped = plan.clamp_to_app_limit(10 * _MiB)
    assert val == 10 * _MiB
    assert clamped is False


def test_clamp_to_app_limit_caps_oversize():
    val, clamped = plan.clamp_to_app_limit(5 * 1024 * _MiB)  # 5 GiB
    assert val == plan.BLOSSOM_MAX_FILE_SIZE
    assert clamped is True
