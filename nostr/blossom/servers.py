"""Default Blossom server registry.

Ported verbatim from STANDUP so the two clients agree on which servers
are first-class, what each one's published per-file cap is, and which
servers are surfaced as one-click recommendations vs. only available via
manual entry.

Sizes are in bytes — the upload path never has to convert.
"""

from __future__ import annotations

from types import MappingProxyType


_KiB = 1024
_MiB = 1024 * _KiB


# Global ceiling. The largest file the app will accept regardless of the
# configured primary's individual cap; acts as a hard sanity bound and
# the upper bound for the per-upload planner.
BLOSSOM_MAX_FILE_SIZE = 100 * _MiB

# Used by ``plan_upload`` when the configured server has no published
# per-file limit. Best-effort: we still try the upload, but cap the file
# at this size up front so users can't push a 4 GB video to a black box.
BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK = 100 * _MiB


# Per-server metadata keyed by hostname (not origin) so trailing slashes
# and protocol variations don't break lookup. Always read via
# ``plan.get_server_info()`` — never index this map directly from
# feature code.
#
# Field semantics (matches STANDUP):
#   free          — usable without payment for the typical workload
#   paid          — operator runs a paid tier (informational only)
#   requires_auth — server demands a signed kind 24242 event (BUD-02)
#   free_max_file — largest single file the free tier accepts, in bytes;
#                   None when not published
#   paid_max_file — same, for the paid tier; None when not applicable
#   confidence    — 'documented' | 'partial' | 'unpublished'; drives the
#                   indicator dot in Settings and the warning copy in the
#                   upload hint
#   notes         — short key suffix for an operator-specific note
BLOSSOM_SERVER_INFO = MappingProxyType({
    "blossom.band": {
        "free": True,
        "paid": True,
        "requires_auth": True,
        "free_max_file": 20 * _MiB,
        "paid_max_file": 100 * _MiB,
        "confidence": "documented",
        "notes": "band",
    },
    "blossom.nostr.build": {
        "free": True,
        "paid": True,
        "requires_auth": True,
        "free_max_file": 20 * _MiB,
        "paid_max_file": 100 * _MiB,
        "confidence": "documented",
        "notes": "nostrBuild",
    },
    "nostr.download": {
        "free": True,
        "paid": False,
        "requires_auth": True,
        "free_max_file": 100 * _MiB,
        "paid_max_file": None,
        "confidence": "partial",
        "notes": "nostrDownload",
    },
    "blossom.primal.net": {
        "free": True,
        "paid": False,
        "requires_auth": True,
        "free_max_file": None,
        "paid_max_file": None,
        "confidence": "unpublished",
        "notes": "primal",
    },
    "cdn.nostrcheck.me": {
        "free": True,
        "paid": False,
        "requires_auth": True,
        "free_max_file": 100 * _MiB,
        "paid_max_file": None,
        "confidence": "partial",
        "notes": "nostrcheck",
    },
    "cdn.satellite.earth": {
        "free": False,
        "paid": True,
        "requires_auth": True,
        "free_max_file": None,
        "paid_max_file": 5 * 1024 * _MiB,  # 5 GiB
        "confidence": "documented",
        "notes": "satellite",
    },
})


# Out-of-the-box server set. Order matters: index 0 is the primary, the
# rest are mirror targets.
#
# ``blossom.band`` is intentionally NOT in this list (was removed
# 2026-05-19) because the operator went unresponsive: uploads succeeded
# but deletes returned 5xx errors consistently. Metadata is preserved
# in BLOSSOM_SERVER_INFO so users who already have files there get
# sensible sizing info, and power users can re-add it via Settings if
# the operator recovers.
DEFAULT_BLOSSOM_SERVERS = (
    "https://nostr.download",
    "https://blossom.primal.net",
)


# Recommended add-ons surfaced in Settings → Blossom. Single-click add,
# in priority order.
#
# ``cdn.satellite.earth`` is intentionally NOT here even though the
# metadata table knows about it: paid (Lightning prepayment), and the
# app has no LN top-up flow, so recommending it would point users at a
# server they cannot functionally use. Power users can still add it by
# pasting the URL.
RECOMMENDED_ADDON_SERVERS = (
    "https://blossom.nostr.build",
    "https://cdn.nostrcheck.me",
)
