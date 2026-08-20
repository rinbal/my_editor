# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import source-resolver registry.

A single, ordered list of *source resolvers*. Each resolver knows how to
(a) recognise one kind of import source (``detect``) and (b) turn it
into the app's normalised ``Feed`` shape (``resolve``). Everything
downstream of a resolver (preview, filtering, the import pipeline, the
UI) consumes that one ``Feed``/``FeedItem`` shape and never needs to
know which kind of source produced it.

Adding a new format is a closed, local change: write one resolver
module, register it here, add its tests. No edits to the pipeline or
the UI.

Order matters: resolvers are matched top-to-bottom, most specific
first, with the RSS resolver last as the broad catch-all. A resolver
whose ``detect`` raises is skipped rather than allowed to break
matching.

Resolution is asynchronous (network I/O runs on the Qt event loop), so
``resolve`` is callback-driven: exactly one of ``on_success`` /
``on_failure`` fires per call, plus optional ``on_stage`` progress
callbacks along the way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple, Union

from ..rss.parser import Feed
from .errors import ERROR_CODES, SourceError


@dataclass(frozen=True)
class ResolveInput:
    """What the user handed us: a URL, a pasted body, or both."""

    url: Optional[str] = None
    pasted_body: Optional[str] = None


@dataclass(frozen=True)
class ResolveResult:
    """A resolved source: the canonical URL to key on, plus the feed."""

    url: str
    feed: Feed
    # Alternatives considered during discovery, in the order they were
    # tried; lets UX surfaces say "we picked X, here are more we found".
    hints: Tuple = ()


@dataclass(frozen=True)
class ResolveContext:
    """Callbacks + collaborators handed to a resolver.

    ``fetcher`` is any object exposing
    ``fetch(url, *, on_success, on_failure)`` where ``on_failure``
    receives a :class:`SourceError` (see ``fetch.SourceFetcher``).
    ``is_cancelled`` is polled between steps so an abandoned resolution
    stops issuing network requests; the contract that exactly one
    terminal callback fires is waived once it returns ``True``.
    ``run_blocking`` optionally offloads CPU-heavy steps (parsing) to a
    worker thread with the shape ``(fn, on_done, on_error)``; when
    absent, :meth:`blocking` runs the function inline, which keeps pure
    tests synchronous.
    """

    fetcher: object
    on_success: Callable[[ResolveResult], None]
    on_failure: Callable[[SourceError], None]
    on_stage: Optional[Callable[[dict], None]] = None
    is_cancelled: Callable[[], bool] = field(default=lambda: False)
    run_blocking: Optional[Callable[..., None]] = None
    # Relay-query surface for Nostr-facing resolvers (an object with
    # ``latest(relays, filters, on_done)`` and ``addressable(...)``,
    # see ``sources.nostr.RelayQueryAdapter``). ``None`` when the host
    # has no relay pool; those resolvers then fail with a clear
    # NO_RELAY_ACCESS error instead of crashing.
    nostr_query: Optional[object] = None

    def stage(self, name: str, url: str = "", **extra) -> None:
        if self.on_stage is None:
            return
        try:
            self.on_stage({"name": name, "url": url, **extra})
        except Exception:  # noqa: BLE001, a UI observer must never sink a resolve
            pass

    def blocking(
        self,
        fn: Callable[[], object],
        on_done: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """Run ``fn`` via the executor when one is wired, else inline."""
        if self.run_blocking is not None:
            self.run_blocking(fn, on_done, on_error)
            return
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001, routed to on_error by contract
            on_error(exc)
            return
        on_done(result)


@dataclass(frozen=True)
class SourceResolver:
    """One source kind: cheap synchronous ``detect``, async ``resolve``."""

    id: str
    label: str
    detect: Callable[[ResolveInput], bool]
    resolve: Callable[[ResolveInput, ResolveContext], None]


def _normalize_input(value: Union[ResolveInput, str, None]) -> ResolveInput:
    if isinstance(value, ResolveInput):
        url = value.url.strip() if isinstance(value.url, str) else value.url
        return ResolveInput(url=url, pasted_body=value.pasted_body)
    if isinstance(value, str):
        return ResolveInput(url=value.strip())
    return ResolveInput()


def _resolvers() -> Tuple[SourceResolver, ...]:
    # Imported lazily so registry import stays cheap and resolver
    # modules can import registry types without a cycle.
    from .resolvers.bluesky import BLUESKY_RESOLVER
    from .resolvers.ghost import GHOST_RESOLVER
    from .resolvers.mdx import MDX_RESOLVER
    from .resolvers.nostr import NOSTR_RESOLVER
    from .resolvers.nostrhub import NOSTRHUB_RESOLVER
    from .resolvers.rss import RSS_RESOLVER
    from .resolvers.sitemap import SITEMAP_RESOLVER
    from .resolvers.wxr import WXR_RESOLVER

    # Most specific first; the RSS resolver stays last as the
    # catch-all. New resolvers are inserted *above* it. NostrHub
    # precedes the generic Nostr resolver so nostrhub.io links keep
    # their kind-30817 handling; WXR and Ghost are body-detected
    # (export files) and must outrank the RSS paste path.
    return (
        NOSTRHUB_RESOLVER,
        NOSTR_RESOLVER,
        BLUESKY_RESOLVER,
        MDX_RESOLVER,
        WXR_RESOLVER,
        GHOST_RESOLVER,
        SITEMAP_RESOLVER,
        RSS_RESOLVER,
    )


def detect_resolver(value: Union[ResolveInput, str, None]) -> Optional[SourceResolver]:
    """First resolver that claims the input, or ``None`` if none do."""
    norm = _normalize_input(value)
    for resolver in _resolvers():
        try:
            if resolver.detect(norm):
                return resolver
        except Exception:  # noqa: BLE001, a broken detector must not sink matching
            continue
    return None


def can_resolve_source(value: Union[ResolveInput, str, None]) -> bool:
    """The single validation authority for "is this a supported source?".

    Registering a new resolver automatically widens what callers (the
    panel's URL gate, a future subscription store) accept.
    """
    return detect_resolver(value) is not None


def resolve_source(
    value: Union[ResolveInput, str],
    *,
    fetcher: object,
    on_success: Callable[[ResolveResult], None],
    on_failure: Callable[[SourceError], None],
    on_stage: Optional[Callable[[dict], None]] = None,
    is_cancelled: Callable[[], bool] = lambda: False,
    run_blocking: Optional[Callable[..., None]] = None,
    nostr_query: Optional[object] = None,
) -> None:
    """Resolve ``value`` into a normalised ``Feed`` via the first
    matching resolver. Fails with ``UNSUPPORTED_SOURCE`` when nothing
    claims the input."""
    norm = _normalize_input(value)
    resolver = detect_resolver(norm)
    ctx = ResolveContext(
        fetcher=fetcher,
        on_success=on_success,
        on_failure=on_failure,
        on_stage=on_stage,
        is_cancelled=is_cancelled,
        run_blocking=run_blocking,
        nostr_query=nostr_query,
    )
    if resolver is None:
        on_failure(SourceError(
            "This source is not one we know how to import",
            ERROR_CODES.UNSUPPORTED_SOURCE,
        ))
        return
    resolver.resolve(norm, ctx)
