"""Pure upload planner: pre-flight a file size against the configured servers.

Single entry point — ``plan_upload(file_size, servers)`` — decides which
servers can accept the file, which can't, and whether the configured
primary lost its slot (reroute). The same planner output drives the
upload dispatch, the toast copy, and the per-row UI hint, so there's no
duplicated size-check logic anywhere.

Mirrors STANDUP's ``utils/blossomPlan.js`` exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence
from urllib.parse import urlparse

from .servers import (
    BLOSSOM_MAX_FILE_SIZE,
    BLOSSOM_SERVER_INFO,
    BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK,
)


@dataclass(frozen=True)
class ServerInfo:
    """Metadata for one Blossom server. Always non-null — unknown servers
    get a synthesized ``unpublished`` record so callers can rely on the shape."""

    free: bool
    paid: bool
    requires_auth: bool
    free_max_file: Optional[int]    # bytes, or None when unpublished
    paid_max_file: Optional[int]    # bytes, or None when unpublished
    confidence: str                  # 'documented' | 'partial' | 'unpublished'
    notes: Optional[str]


@dataclass(frozen=True)
class SkippedServer:
    server: str
    reason: str   # 'fileTooLarge' for now; future reasons added here


@dataclass(frozen=True)
class UploadPlan:
    """Result of pre-flighting a file against the configured server list."""

    eligible: List[str]
    skipped: List[SkippedServer]
    rerouted: bool
    primary: Optional[str]


def _host_of(server_url: str) -> Optional[str]:
    """Lowercase hostname of a server URL, or None on parse failure."""
    try:
        return (urlparse(server_url).hostname or "").lower() or None
    except (ValueError, AttributeError):
        return None


def get_server_info(server_url: str) -> ServerInfo:
    """Return curated metadata for a known server, or a synthesized
    ``unpublished`` record for any custom URL. Never returns None."""
    host = _host_of(server_url)
    raw = BLOSSOM_SERVER_INFO.get(host) if host else None
    if raw is not None:
        return ServerInfo(
            free=bool(raw.get("free", True)),
            paid=bool(raw.get("paid", False)),
            requires_auth=bool(raw.get("requires_auth", True)),
            free_max_file=raw.get("free_max_file"),
            paid_max_file=raw.get("paid_max_file"),
            confidence=str(raw.get("confidence", "unpublished")),
            notes=raw.get("notes"),
        )
    # Unknown / user-added server: assume free + signed-event required
    # (true for any spec-compliant Blossom server), no published cap.
    return ServerInfo(
        free=True,
        paid=False,
        requires_auth=True,
        free_max_file=None,
        paid_max_file=None,
        confidence="unpublished",
        notes=None,
    )


def get_effective_max_file(server_url: str) -> int:
    """Largest single-file size, in bytes, that ``server_url`` will accept
    on its free tier. ``None`` published-cap → ``BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK``.

    Predictable upper bound beats letting users push arbitrarily large
    files into a server that may reject them silently after a slow
    transfer.
    """
    info = get_server_info(server_url)
    if info.free_max_file is None:
        return BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK
    return info.free_max_file


def plan_upload(file_size: int, servers: Sequence[str]) -> UploadPlan:
    """Decide which of ``servers`` can accept a file of ``file_size`` bytes.

    Behaviour matches ``utils/blossomPlan.js``:
      - eligible:  servers that accept the file, in the original order
                   (eligible[0] is the actual upload primary)
      - skipped:   servers that can't, with a reason
      - rerouted:  True when the configured primary (servers[0]) lost
                   its slot because the file was too large *and* at
                   least one other server can take it
      - primary:   convenience accessor for eligible[0] or None

    Pure: never mutates inputs, never reads anything outside the frozen
    metadata table.
    """
    server_list = list(servers)
    if file_size <= 0 or not server_list:
        return UploadPlan(eligible=[], skipped=[], rerouted=False, primary=None)

    eligible: List[str] = []
    skipped: List[SkippedServer] = []
    for server in server_list:
        if file_size > get_effective_max_file(server):
            skipped.append(SkippedServer(server=server, reason="fileTooLarge"))
            continue
        eligible.append(server)

    configured_primary = server_list[0]
    rerouted = bool(eligible) and configured_primary not in eligible

    return UploadPlan(
        eligible=eligible,
        skipped=skipped,
        rerouted=rerouted,
        primary=eligible[0] if eligible else None,
    )


def clamp_to_app_limit(byte_count: Optional[int]) -> tuple[Optional[int], bool]:
    """Clamp a server's documented cap to the app's actual upload ceiling.

    Returns ``(value, clamped)``. The UI must not promise a number the
    upload code can't honour (e.g. satellite.earth's metadata-true
    5 GiB paid tier vs. the 100 MiB global cap).
    """
    if not isinstance(byte_count, int):
        return None, False
    if byte_count > BLOSSOM_MAX_FILE_SIZE:
        return BLOSSOM_MAX_FILE_SIZE, True
    return byte_count, False
