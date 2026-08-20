# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import a list of already-selected feed items as NIP-37 drafts.

The back half of the import flow. Resolution (fetch + parse + discovery)
happens earlier, behind the resolver registry, at preview time; this job
receives the user's *selection* of normalised ``FeedItem``s and turns
each into an encrypted draft:

  1. Normalise the item to an :class:`ArticleTemplate` (HTML-to-Markdown
     conversion runs on a worker thread; a big body must not stall the
     UI).
  2. Long-form (naddr) recovery: when the item's link or guid carries an
     addressable coordinate (bech32 naddr anywhere in the value, or a
     bare NIP-01 ``kind:pubkey:d``), the canonical prose lives in a
     kind-30023 event on relays and the feed body is only a teaser,
     *however long that teaser reads*. So we always resolve when a
     coordinate is present and prefer the relay body when it is longer
     than the feed body, falling back to the feed text otherwise.
  3. Full-text recovery (opt-in, default on): a thin item (teaser under
     ``THIN_CONTENT_CHARS``) with an ordinary http(s) link gets its
     article page fetched and the main content lifted via Readability.
     Adopted only when the result is a clear win: a bad extraction
     (stray nav / footer) is usually *shorter*, not longer. Recovers
     the real title for slug-titled (sitemap) items.
  4. Image rehosting (opt-in, default on when a Blossom server is
     configured): every unique image in the markdown is mirrored to the
     user's Blossom server (BUD-04, the server pulls the source URL
     itself) and the markdown rewritten. A user-curated skip set keeps
     chosen images at their original URLs; one failed image never fails
     the item.
  5. Build the unsigned NIP-23 inner event via ``build_article`` (with
     the ``source`` tag identifying the origin feed) and hand it to a
     ``DraftPublishJob`` for encryption, NIP-37 wrapping (kind 31234),
     signing, and relay publish.

Items run *sequentially*: the signer's NIP-46 connection is the choke
point, parallel approval popups flood the user and most signers can't
batch. Batches above ``BATCH_PACE_THRESHOLD`` insert a short pause
between items so relays and the signer see a civilised cadence.

Accounting invariant (verified against ``publisher.py``): a
``DraftPublishJob`` never fires ``failed`` after ``stashed``; once
signed, the only remaining outcome is ``completed`` with per-relay
results. Counting success at stash time is therefore sound.

Identifier migration: new imports carry the ``rss-`` d-tag prefix. When
``identifier_exists`` reports that a draft with the *bare* (pre-prefix)
identifier already exists, that identifier is reused so the import
replaces the existing draft instead of silently duplicating it.

Every external boundary is injectable (``long_form_fetcher``,
``publish_job_factory``, ``run_blocking`` executor, ``pacer``), so the
whole state machine is testable without network, relays, a signer, or
timers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from ..outbox import RelayListCache
from ..profiles import Profile
from ..publisher import DraftPublishJob, build_article
from ..relay import RelayPool
from ..bunker import BunkerSessionPool
from ..rss.normalize import (
    ArticleTemplate,
    html_to_markdown,
    item_to_article,
    source_link_footer,
)
from ..rss.nostr_resolver import (
    LongFormCoord,
    LongFormFetcher,
    extract_nostr_coord,
)
from ..rss.parser import FeedItem
from . import workers
from .constants import (
    BATCH_PACE_MS,
    BATCH_PACE_THRESHOLD,
    IDENTIFIER_PREFIX,
    SOURCE_TAG,
    THIN_CONTENT_CHARS,
)
from .fetch import SourceFetcher
from .fulltext import extract_readable_content
from .images import MirrorProgress, blossom_mirror, rehost_images
from .sources.podcast import build_podcast_markdown, fetch_podcast_chapters


