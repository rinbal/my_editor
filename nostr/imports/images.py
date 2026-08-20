# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image rehosting for imported drafts.

A feed body's images live on the source blog's server; once imported,
the draft would depend on that origin staying alive and permissive.
Rehosting puts a copy on the user's own Blossom server and rewrites the
markdown to that address.

Split of responsibilities:

- :func:`scan_markdown_images` / :func:`scan_html_images`: pure scans
  used by the pipeline (markdown, at import time) and the image-review
  dialog (HTML, at preview time).
- :func:`rehost_images`: the sequential loop over unique image URLs
  with per-image progress, a user-curated skip set, and markdown
  rewriting. The transport is an injected callable so the loop is
  testable without network or signer.
- :func:`blossom_rehost`: the production transport: fetch the source
  bytes, then hand them to the shared Blossom upload primitive in
  ``nostr.blossom.replicate``, which the media library uses too.

Why the bytes travel through this process rather than asking the
destination server to pull the source URL itself, which is what this
did before: BUD-11's endpoint table makes an ``x`` tag REQUIRED on
``PUT /mirror``, with "SHA-256 of the mirrored blob" as the implied
hash. A client that has never seen the bytes cannot produce that value,
so the old path shipped a mirror request missing a tag the spec
requires and a strict server is entitled to refuse. Fetching first
turns the hash into a fact this process measured: it goes in the token,
in ``X-SHA-256``, in the NIP-92 ``imeta`` tag the draft is published
with, and is checked against the descriptor that comes back. BUD-04's
own example flow assumes the client already uploaded the blob and still
holds that token, so mirroring an arbitrary third-party URL was never
the flow it describes; ``PUT /mirror`` is also optional for servers
while ``PUT /upload`` is not. The usual argument for
mirroring, that a browser cannot read another origin's bytes, does not
apply to a desktop app. The cost is that the user pays the download as
well as the upload, bounded by the size cap and the sequential loop.

One failed image never fails the run: the original URL stays in the
markdown and the failure is reported so the UI can say "1 of 4 images
couldn't be mirrored".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import url_safety

from ..blossom import replicate
from ..blossom.client import BlossomClient, server_origin
from ..blossom.plan import get_effective_max_file
from ..bunker import BunkerSessionPool
from ..profiles import Profile
from .fetch import BlobFetcher


# Copy shown for a URL the mirror policy refuses. Short because it lands
# in the per-image row of the review dialog next to the filename.
_UNSAFE_URL_ERROR = "URL was not allowed"


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


@dataclass(frozen=True)
class RehostedImage:
    """What this process measured about one image it put on a server.

    Every field is a measurement made here: the hash is of the bytes
    that were downloaded, the mime is what the download reported, and
    the size is the length of the buffer that was sent. The server's own
    blob descriptor is deliberately not the source. Its hash agrees by
    the time a caller sees this, because the upload primitive refuses a
    descriptor naming another blob, but its type and size are claims,
    and describing media with a claim nobody checked is the fabrication
    AD-14 forbids.
    """

    url: str
    sha256: str
    mime: str = ""
    size: int = 0


@dataclass
class MirrorOutcome:
    markdown: str
    mirrored: int = 0
    failed: List[str] = field(default_factory=list)
    # Source URL to rehosted URL, for the images that were actually
    # rewritten. Exposed so a caller holding the same URL somewhere
    # outside the body (the NIP-23 cover) can stay in step with it.
    mapping: Dict[str, str] = field(default_factory=dict)
    # Source URL to what was measured about the image now living at the
    # rewritten address, for the caller that publishes the draft and has
    # to say what its media is. Only rehosted images appear: a skipped
    # or failed one still points at somebody else's server and there is
    # nothing about it this process measured.
    descriptors: Dict[str, RehostedImage] = field(default_factory=dict)


def image_label(url: str) -> str:
    """Last-segment filename for progress UI; falls back to the host."""
    try:
        parsed = urlparse(url)
        segments = [s for s in parsed.path.split("/") if s]
        last = segments[-1] if segments else (parsed.hostname or url)
        return unquote(last)[:80]
    except ValueError:
        return url[:80]


def _is_web_url(url: str) -> bool:
    """True for an http(s) URL. Everything else is out of scope here.

    ``data:`` URIs are already self-contained, and ``file:``,
    ``javascript:`` and friends must never reach the review dialog's
    preview or a mirror request; the scans feed both.
    """
    try:
        return urlparse(url).scheme.lower() in ("http", "https")
    except ValueError:
        return False


def scan_markdown_images(markdown: str) -> List[str]:
    """Unique image URLs in ``markdown``, in first-seen order."""
    seen: List[str] = []
    for match in _MD_IMAGE_RE.finditer(markdown or ""):
        url = match.group(2).strip()
        if not url or url in seen or not _is_web_url(url):
            continue
        seen.append(url)
    return seen


