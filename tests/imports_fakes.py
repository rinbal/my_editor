# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fakes for the importer test-suite.

Everything settles synchronously so orchestration tests are
deterministic without an event loop, network, relays, or a signer.
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from nostr.imports.errors import ERROR_CODES, SourceError
from nostr.rss.parser import FeedItem


PUBKEY = "ab" * 32

PROFILE = SimpleNamespace(
    user_pubkey=PUBKEY,
    bunker_relays=["wss://bunker.example"],
)

RESULTS_OK = [("wss://a.example", True, ""), ("wss://b.example", False, "err")]


# Long enough that a default item is never "thin" (THIN_CONTENT_CHARS),
# so tests only exercise the recovery paths when they opt in.
_DEFAULT_BODY_HTML = (
    "<p>Hello world, this is a perfectly ordinary body with more than "
    "enough prose in it to clear the thin-content threshold easily.</p>"
)


def make_item(
    title="Post",
    *,
    guid=None,
    link=None,
    summary=None,
    content_html=_DEFAULT_BODY_HTML,
    published_at=None,
    categories=(),
    image=None,
    author=None,
    title_from_url=False,
    content_markdown=None,
) -> FeedItem:
    return FeedItem(
        guid=guid if guid is not None else title,
        title=title,
        link=link,
        summary=summary,
        content_html=content_html,
        published_at=published_at,
        categories=tuple(categories),
        image=image,
        author=author,
        title_from_url=title_from_url,
        content_markdown=content_markdown,
    )


def rss_feed(items):
    """items: list of dicts with title/link/guid/description/pubdate."""
    rows = []
    for it in items:
        fields = [f"<title>{it['title']}</title>"]
        if it.get("link"):
            fields.append(f"<link>{it['link']}</link>")
        if it.get("guid"):
            fields.append(f"<guid isPermaLink=\"false\">{it['guid']}</guid>")
        fields.append(
            f"<description>{it.get('description', 'body text')}</description>")
        if it.get("pubdate"):
            fields.append(f"<pubDate>{it['pubdate']}</pubDate>")
        rows.append("<item>" + "".join(fields) + "</item>")
    return (
        "<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>"
        "<title>My Blog</title><link>https://example.com</link>"
        "<description>test blog</description>"
        + "".join(rows)
        + "</channel></rss>"
    )


TWO_ITEM_FEED = rss_feed([
    {"title": "First", "link": "https://example.com/a", "guid": "g1",
     "pubdate": "Mon, 01 Jan 2024 00:00:00 GMT"},
    {"title": "Second", "link": "https://example.com/b", "guid": "g2",
     "pubdate": "Tue, 02 Jan 2024 00:00:00 GMT"},
])

HTML_WITH_HINT = (
    "<!doctype html><html><head>"
    "<link rel=\"alternate\" type=\"application/rss+xml\" href=\"/feed.xml\">"
    "</head><body>welcome</body></html>"
)

HTML_NO_HINT = "<!doctype html><html><head></head><body>welcome</body></html>"


class FakeFetcher:
    """Synchronous fetcher: url -> ("ok", body) | ("err", reason).

    Unknown URLs answer like a 404 so discovery probes against
    unregistered paths fail through naturally, as they would live.
    """

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def fetch(self, url, *, on_success, on_failure):
        self.calls.append(url)
        kind, payload = self.responses.get(url, ("err", "HTTP 404"))
        if kind == "ok":
            on_success(payload)
        else:
            on_failure(SourceError(payload, ERROR_CODES.FETCH_ERROR))


class ManualFetcher:
    """Fetcher that parks its callbacks so tests control settle timing."""

    def __init__(self):
        self.calls = []
        self.pending = []  # (url, on_success, on_failure)

    def fetch(self, url, *, on_success, on_failure):
        self.calls.append(url)
        self.pending.append((url, on_success, on_failure))


class FakeRelayListCache:
    def __init__(self, read=("wss://read.example",)):
        self._relay_list = SimpleNamespace(read=list(read), write=[])
        self.calls = []

    def fetch(self, pubkey, relays=None, on_done=None):
        self.calls.append((pubkey, tuple(relays or ())))
        on_done(self._relay_list)


class FakeLongFormFetcher:
    """event=None means not-found; otherwise every fetch resolves it."""

    def __init__(self, event=None):
        self.event = event
        self.calls = []

    def fetch(self, coord, *, extra_relays, on_success, on_not_found, **_kw):
        self.calls.append((coord, tuple(extra_relays)))
        if self.event is not None:
            on_success(self.event)
        else:
            on_not_found()


class FakePublishJob(QObject):
    """Mimics DraftPublishJob's signal surface, settling synchronously."""

    status_changed = Signal(str)
    stashed = Signal(str, str, int)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, *, outcome, inner_event=None, identifier=None,
                 parent=None, **_ignored):
        super().__init__(parent)
        self.inner_event = inner_event
        self.identifier = identifier
        self.cancelled = False
        self._outcome = outcome

    def start(self):
        kind, payload = self._outcome
        if kind == "ok":
            self.stashed.emit(self.identifier, "ev-" + self.identifier, 1234)
            self.completed.emit(payload)
        elif kind == "fail":
            self.failed.emit(payload)
        # "pending": never settles; used by cancellation tests.

    def cancel(self):
        self.cancelled = True


def make_factory(outcomes=None):
    """Publish-job factory yielding scripted outcomes, oldest first.

    Returns (factory, created_jobs). Default outcome is success with
    ``RESULTS_OK`` once the script is exhausted.
    """
    script = list(outcomes or [])
    created = []

    def factory(**kwargs):
        outcome = script.pop(0) if script else ("ok", RESULTS_OK)
        job = FakePublishJob(outcome=outcome, **kwargs)
        created.append(job)
        return job

    return factory, created


def inline_run_blocking(fn, on_done, on_error):
    """Executor with run_blocking's shape that runs inline."""
    try:
        result = fn()
    except BaseException as exc:  # noqa: BLE001, routed per contract
        on_error(exc)
        return
    on_done(result)


class RecordingPacer:
    """Pacer seam: records delays; runs immediately or parks callbacks."""

    def __init__(self, immediate=True):
        self.immediate = immediate
        self.calls = []
        self.pending = []

    def __call__(self, ms, fn):
        self.calls.append(ms)
        if self.immediate:
            fn()
        else:
            self.pending.append(fn)
