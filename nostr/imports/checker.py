# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check the feeds a person follows, while MyEditor runs.

EINUNDZWANZIG STANDUP checks feeds on its server every 15 minutes. This
app has no server, and the owner chose no operating-system scheduler
either: feeds are checked only while MyEditor is open, at launch and
then on a timer, and only while Nostr is in use with an account
(the controller starts and stops the checker; nothing here touches the
network before that).

The rhythm is STANDUP's (backend/core/connected_sources.py):

- at launch, ten seconds after the account is ready, every source that
  is due; then a tick every minute picks the due sources, oldest first,
  two at a time at most;
- after a check, the next one in 15 minutes (longer above 50 automatic
  sources, so a long list stays polite); after a failure 30 minutes, an
  hour, two... at most a day (inbox_store.record_failure);
- a check of a source holds a lease, so it never runs twice at once; the
  person can ask for one at most once a minute;
- a conditional request (ETag, Last-Modified) lets an unchanged feed
  answer 304 without a download;
- offline, a tick is skipped without counting a failure.

Only feeds are checked automatically (RSS, Atom, JSON Feed: the RSS
resolver's sources). Nostr authors, sitemaps, GitHub folders and the
like are read when the person opens them, as in STANDUP. What a check
finds goes to the inbox (inbox_store.ingest), where the first check of
a source is a baseline. Every fetch passes the network guard
(netguard.py), through the fetcher.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import _

from ..rss.discovery import looks_like_html
from ..rss.parser import RssError, parse_feed, with_whole_links
from . import workers
from .errors import ERROR_CODES, SourceError, failure_words
from .inbox_store import CHECK_INTERVAL, InboxStore, SourceRow
from .registry import ResolveInput, detect_resolver, resolve_source
from .sources.podcast import enrich_feed_with_podcast

_log = logging.getLogger(__name__)

LAUNCH_DELAY_MS = 10_000
TICK_MS = 60_000
CONCURRENT = 2
# The owner's decision Q-8: at most about 200 checks an hour, however many
# sources are followed and however many are due at launch.
CHECKS_PER_HOUR = 200
SOURCES_PER_INTERVAL = 50
FEED_FORMATS = ("rss", "atom", "jsonfeed")

# A source's one quiet line after a check that did not work (lessons 36).
TOO_LARGE = _("This feed is too large to check: import its posts from an export file instead.")
CHECK_FAILED = _("This feed couldn't be checked: MyEditor tries again on its own.")


def checks_automatically(url: str) -> bool:
    """Whether a source is a feed this app checks while it runs (the RSS
    resolver's, over http or https); every other source is read when the
    person opens it."""
    resolver = detect_resolver(url)
    return resolver is not None and resolver.id == "rss" and (
        url.strip().lower().startswith(("https://", "http://")))


def interval_for(automatic_sources: int) -> int:
    """Seconds until a source's next check: 15 minutes, stretched above
    50 automatic sources so a long list is not fetched every quarter hour."""
    return CHECK_INTERVAL * max(1, math.ceil(automatic_sources / SOURCES_PER_INTERVAL))


def error_text(error: SourceError) -> str:
    """The words a source shows after a failed check."""
    if error.code == ERROR_CODES.TOO_LARGE:
        return TOO_LARGE
    if error.code in (ERROR_CODES.LOCAL_NETWORK, ERROR_CODES.NOT_A_FEED,
                      ERROR_CODES.NO_FEED_FOUND, ERROR_CODES.FETCH_ERROR):
        return failure_words(error)
    return CHECK_FAILED


def _default_online() -> bool:
    try:
        from PySide6.QtNetwork import QNetworkInformation
        if QNetworkInformation.instance() is None:
            QNetworkInformation.loadDefaultBackend()
        info = QNetworkInformation.instance()
        if info is None:
            return True
        return info.reachability() in (QNetworkInformation.Reachability.Online,
                                       QNetworkInformation.Reachability.Unknown)
    except Exception:  # noqa: BLE001, no backend: assume online
        return True


class FeedChecker(QObject):
    """Checks one account's automatic sources while running.

    Signals:
      checking(str, bool)        a source's check started or ended
      source_checked(str, int)   a check ended: the source, posts new in
                                 the Inbox (0 after a failure)
    """

    checking = Signal(str, bool)
    source_checked = Signal(str, int)

    def __init__(self, *, store: InboxStore, fetcher,
                 run_blocking: Optional[Callable[..., None]] = None,
                 online: Callable[[], bool] = _default_online,
                 scheduler: Optional[Callable[[int, Callable[[], None]], None]] = None,
                 tick_ms: int = TICK_MS, launch_delay_ms: int = LAUNCH_DELAY_MS,
                 clock: Callable[[], float] = time.monotonic,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._fetcher = fetcher
        self._run_blocking = run_blocking or (
            lambda fn, ok, err: workers.run_blocking(fn, ok, err, parent=self))
        self._online = online
        self._schedule = scheduler or (lambda ms, fn: QTimer.singleShot(ms, self, fn))
        self._launch_delay_ms = launch_delay_ms
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self.tick)
        self._running = False
        self._generation = 0
        self._in_flight: Dict[str, int] = {}
        self._clock = clock
        # When the checks of the last hour started (for the hourly budget).
        self._started: Deque[float] = deque()

    # -- control ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Check what is due shortly after launch, then every minute."""
        if self._running:
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        self._schedule(self._launch_delay_ms,
                       lambda: generation == self._generation and self.tick())
        self._timer.start()

    def stop(self) -> None:
        """Stop checking; checks under way end without a result."""
        self._running = False
        self._generation += 1
        self._timer.stop()
        for key in list(self._in_flight):
            self._store.release(key)
            self.checking.emit(key, False)
        self._in_flight.clear()

    def is_checking(self, key: str) -> bool:
        return key in self._in_flight

    def check_now(self, key: str) -> bool:
        """The person asked: check ``key`` now (at most once a minute).
        False when it is not allowed yet, or already under way."""
        if not self._running or key in self._in_flight:
            return False
        if not self._store.claim(key, manual=True):
            return False
        source = self._store.source(key)
        if source is None:
            return False
        self._check(source)
        return True

    def tick(self) -> None:
        """Start the checks that are due, while there is room."""
        if not self._running or not self._online():
            return
        room = min(CONCURRENT - len(self._in_flight), self._budget_left())
        if room <= 0:
            return
        for source in self._store.due_sources(room):
            if source.key in self._in_flight or not self._store.claim(source.key):
                continue
            self._started.append(self._clock())
            self._check(source)

    def _budget_left(self) -> int:
        """Checks this hour may still start (Q-8). A person's own Check Now
        is not counted against it."""
        hour_ago = self._clock() - 3600
        while self._started and self._started[0] <= hour_ago:
            self._started.popleft()
        return CHECKS_PER_HOUR - len(self._started)

    # -- one check ---------------------------------------------------------------

    def _interval(self) -> int:
        # Sources whose automatic checks are off add no checks (Q-8).
        automatic = sum(1 for s in self._store.sources() if s.automatic and not s.paused)
        return interval_for(automatic)

    def _check(self, source: SourceRow) -> None:
        generation = self._generation
        key = source.key
        self._in_flight[key] = generation
        self.checking.emit(key, True)
        # Where the feed moved to, but the followed address again after a
        # failure: a temporary redirect must not replace it for good
        # (review L11).
        url = source.url if source.failures else (source.final_url or source.url)

        def live() -> bool:
            return generation == self._generation and self._in_flight.get(key) == generation

        def on_body(text: str, meta: dict) -> None:
            if not live():
                return
            if looks_like_html(text):
                self._discover(source, live)
                return
            self._run_blocking(
                lambda: with_whole_links(enrich_feed_with_podcast(parse_feed(text), text),
                                         meta.get("final_url", "") or url),
                lambda feed: live() and self._ingest(
                    key, feed, etag=meta.get("etag", ""),
                    last_modified=meta.get("last_modified", ""),
                    final_url=meta.get("final_url", "") or url),
                lambda exc: live() and self._fail(key, _as_error(exc)))

        def on_not_modified() -> None:
            if live():
                try:
                    self._store.record_unchanged(key, interval=self._interval())
                except Exception:  # noqa: BLE001, the slot must come back
                    _log.warning("could not record an unchanged feed", exc_info=True)
                finally:
                    self._done(key, 0)

        self._fetcher.fetch_feed(
            url, etag=source.etag, last_modified=source.last_modified,
            on_body=on_body, on_not_modified=on_not_modified,
            on_failure=lambda error: live() and self._fail(key, error))

    def _discover(self, source: SourceRow, live: Callable[[], bool]) -> None:
        """The address is a web page: find its feed (the importer's own
        discovery), and remember the feed's address for the next check."""
        def found(result) -> None:
            if not live():
                return
            if result.feed.format not in FEED_FORMATS:
                self._fail(source.key, SourceError("", ERROR_CODES.NO_FEED_FOUND))
                return
            self._ingest(source.key, result.feed, final_url=result.url)

        resolve_source(ResolveInput(url=source.url), fetcher=self._fetcher,
                       on_success=found,
                       on_failure=lambda error: live() and self._fail(source.key, error),
                       is_cancelled=lambda: not live(), run_blocking=self._run_blocking)

    def _ingest(self, key: str, feed, *, etag: str = "", last_modified: str = "",
                final_url: str = "") -> None:
        new = 0
        try:
            new = self._store.ingest(
                key, list(feed.items), interval=self._interval(), etag=etag,
                last_modified=last_modified, final_url=final_url,
                feed_title=feed.title or "", site_url=feed.link or "").new
        except Exception:  # noqa: BLE001, a locked or full disk must not hold the slot
            _log.warning("could not store a checked feed", exc_info=True)
            self._record_failure(key, SourceError("", ERROR_CODES.UNKNOWN))
        finally:
            self._done(key, new)

    def _fail(self, key: str, error: SourceError) -> None:
        try:
            self._record_failure(key, error)
        finally:
            self._done(key, 0)

    def _record_failure(self, key: str, error: SourceError) -> None:
        try:
            self._store.record_failure(key, error_text(error))
        except Exception:  # noqa: BLE001
            _log.warning("could not record a failed check", exc_info=True)

    def _done(self, key: str, new: int) -> None:
        """A check ended, whatever happened: its slot comes back (review M4:
        a write that raised kept it for good, and two stopped every check)."""
        self._in_flight.pop(key, None)
        self.checking.emit(key, False)
        self.source_checked.emit(key, new)
        # A free slot: the next due source need not wait for the minute,
        # but goes on the next turn of the event loop, never inside this
        # call (a run of failures answered at once nested deeper and deeper).
        generation = self._generation
        self._schedule(0, lambda: generation == self._generation and self.tick())


def _as_error(exc: BaseException) -> SourceError:
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, RssError):
        return SourceError(str(exc), ERROR_CODES.NOT_A_FEED)
    return SourceError(str(exc), ERROR_CODES.UNKNOWN)
