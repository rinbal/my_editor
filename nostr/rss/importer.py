# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end import of one feed URL as NIP-37 drafts.

Pipeline:

  1. Resolve the user's NIP-65 relay list (so the long-form resolver
     has somewhere to read from).
  2. Fetch the feed via :class:`~nostr.rss.fetcher.FeedFetcher`.
  3. Parse with :func:`~nostr.rss.parser.parse_feed`.
  4. Filter (``since`` and ``limit``).
  5. For each surviving item:
     - If the link points at a NIP-23 long-form event (``nostr:naddr``
       URI scheme, or a thin item whose HTTP link embeds a bech32
       naddr), fetch the event from relays and use its content as the
       draft body.
     - Build an unsigned NIP-23 inner event via ``build_article`` and
       hand it to :class:`~nostr.publisher.DraftPublishJob` for
       encryption, wrapping (kind 31234), signing, and relay publish.

Jobs run *sequentially*. The signer's NIP-46 connection is the choke
point: parallel approvals would flood the bunker and most signers can't
batch approvals yet.

Cancellation: :meth:`cancel` suppresses further signal emissions and the
currently in-flight ``DraftPublishJob`` is cancelled. Items not yet
started are simply skipped.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from ..outbox import RelayListCache
from ..profiles import Profile
from ..publisher import DraftPublishJob, build_article
from ..relay import RelayPool
from ..bunker import BunkerSessionPool
from .discovery import (
    candidate_root_feed,
    extract_feeds_from_html,
    looks_like_html,
    normalize_user_url,
)
from .fetcher import FeedFetcher
from .normalize import ArticleTemplate, item_to_article, source_link_footer
from .nostr_resolver import (
    LongFormCoord,
    LongFormFetcher,
    extract_nostr_coord,
    is_nostr_uri_scheme,
)
from .parser import Feed, FeedItem, RssError, parse_feed


# Items whose markdown body (excluding the appended source-link footer)
# is shorter than this are treated as "thin" and eligible for resolution
# from a kind:30023 long-form event referenced by the link.
_THIN_CONTENT_CHARS: int = 80


# Friendly copy shown when discovery exhausts every option. Kept here so
# UX wording lives next to the logic that fires it.
_FRIENDLY_NO_FEED_FOUND = (
    "We couldn't find a feed at this site. "
    "Try pasting the feed URL directly, "
    "or look for an RSS / Atom link in the page footer."
)
_FRIENDLY_NOT_A_FEED = (
    "This response doesn't look like a feed. "
    "We expected RSS, Atom, or JSON Feed format."
)