def scan_html_images(html: str) -> List[str]:
    """Unique ``<img src>`` URLs in an HTML fragment, first-seen order."""
    seen: List[str] = []
    for match in _HTML_IMG_SRC_RE.finditer(html or ""):
        url = match.group(1).strip()
        if not url or url in seen or not _is_web_url(url):
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
    mirror: Callable[[str, Callable[..., None], Callable[[str], None]], None],
    skip_urls: Iterable[str] = (),
    on_progress: Optional[Callable[[MirrorProgress], None]] = None,
    on_done: Callable[[MirrorOutcome], None],
    is_cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Rehost every unique image in ``markdown`` and rewrite it.

    ``mirror(source_url, on_success, on_failure)`` rehosts one image;
    ``on_success`` receives the URL it now lives at, and optionally a
    :class:`RehostedImage` describing the bytes it sent. A transport
    that measured nothing passes the URL alone and the outcome simply
    carries no description of that image. Images run *sequentially*:
    each one is a signer round-trip and parallel approval popups flood
    the user.

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
    measured: Dict[str, RehostedImage] = {}

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
        outcome.mapping = dict(mapping)
        outcome.descriptors = dict(measured)
        on_done(outcome)

    def _next(index: int) -> None:
        if index >= total or is_cancelled():
            _finish()
            return
        url = urls[index]
        if not url_safety.is_safe_mirror_source(url):
            # Never fetch an address that points back into the local
            # network; the original URL stays in the markdown.
            outcome.failed.append(url)
            _report(index, url, "failed", error=_UNSAFE_URL_ERROR)
            _next(index + 1)
            return
        _report(index, url, "mirroring")

        def _ok(mirrored_url: str, description=None, i=index, u=url) -> None:
            if mirrored_url:
                mapping[u] = mirrored_url
                if description is not None:
                    measured[u] = description
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


def blossom_rehost(
    *,
    session_pool: BunkerSessionPool,
    profile: Profile,
    server: str,
    client: Optional[BlossomClient] = None,
    fetch_bytes: Optional[Callable[..., None]] = None,
    parent=None,
) -> Tuple[Callable[[str, Callable[..., None], Callable[[str], None]], None],
           BlossomClient]:
    """Production transport for :func:`rehost_images`.

    Returns ``(rehost, client)``: the callable downloads the source
    image, then puts those bytes on ``server`` through
    :func:`nostr.blossom.replicate.upload_to_server`, the same primitive
    the media library uploads through. The client is returned so the
    caller can own its lifetime.

    On success it reports the new URL together with a
    :class:`RehostedImage`, so the draft can be published with NIP-92
    metadata about the images this import actually put on a server.

    ``fetch_bytes(url, on_success=..., on_failure=...)`` is the download
    seam, injected by tests and defaulting to a :class:`BlobFetcher`
    capped at what ``server`` publishes as its own limit, or the
    fetcher's ceiling when that is lower. Nothing is signed until the
    bytes are in hand, so an image the server would refuse for its size
    costs no signer prompt.
    """
    blossom_client = client or BlossomClient(parent=parent)
    origin = server_origin(server)
    fetch = fetch_bytes
    if fetch is None:
        fetch = BlobFetcher(
            parent=parent, max_bytes=get_effective_max_file(origin)
        ).fetch

    def _rehost(
        source_url: str,
        on_success: Callable[..., None],
        on_failure: Callable[[str], None],
    ) -> None:
        # Second net: the loop already filters, and a caller wiring this
        # transport up differently must not get past it either. Refused
        # before anything leaves the process, so no request and no
        # signer prompt.
        if not url_safety.is_safe_mirror_source(source_url):
            on_failure(_UNSAFE_URL_ERROR)
            return

        def _got_bytes(body: bytes, mime: str) -> None:
            # Hashed once, here, so the token's `x` tag, the X-SHA-256
            # header, the check against the returned descriptor and the
            # `x` field of the published imeta tag are all the same
            # measurement of the same buffer.
            sha = hashlib.sha256(body).hexdigest()

            def _uploaded(result) -> None:
                rehosted = str(result.get("url") or "")
                on_success(rehosted, RehostedImage(
                    url=rehosted,
                    sha256=sha,
                    mime=mime or "",
                    size=len(body),
                ))

            replicate.upload_to_server(
                session_pool=session_pool,
                profile=profile,
                client=blossom_client,
                server=origin,
                body=body,
                mime=mime,
                sha256=sha,
                on_success=_uploaded,
                on_failure=lambda err: on_failure(str(err)),
            )

        fetch(source_url, on_success=_got_bytes, on_failure=on_failure)

    return _rehost, blossom_client
