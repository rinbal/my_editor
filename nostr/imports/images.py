# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image rehosting for imported drafts (Blossom BUD-04 mirror).

A feed body's images live on the source blog's server; once imported,
the draft would depend on that origin staying alive and permissive.
Mirroring copies each image to the user's Blossom server: the *server*
fetches the source URL itself (BUD-04 ``PUT /mirror``), so image-origin
access restrictions never matter, and the markdown is rewritten to the
mirrored URL.

Split of responsibilities:

- :func:`scan_markdown_images` / :func:`scan_html_images`: pure scans
  used by the pipeline (markdown, at import time) and the image-review
  dialog (HTML, at preview time).
- :func:`rehost_images`: the sequential mirror loop over unique image
  URLs with per-image progress, a user-curated skip set, and markdown
  rewriting. The actual mirror transport is an injected callable so the
  loop is testable without network or signer.
- :func:`blossom_mirror`: the production transport: build a BUD
  auth event (kind 24242, ``t=upload``), sign it through the bunker
  pool, then ``BlossomClient.mirror``.

One failed image never fails the run: the original URL stays in the
markdown and the failure is reported so the UI can say "1 of 4 images
couldn't be mirrored".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from ..blossom.auth import build_blossom_auth_event
from ..blossom.client import BlossomClient, server_origin
from ..bunker import BunkerSessionPool
from ..profiles import Profile


# Matches ``![alt](url)``. Mirrors the reference importer's scan; titles
# and angle-bracketed destinations are rare enough in feed-generated
# markdown that the simple form wins.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

_HTML_IMG_SRC_RE = re.compile(
    r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


@dataclass(frozen=True)
class MirrorProgress:
    """One image's state as it moves through the mirror lifecycle."""

    index: int
    total: int
    url: str
    label: str
    status: str  # "queued" | "mirroring" | "mirrored" | "failed"
    mirrored_url: str = ""
    error: str = ""


@dataclass
class MirrorOutcome:
    markdown: str
    mirrored: int = 0
    failed: List[str] = field(default_factory=list)


def image_label(url: str) -> str:
    """Last-segment filename for progress UI; falls back to the host."""
    try:
        parsed = urlparse(url)
        segments = [s for s in parsed.path.split("/") if s]
        last = segments[-1] if segments else (parsed.hostname or url)
        return unquote(last)[:80]
    except ValueError:
        return url[:80]


def scan_markdown_images(markdown: str) -> List[str]:
    """Unique image URLs in ``markdown``, in first-seen order.

    ``data:`` URIs are skipped: they are already self-contained.
    """
    seen: List[str] = []
    for match in _MD_IMAGE_RE.finditer(markdown or ""):
        url = match.group(2).strip()
        if not url or url.startswith("data:") or url in seen:
            continue
        seen.append(url)
    return seen


def scan_html_images(html: str) -> List[str]:
    """Unique ``<img src>`` URLs in an HTML fragment, first-seen order."""
    seen: List[str] = []
    for match in _HTML_IMG_SRC_RE.finditer(html or ""):
        url = match.group(1).strip()
        if not url or url.startswith("data:") or url in seen:
            continue
        seen.append(url)
    return seen


def rewrite_markdown_images(markdown: str, mapping: Dict[str, str]) -> str:
    """Replace image destinations per ``mapping``; unknown URLs stay."""

    def _sub(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2).strip()
        replacement = mapping.get(src)
        return f"![{alt}]({replacement})" if replacement else match.group(0)

    return _MD_IMAGE_RE.sub(_sub, markdown or "")


def rehost_images(
    markdown: str,
    *,
    mirror: Callable[[str, Callable[[str], None], Callable[[str], None]], None],
    skip_urls: Iterable[str] = (),
    on_progress: Optional[Callable[[MirrorProgress], None]] = None,
    on_done: Callable[[MirrorOutcome], None],
    is_cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Mirror every unique image in ``markdown`` and rewrite it.

    ``mirror(source_url, on_success, on_failure)`` performs one BUD-04
    mirror; ``on_success`` receives the mirrored URL. Images run
    *sequentially*: each mirror is a signer round-trip and parallel
    approval popups flood the user.

    URLs in ``skip_urls`` stay untouched: the user explicitly opted them
    out of mirroring. Cancellation stops issuing mirrors; ``on_done``
    still fires with whatever was rewritten so the draft ships.
    """
    skip = set(skip_urls or ())
    urls = [u for u in scan_markdown_images(markdown) if u not in skip]
    outcome = MirrorOutcome(markdown=markdown or "")
    if not urls:
        on_done(outcome)
        return

    total = len(urls)
    mapping: Dict[str, str] = {}

    def _report(index: int, url: str, status: str,
                mirrored_url: str = "", error: str = "") -> None:
        if on_progress is None:
            return
        try:
            on_progress(MirrorProgress(
                index=index, total=total, url=url, label=image_label(url),
                status=status, mirrored_url=mirrored_url, error=error,
            ))
        except Exception:  # noqa: BLE001, a UI observer must never sink the run
            pass

    # Seed every image as queued so the UI can render the full list
    # immediately and animate state changes in place.
    for index, url in enumerate(urls):
        _report(index, url, "queued")

    def _finish() -> None:
        outcome.markdown = rewrite_markdown_images(markdown, mapping)
        on_done(outcome)

    def _next(index: int) -> None:
        if index >= total or is_cancelled():
            _finish()
            return
        url = urls[index]
        _report(index, url, "mirroring")

        def _ok(mirrored_url: str, i=index, u=url) -> None:
            if mirrored_url:
                mapping[u] = mirrored_url
                outcome.mirrored += 1
                _report(i, u, "mirrored", mirrored_url=mirrored_url)
            else:
                outcome.failed.append(u)
                _report(i, u, "failed", error="Empty mirror response")
            _next(i + 1)

        def _err(reason: str, i=index, u=url) -> None:
            outcome.failed.append(u)
            _report(i, u, "failed", error=str(reason))
            _next(i + 1)

        try:
            mirror(url, _ok, _err)
        except Exception as exc:  # noqa: BLE001, transport bug must not hang the item
            _err(str(exc), index, url)

    _next(0)


def blossom_mirror(
    *,
    session_pool: BunkerSessionPool,
    profile: Profile,
    server: str,
    client: Optional[BlossomClient] = None,
    parent=None,
) -> Tuple[Callable[[str, Callable[[str], None], Callable[[str], None]], None],
           BlossomClient]:
    """Production mirror transport for :func:`rehost_images`.

    Returns ``(mirror, client)``: the callable signs a fresh BUD auth
    event per request through the bunker pool and issues the BUD-04
    mirror; the client is returned so the caller can own its lifetime.
    """
    blossom_client = client or BlossomClient(parent=parent)
    origin = server_origin(server)

    def _mirror(
        source_url: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        unsigned = build_blossom_auth_event(
            "upload", server=origin, pubkey_hex=profile.user_pubkey)

        def _on_ready(bunker_client) -> None:
            bunker_client.sign_event(
                unsigned,
                on_success=lambda signed: blossom_client.mirror(
                    origin,
                    source_url,
                    signed,
                    on_success=lambda result: on_success(
                        str(result.get("url") or "")),
                    on_failure=lambda err: on_failure(str(err)),
                ),
                on_failure=lambda reason: on_failure(
                    f"signer rejected the Blossom auth event: {reason}"),
            )

        session_pool.get(profile, on_ready=_on_ready, on_error=on_failure)

    return _mirror, blossom_client