class FeedImportJob(QObject):
    """End-to-end RSS to NIP-37 draft import for one feed URL.

    Signals (in firing order on the happy path):
      status_changed(str)              human-readable progress text
      feed_loaded(str, int)            (feed title, item count after filter)
      item_started(int, str)           (zero-based index, item title)
      item_resolving_from_nostr(int, str)
                                       (index, item title) — fires when
                                       the item is being fetched as a
                                       NIP-23 long-form event from relays
                                       before draft publishing. The
                                       eventual completion (success or
                                       fallback) is reported via the
                                       normal item_succeeded / item_failed
                                       signals.
      item_succeeded(int, str)         (index, identifier hex)
      item_failed(int, str)            (index, short reason)
      progress(int, int)               (done, total)
      completed(int, int)              (succeeded, attempted) on normal end
      failed(str)                      terminal pre-item failure (fetch/parse)

    Per-item failures do not stop the import: the next item is attempted.
    Only fetch and parse failures fire ``failed`` and terminate the job.
    """

    status_changed = Signal(str)
    feed_loaded = Signal(str, int)
    item_started = Signal(int, str)
    item_resolving_from_nostr = Signal(int, str)
    item_succeeded = Signal(int, str)
    item_failed = Signal(int, str)
    progress = Signal(int, int)
    completed = Signal(int, int)
    failed = Signal(str)

    def __init__(
        self,
        *,
        feed_url: str,
        profile: Profile,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        since: Optional[int] = None,
        limit: Optional[int] = None,
        append_source_link: bool = True,
        extra_hashtags: Optional[List[str]] = None,
        identifier_prefix: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        normalised = normalize_user_url(feed_url or "")
        if not normalised:
            raise ValueError("feed URL must not be empty")
        # ``_current_url`` is the URL we're actually fetching right now.
        # Discovery may rewrite it; ``_tried_urls`` records every value
        # it ever held so we don't refetch the same target twice.
        self._current_url: str = normalised
        self._profile = profile
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._since = since
        self._limit = limit
        self._append_source_link = append_source_link
        self._extra_hashtags: Tuple[str, ...] = tuple(extra_hashtags or ())
        self._identifier_prefix = identifier_prefix

        self._fetcher = FeedFetcher(self)
        self._long_form_fetcher = LongFormFetcher(relay_pool, parent=self)
        self._queue: List[FeedItem] = []
        self._index: int = 0
        self._succeeded: int = 0
        self._attempted: int = 0
        self._current_job: Optional[DraftPublishJob] = None
        self._cancelled: bool = False
        # Track URLs we've already tried so discovery can't loop and we
        # never waste a round-trip re-fetching the same target.
        self._tried_urls: set[str] = set()
        # User's NIP-65 read set, resolved once at start() so per-item
        # long-form lookups don't each pay a relay-list round-trip.
        self._user_read_relays: List[str] = []

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Kick off the import. Safe to call once per instance.

        The user's NIP-65 read relays are resolved before the feed fetch
        so the long-form resolver has a target set ready when it's time
        to substitute thin items. The feed fetch itself doesn't need
        them, but ordering this first keeps the rest of the pipeline
        synchronous-feeling and avoids a per-item lookup later.
        """
        self._emit_status("Looking up your relay list...")
        self._relay_list_cache.fetch(
            self._profile.user_pubkey,
            relays=list(dict.fromkeys(self._profile.bunker_relays)),
            on_done=self._on_relay_list_ready,
        )

    def _on_relay_list_ready(self, relay_list) -> None:
        if self._cancelled:
            return
        # ``read`` is the NIP-65 "this is where I read events from" set.
        # Use it as the starting point for long-form lookups; per-naddr
        # relay hints are added on top inside LongFormFetcher.fetch.
        self._user_read_relays = list(getattr(relay_list, "read", ()) or ())
        self._fetch(self._current_url, status=f"Fetching {self._current_url}")

    # -- fetch helper ------------------------------------------------------

    def _fetch(self, url: str, *, status: Optional[str] = None) -> None:
        """Issue one HTTP attempt against ``url`` and record the try.

        ``_tried_urls`` makes discovery idempotent: if a hint we
        derived points at a URL we already fetched, we surrender
        cleanly instead of looping.
        """
        self._current_url = url
        self._tried_urls.add(url)
        if status:
            self._emit_status(status)
        self._fetcher.fetch(
            url,
            on_success=self._on_feed_text,
            on_failure=self._on_fetch_failed,
        )

    def cancel(self) -> None:
        """Stop the import. The in-flight draft job (if any) is cancelled
        and no further per-item work is scheduled."""
        self._cancelled = True
        if self._current_job is not None:
            self._current_job.cancel()

    # -- signal-emission helpers (guarded by ``_cancelled``) --------------

    def _emit_status(self, text: str) -> None:
        if not self._cancelled:
            self.status_changed.emit(text)

    def _emit_failed(self, reason: str) -> None:
        if not self._cancelled:
            self.failed.emit(reason)

    # -- fetch + parse -----------------------------------------------------

    def _on_fetch_failed(self, reason: str) -> None:
        if self._cancelled:
            return
        # ``reason`` is QNetworkReply.errorString() text such as "Host not
        # found" or "Connection refused". Those are already user-readable;
        # we just frame them with a soft preface.
        self._emit_failed(f"We couldn't reach that URL: {reason.rstrip('.')}.")

    def _on_feed_text(self, text: str) -> None:
        if self._cancelled:
            return
        try:
            feed = parse_feed(text)
        except RssError:
            # Response wasn't a parseable feed. Two possibilities:
            #   (a) it's an HTML page, in which case discovery may find
            #       a real feed advertised in <link rel="alternate">,
            #   (b) it's garbage / a non-feed XML doc, in which case we
            #       give a friendly explanation.
            if self._try_discovery(text):
                return
            self._emit_failed(self._friendly_unparseable_message(text))
            return
        except Exception as exc:  # noqa: BLE001
            self._emit_failed(f"Unexpected parse error: {exc}")
            return

        items = self._filter_items(feed)
        self._queue = items
        if not self._cancelled:
            self.feed_loaded.emit(feed.title or self._current_url, len(items))
        self._emit_status(
            f"{len(items)} item(s) to import"
            if items
            else "No items to import after filtering"
        )
        if not items:
            if not self._cancelled:
                self.completed.emit(0, 0)
            return

        self._start_next_item()

    # -- discovery --------------------------------------------------------

    def _try_discovery(self, html_or_garbage: str) -> bool:
        """If the body looks like HTML, try to find a feed link and
        retry. Returns ``True`` when a retry was scheduled, in which
        case the caller must not also emit a failure.
        """
        if not looks_like_html(html_or_garbage):
            return False

        # First preference: <link rel="alternate" type="application/rss+xml" ...>.
        hints = extract_feeds_from_html(
            html_or_garbage,
            base_url=self._current_url,
        )
        for hint in hints:
            if hint.url not in self._tried_urls:
                self._fetch(
                    hint.url,
                    status=f"Found feed link, fetching {hint.url}",
                )
                return True

        # Second preference: the well-known ``/feed/`` root path. Covers
        # WordPress, Ghost, and any site that follows the convention.
        fallback = candidate_root_feed(self._current_url)
        if fallback and fallback not in self._tried_urls:
            self._fetch(
                fallback,
                status=f"No feed link in page, trying {fallback}",
            )
            return True

        return False

    def _friendly_unparseable_message(self, body: str) -> str:
        """Human-readable explanation when discovery has nothing left."""
        if looks_like_html(body):
            return _FRIENDLY_NO_FEED_FOUND
        return _FRIENDLY_NOT_A_FEED

    def _filter_items(self, feed: Feed) -> List[FeedItem]:
        items = list(feed.items)
        if self._since is not None:
            cutoff = int(self._since)
            items = [it for it in items if (it.published_at or 0) >= cutoff]
        if self._limit is not None and self._limit >= 0:
            items = items[: int(self._limit)]
        return items

    # -- per-item pipeline -------------------------------------------------

    def _start_next_item(self) -> None:
        if self._cancelled:
            return
        if self._index >= len(self._queue):
            self.completed.emit(self._succeeded, self._attempted)
            return

        item = self._queue[self._index]
        self._attempted += 1
        title_for_ui = item.title or item.link or "(untitled)"
        self.item_started.emit(self._index, title_for_ui)
        self._emit_status(f"Item {self._index + 1}/{len(self._queue)}: {title_for_ui}")

        try:
            template = item_to_article(
                item,
                hashtags=self._extra_hashtags,
                identifier_prefix=self._identifier_prefix,
                append_source_link=self._append_source_link,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure(f"Could not normalise item: {exc}")
            return

        # If the item points at a NIP-23 long-form event, route through
        # the resolver before publishing. The resolver always ends up
        # back in ``_publish_template_as_draft`` (with substituted or
        # original content), so the rest of the pipeline doesn't branch.
        coord = self._coord_for_resolution(item, template)
        if coord is not None:
            self._resolve_long_form_then_publish(coord, template, item, title_for_ui)
            return

        self._publish_template_as_draft(template)

    def _coord_for_resolution(
        self,
        item: FeedItem,
        template: ArticleTemplate,
    ) -> Optional[LongFormCoord]:
        """Decide whether to fetch the item's body from Nostr.

        Returns the coordinate to resolve, or ``None`` to use the feed
        body as-is.

        Two cases qualify:
        1. The item link is a ``nostr:naddr...`` URI. The publisher
           explicitly tells us the prose lives on Nostr, so we always
           resolve.
        2. The item link is HTTP(S) but embeds a bech32 naddr (njump,
           habla, yakihonne, etc.) AND the feed body is "thin" (under
           :data:`_THIN_CONTENT_CHARS` characters once the source-link
           footer is subtracted). In that case the feed clearly only
           ships a teaser and the real content is one Nostr fetch away.
        """
        # Some feeds put the naddr in <guid> instead of <link>; try both.
        coord = extract_nostr_coord(item.link) or extract_nostr_coord(item.guid)
        if coord is None:
            return None
        if is_nostr_uri_scheme(item.link) or is_nostr_uri_scheme(item.guid):
            return coord
        # HTTP link with embedded naddr: only resolve when the body is
        # genuinely thin, otherwise the feed-provided prose is the most
        # canonical thing we have.
        return coord if self._body_is_thin(template, item) else None

    def _body_is_thin(self, template: ArticleTemplate, item: FeedItem) -> bool:
        """Measure the markdown body excluding the source-link footer."""
        body_chars = len(template.content.strip())
        if self._append_source_link and item.link:
            footer_chars = len(source_link_footer(item.link))
            body_chars = max(0, body_chars - footer_chars)
        return body_chars < _THIN_CONTENT_CHARS

    def _resolve_long_form_then_publish(
        self,
        coord: LongFormCoord,
        template: ArticleTemplate,
        item: FeedItem,
        title_for_ui: str,
    ) -> None:
        """Fetch the kind:30023 event and publish with substituted prose.

        On any failure (timeout, no relays, empty event content) we fall
        back to the feed-provided template so the draft still ships.
        """
        if not self._cancelled:
            self.item_resolving_from_nostr.emit(self._index, title_for_ui)
        self._emit_status(f"Resolving '{title_for_ui}' from Nostr...")
        self._long_form_fetcher.fetch(
            coord,
            extra_relays=self._user_read_relays,
            on_success=lambda event, t=template, i=item: (
                self._on_long_form_resolved(event, t, i)
            ),
            on_not_found=lambda t=template: self._publish_template_as_draft(t),
        )

    def _on_long_form_resolved(
        self,
        event: dict,
        template: ArticleTemplate,
        item: FeedItem,
    ) -> None:
        if self._cancelled:
            return
        # NIP-23 ``.content`` is already Markdown — no HTML conversion
        # needed. Empty content means the event is itself a stub, in
        # which case the feed-provided template is the better artefact.
        prose = (event.get("content") or "").strip()
        if not prose:
            self._publish_template_as_draft(template)
            return
        if self._append_source_link and item.link:
            prose = (prose + source_link_footer(item.link)).strip()
        self._publish_template_as_draft(replace(template, content=prose))

    def _publish_template_as_draft(self, template: ArticleTemplate) -> None:
        """Build the inner event and kick off the ``DraftPublishJob``."""
        if self._cancelled:
            return
        try:
            inner = build_article(
                template.content,
                self._profile.user_pubkey,
                template.slug,
                title=template.title,
                summary=template.summary,
                image=template.image,
                published_at=template.published_at,
                hashtags=template.hashtags,
            )
        except ValueError as exc:
            self._record_failure(f"Could not build article: {exc}")
            return

        try:
            job = DraftPublishJob(
                relay_pool=self._relay_pool,
                relay_list_cache=self._relay_list_cache,
                session_pool=self._session_pool,
                profile=self._profile,
                inner_event=inner,
                identifier=template.slug,
                parent=self,
            )
        except ValueError as exc:
            self._record_failure(f"Could not start draft job: {exc}")
            return

        self._current_job = job
        slug_for_signals = template.slug
        job.status_changed.connect(self._on_draft_status)
        job.stashed.connect(
            lambda identifier, _event_id, _ts, _slug=slug_for_signals: (
                self._on_draft_stashed(_slug)
            )
        )
        job.completed.connect(
            lambda _results, _slug=slug_for_signals: self._on_draft_completed(_slug)
        )
        job.failed.connect(self._on_draft_failed)
        job.start()

    def _on_draft_status(self, text: str) -> None:
        # Forward the underlying job's status so the panel can show what
        # the signer / relays are doing without us re-deriving it.
        self._emit_status(text)

    def _on_draft_stashed(self, identifier: str) -> None:
        # ``stashed`` fires before relay results; mark this item as
        # succeeded as soon as it's signed (the draft already exists on
        # the user's signer and the relay publish is best-effort).
        if self._cancelled:
            return
        self.item_succeeded.emit(self._index, identifier)
        self._succeeded += 1

    def _on_draft_completed(self, _identifier: str) -> None:
        if self._cancelled:
            return
        self._current_job = None
        self.progress.emit(self._index + 1, len(self._queue))
        self._index += 1
        self._start_next_item()

    def _on_draft_failed(self, reason: str) -> None:
        if self._cancelled:
            return
        self._record_failure(reason)

    def _record_failure(self, reason: str) -> None:
        """Per-item failure path. Increments index and moves on."""
        self._current_job = None
        self.item_failed.emit(self._index, reason)
        self.progress.emit(self._index + 1, len(self._queue))
        self._index += 1
        self._start_next_item()