class ImportItemsJob(QObject):
    """Sequential import of selected feed items as encrypted drafts.

    Signals (per item, in firing order on the happy path):
      status_changed(str)              human-readable progress text
      item_started(int, str)           (zero-based index, item title)
      item_resolving_from_nostr(int, str)
                                       fires while the item's body is
                                       fetched as a NIP-23 event
      item_extracting(int, str)        fires while a thin item's source
                                       page is fetched for full-text
                                       recovery
      item_mirroring(int, int, int, int)
                                       (index, mirrored, failed, total)
                                       as the item's images move through
                                       the Blossom mirror loop
      item_succeeded(int, str)         (index, identifier). Fires at
                                       *stash* time: signed and saved;
                                       relay publish still in flight.
      item_published(int, int, int)    (index, accepted relays, total)
                                       once the relay publish settles;
                                       ``accepted`` may be 0.
      item_failed(int, str)            (index, short reason)
      progress(int, int)               (done, total)
    And once per run:
      completed(int, int)              (succeeded, attempted)

    Per-item failures never stop the batch. There is no terminal
    ``failed``: resolution already happened at preview time, so every
    failure here is per-item.
    """

    status_changed = Signal(str)
    item_started = Signal(int, str)
    item_resolving_from_nostr = Signal(int, str)
    item_extracting = Signal(int, str)
    item_mirroring = Signal(int, int, int, int)
    item_succeeded = Signal(int, str)
    item_published = Signal(int, int, int)
    item_failed = Signal(int, str)
    progress = Signal(int, int)
    completed = Signal(int, int)

    def __init__(
        self,
        *,
        items: Sequence[FeedItem],
        feed_url: str,
        profile: Profile,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        append_source_link: bool = True,
        fetch_full_text: bool = True,
        rehost_images: bool = True,
        blossom_server: str = "",
        skip_image_urls: Optional[Iterable[str]] = None,
        extra_hashtags: Optional[List[str]] = None,
        identifier_prefix: Optional[str] = IDENTIFIER_PREFIX,
        identifier_exists: Optional[Callable[[str], bool]] = None,
        fetcher: Optional[SourceFetcher] = None,
        long_form_fetcher: Optional[LongFormFetcher] = None,
        image_mirror: Optional[Callable[..., None]] = None,
        publish_job_factory: Optional[Callable[..., DraftPublishJob]] = None,
        run_blocking: Optional[Callable[..., None]] = None,
        pacer: Optional[Callable[[int, Callable[[], None]], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._items: List[FeedItem] = list(items)
        self._feed_url = (feed_url or "").strip()
        self._profile = profile
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._append_source_link = append_source_link
        self._fetch_full_text = fetch_full_text
        self._skip_image_urls = set(skip_image_urls or ())
        self._extra_hashtags: Tuple[str, ...] = tuple(extra_hashtags or ())
        self._identifier_prefix = identifier_prefix
        self._identifier_exists = identifier_exists

        # Injectable seams; defaults wire to the real stack.
        self._fetcher = fetcher if fetcher is not None else SourceFetcher(self)
        self._long_form_fetcher = (
            long_form_fetcher
            if long_form_fetcher is not None
            else LongFormFetcher(relay_pool, parent=self)
        )
        self._publish_job_factory = publish_job_factory or DraftPublishJob
        self._run_blocking = run_blocking or (
            lambda fn, ok, err: workers.run_blocking(fn, ok, err, parent=self)
        )
        self._pacer = pacer or (lambda ms, fn: QTimer.singleShot(ms, fn))
        # Image-mirror transport: injected for tests, else the real
        # Blossom BUD-04 path when a server is configured. ``None``
        # disables the mirror step entirely.
        self._image_mirror = image_mirror
        if (
            self._image_mirror is None
            and rehost_images
            and blossom_server
        ):
            self._image_mirror, _ = blossom_mirror(
                session_pool=session_pool,
                profile=profile,
                server=blossom_server,
                parent=self,
            )
        if not rehost_images:
            self._image_mirror = None

        self._index: int = 0
        self._succeeded: int = 0
        self._attempted: int = 0
        self._current_job: Optional[DraftPublishJob] = None
        self._cancelled: bool = False
        self._user_read_relays: List[str] = []

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Kick off the import. Safe to call once per instance."""
        if not self._items:
            self.completed.emit(0, 0)
            return
        # The user's NIP-65 read relays are resolved up front so
        # per-item long-form lookups don't each pay a round-trip.
        self._emit_status("Looking up your relay list…")
        self._relay_list_cache.fetch(
            self._profile.user_pubkey,
            relays=list(dict.fromkeys(self._profile.bunker_relays)),
            on_done=self._on_relay_list_ready,
        )

    def cancel(self) -> None:
        """Stop the import. The in-flight draft job (if any) is
        cancelled and no further per-item work is scheduled."""
        self._cancelled = True
        if self._current_job is not None:
            self._current_job.cancel()

    # -- setup -------------------------------------------------------------

    def _on_relay_list_ready(self, relay_list) -> None:
        if self._cancelled:
            return
        self._user_read_relays = list(getattr(relay_list, "read", ()) or ())
        self._start_next_item()

    # -- helpers -----------------------------------------------------------

    def _emit_status(self, text: str) -> None:
        if not self._cancelled:
            self.status_changed.emit(text)

    # -- per-item pipeline -------------------------------------------------

    def _start_next_item(self) -> None:
        if self._cancelled:
            return
        if self._index >= len(self._items):
            self.completed.emit(self._succeeded, self._attempted)
            return
        if self._index > 0 and len(self._items) > BATCH_PACE_THRESHOLD:
            # Breathing room between items on large batches; the guard
            # inside _begin_current_item covers a cancel mid-pause.
            self._pacer(BATCH_PACE_MS, self._begin_current_item)
        else:
            self._begin_current_item()

    def _begin_current_item(self) -> None:
        if self._cancelled:
            return
        item = self._items[self._index]
        self._attempted += 1
        title_for_ui = item.title or item.link or "(untitled)"
        self.item_started.emit(self._index, title_for_ui)
        self._emit_status(
            f"Item {self._index + 1}/{len(self._items)}: {title_for_ui}"
        )
        # HTML-to-Markdown conversion is the CPU-heavy step; keep it off
        # the UI thread.
        self._run_blocking(
            lambda item=item: item_to_article(
                item,
                hashtags=self._extra_hashtags,
                identifier_prefix=self._identifier_prefix,
                append_source_link=self._append_source_link,
            ),
            lambda template, item=item, t=title_for_ui: (
                self._on_template_ready(item, template, t)
            ),
            lambda exc: self._on_template_failed(exc),
        )

    def _on_template_failed(self, exc: BaseException) -> None:
        if self._cancelled:
            return
        self._record_failure(f"Could not normalise item: {exc}")

    def _on_template_ready(
        self,
        item: FeedItem,
        template: ArticleTemplate,
        title_for_ui: str,
    ) -> None:
        if self._cancelled:
            return
        template = self._apply_identifier_migration(template)
        # Sources that already deliver Markdown (Nostr long-form,
        # NostrHub NIPs, MDX) are authoritative: the body IS the
        # canonical prose, so no recovery pass could improve on it.
        if (item.content_markdown or "").strip():
            self._publish_template_as_draft(template)
            return
        # Podcast episodes become a structured Markdown body (audio,
        # chapters, transcript, show notes). The feed body IS the show
        # notes; the episode's web page would only duplicate them, so
        # this branch precludes naddr/full-text recovery.
        episode = item.podcast
        if episode is not None and getattr(episode, "audio", ""):
            self._resolve_podcast(item, template, episode, title_for_ui)
            return
        # Recovery chain: naddr first (an addressable coordinate is an
        # explicit publisher pointer), full-text second, feed body last.
        coord = extract_nostr_coord(item.link) or extract_nostr_coord(item.guid)
        if coord is not None:
            self._resolve_long_form(coord, template, item, title_for_ui)
            return
        self._maybe_recover_full_text(item, template, title_for_ui)

    def _apply_identifier_migration(
        self, template: ArticleTemplate
    ) -> ArticleTemplate:
        """Grandfather pre-prefix imports.

        When a draft with the bare (unprefixed) identifier already
        exists locally, keep that identifier so re-importing replaces
        the existing draft instead of duplicating it under the new
        prefixed d-tag. Defensive: a failing probe keeps the prefix.
        """
        prefix = self._identifier_prefix or ""
        if not prefix or self._identifier_exists is None:
            return template
        if not template.slug.startswith(prefix):
            return template
        bare = template.slug[len(prefix):]
        if not bare:
            return template
        try:
            if self._identifier_exists(bare):
                return replace(template, slug=bare)
        except Exception:  # noqa: BLE001, probe failure must not sink the item
            pass
        return template

    def _feed_body_length(self, template: ArticleTemplate, item: FeedItem) -> int:
        """Length of the markdown body excluding the source-link footer."""
        body_chars = len(template.content.strip())
        if self._append_source_link and item.link:
            footer_chars = len(source_link_footer(item.link))
            body_chars = max(0, body_chars - footer_chars)
        return body_chars

    def _body_is_thin(self, template: ArticleTemplate, item: FeedItem) -> bool:
        return self._feed_body_length(template, item) < THIN_CONTENT_CHARS

    def _with_footer(self, content: str, item: FeedItem) -> str:
        if self._append_source_link and item.link:
            return (content + source_link_footer(item.link)).strip()
        return content.strip()

    # -- podcast episodes --------------------------------------------------

    def _resolve_podcast(
        self,
        item: FeedItem,
        template: ArticleTemplate,
        episode,
        title_for_ui: str,
    ) -> None:
        """Fetch chapters (best-effort) and render the episode body."""
        self.item_extracting.emit(self._index, title_for_ui)
        self._emit_status(f"Fetching chapters for '{title_for_ui}'…")

        def _with_chapters(chapters) -> None:
            if self._cancelled:
                return
            self._run_blocking(
                lambda: build_podcast_markdown(
                    episode,
                    chapters=chapters,
                    notes=html_to_markdown(item.content_html or "").strip(),
                ),
                lambda markdown: self._publish_template_as_draft(
                    replace(template, content=self._with_footer(markdown, item))),
                lambda _exc, t=template: self._publish_template_as_draft(t),
            )

        if episode.chapters_url:
            fetch_podcast_chapters(
                self._fetcher, episode.chapters_url, _with_chapters)
        else:
            _with_chapters([])

    # -- long-form (naddr) recovery ----------------------------------------

    def _resolve_long_form(
        self,
        coord: LongFormCoord,
        template: ArticleTemplate,
        item: FeedItem,
        title_for_ui: str,
    ) -> None:
        """Fetch the kind:30023 event; adopt its prose when it beats the
        feed body. On any failure (timeout, no relays, stub event) fall
        through to full-text recovery and ultimately the feed body, so
        the draft always ships."""
        if not self._cancelled:
            self.item_resolving_from_nostr.emit(self._index, title_for_ui)
        self._emit_status(f"Resolving '{title_for_ui}' from Nostr…")
        self._long_form_fetcher.fetch(
            coord,
            extra_relays=self._user_read_relays,
            on_success=lambda event, t=template, i=item, ui=title_for_ui: (
                self._on_long_form_resolved(event, t, i, ui)
            ),
            on_not_found=lambda t=template, i=item, ui=title_for_ui: (
                self._maybe_recover_full_text(i, t, ui)
            ),
        )

    def _on_long_form_resolved(
        self,
        event: dict,
        template: ArticleTemplate,
        item: FeedItem,
        title_for_ui: str,
    ) -> None:
        if self._cancelled:
            return
        # NIP-23 ``.content`` is already Markdown. Adopt it only when it
        # actually carries more prose than the feed body: an empty or
        # shorter event means the feed text is the better artefact.
        prose = (event.get("content") or "").strip()
        if prose and len(prose) > self._feed_body_length(template, item):
            self._publish_template_as_draft(
                replace(template, content=self._with_footer(prose, item)))
            return
        self._maybe_recover_full_text(item, template, title_for_ui)

    # -- full-text recovery ------------------------------------------------

    def _maybe_recover_full_text(
        self,
        item: FeedItem,
        template: ArticleTemplate,
        title_for_ui: str,
    ) -> None:
        """Fetch a thin item's own page and lift the article body.

        Runs only when enabled, the feed body is a teaser, and the link
        is an ordinary web page. Best-effort at every step: any failure
        publishes the feed-provided template unchanged.
        """
        if self._cancelled:
            return
        if not (
            self._fetch_full_text
            and self._body_is_thin(template, item)
            and _is_http_url(item.link)
        ):
            self._publish_template_as_draft(template)
            return
        self.item_extracting.emit(self._index, title_for_ui)
        self._emit_status(f"Fetching full text for '{title_for_ui}'…")
        self._fetcher.fetch(
            item.link,
            on_success=lambda body, i=item, t=template: (
                self._on_article_page(body, i, t)
            ),
            on_failure=lambda _err, t=template: (
                self._publish_template_as_draft(t)
            ),
        )

    def _on_article_page(
        self,
        body: str,
        item: FeedItem,
        template: ArticleTemplate,
    ) -> None:
        if self._cancelled:
            return
        feed_body_len = self._feed_body_length(template, item)

        def _extract():
            content = extract_readable_content(body, url=item.link)
            if content is None:
                return None
            markdown = html_to_markdown(content.html).strip()
            return (markdown, content.title) if markdown else None

        def _done(result) -> None:
            if self._cancelled:
                return
            if result is not None:
                markdown, page_title = result
                # Adopt only on a clear win over the summary we already
                # have; a bad extraction is usually shorter, not longer.
                if len(markdown) > max(THIN_CONTENT_CHARS, feed_body_len * 2):
                    updated = replace(
                        template, content=self._with_footer(markdown, item))
                    if item.title_from_url and page_title:
                        updated = replace(updated, title=page_title)
                    self._publish_template_as_draft(updated)
                    return
            self._publish_template_as_draft(template)

        self._run_blocking(
            _extract,
            _done,
            lambda _exc, t=template: self._publish_template_as_draft(t),
        )

    # -- image rehosting ---------------------------------------------------

    def _publish_template_as_draft(self, template: ArticleTemplate) -> None:
        """Mirror images (when enabled), then sign + publish."""
        if self._cancelled:
            return
        if self._image_mirror is None:
            self._sign_and_publish(template)
            return

        counts = {"mirrored": 0, "failed": 0}

        def _on_progress(progress: MirrorProgress) -> None:
            if self._cancelled:
                return
            if progress.status == "mirrored":
                counts["mirrored"] += 1
            elif progress.status == "failed":
                counts["failed"] += 1
            self.item_mirroring.emit(
                self._index, counts["mirrored"], counts["failed"],
                progress.total,
            )
            done = counts["mirrored"] + counts["failed"]
            self._emit_status(
                f"Mirroring images {min(done + 1, progress.total)}/"
                f"{progress.total}…"
            )

        def _on_done(outcome) -> None:
            if self._cancelled:
                return
            if outcome.failed:
                total = outcome.mirrored + len(outcome.failed)
                self._emit_status(
                    f"{len(outcome.failed)} of {total} image(s) couldn't be "
                    "mirrored; originals kept."
                )
            self._sign_and_publish(replace(template, content=outcome.markdown))

        rehost_images(
            template.content,
            mirror=self._image_mirror,
            skip_urls=self._skip_image_urls,
            on_progress=_on_progress,
            on_done=_on_done,
            is_cancelled=lambda: self._cancelled,
        )

    # -- sign + publish ----------------------------------------------------

    def _sign_and_publish(self, template: ArticleTemplate) -> None:
        """Build the inner event and kick off the publish job."""
        if self._cancelled:
            return
        extra_tags = (
            [[SOURCE_TAG, self._feed_url]] if self._feed_url else None
        )
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
                extra_tags=extra_tags,
            )
        except ValueError as exc:
            self._record_failure(f"Could not build article: {exc}")
            return

        try:
            job = self._publish_job_factory(
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
            lambda results, _slug=slug_for_signals: (
                self._on_draft_completed(_slug, results)
            )
        )
        job.failed.connect(self._on_draft_failed)
        job.start()

    # -- draft-job callbacks -----------------------------------------------

    def _on_draft_status(self, text: str) -> None:
        # Forward the underlying job's status so the panel can show what
        # the signer / relays are doing without re-deriving it.
        self._emit_status(text)

    def _on_draft_stashed(self, identifier: str) -> None:
        # Stash fires before relay results; the item counts as saved as
        # soon as it is signed (relay publish is best-effort and is
        # reported separately via item_published).
        if self._cancelled:
            return
        self.item_succeeded.emit(self._index, identifier)
        self._succeeded += 1

    def _on_draft_completed(self, _identifier: str, results: object = None) -> None:
        if self._cancelled:
            return
        accepted, total = _relay_acceptance(results)
        if total:
            self.item_published.emit(self._index, accepted, total)
        self._release_current_job()
        self.progress.emit(self._index + 1, len(self._items))
        self._index += 1
        self._start_next_item()

    def _on_draft_failed(self, reason: str) -> None:
        if self._cancelled:
            return
        self._record_failure(reason)

    def _record_failure(self, reason: str) -> None:
        """Per-item failure path. Increments index and moves on."""
        self._release_current_job()
        self.item_failed.emit(self._index, reason)
        self.progress.emit(self._index + 1, len(self._items))
        self._index += 1
        self._start_next_item()

    def _release_current_job(self) -> None:
        """Detach the settled job.

        The job stays Qt-parented to this importer, so its memory is
        bounded by the importer's own lifetime (the panel releases the
        importer after every run). Its signals are disconnected so a
        misbehaving job that emitted twice could never corrupt the
        batch accounting. Deliberately NOT ``deleteLater``: queueing
        deletion of a sender from inside its own signal stack is
        fragile when the parent chain is torn down by the Python GC
        (crashes observed under test harnesses), and the bounded
        per-run lifetime already prevents the long-lived leak.
        """
        job = self._current_job
        if job is None:
            return
        self._current_job = None
        for signal_name in ("status_changed", "stashed", "completed", "failed"):
            signal = getattr(job, signal_name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass  # already disconnected / no connections


def _is_http_url(link: Optional[str]) -> bool:
    """``True`` for an http(s) web URL we can fetch an article page from."""
    if not link:
        return False
    lowered = link.strip().lower()
    return lowered.startswith("https://") or lowered.startswith("http://")


def _relay_acceptance(results: object) -> Tuple[int, int]:
    """(accepted, total) from a ``DraftPublishJob.completed`` payload.

    The payload is a list of ``(relay, ok, message)`` tuples. Defensive:
    malformed rows count toward the total but never toward accepted.
    """
    if not isinstance(results, (list, tuple)):
        return (0, 0)
    accepted = 0
    for row in results:
        try:
            if row[1]:
                accepted += 1
        except (TypeError, IndexError, KeyError):
            continue
    return accepted, len(results)
