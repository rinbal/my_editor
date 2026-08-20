# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.imports``: registry, errors, RSS resolver.

Everything here is pure or driven by a synchronous fake fetcher, so no
Qt event loop and no network. Guards:

- resolver matching (detect / can_resolve / unsupported input),
- the pasted-body path (parse directly, no fetch),
- the error taxonomy and its friendly-copy mapping,
- resolver-level cancellation short-circuiting network work.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.imports.errors import ERROR_CODES, SourceError, friendly_message
from nostr.imports.registry import (
    ResolveInput,
    can_resolve_source,
    detect_resolver,
    resolve_source,
)


VALID_RSS = (
    "<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>"
    "<title>Pasted Blog</title><link>https://example.com</link>"
    "<description>d</description>"
    "<item><title>Post</title><guid>g1</guid>"
    "<description>body</description></item>"
    "</channel></rss>"
)


class FakeFetcher:
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


class Sink:
    def __init__(self):
        self.result = None
        self.error = None
        self.stages = []

    def on_success(self, result):
        assert self.result is None and self.error is None, "settled twice"
        self.result = result

    def on_failure(self, error):
        assert self.result is None and self.error is None, "settled twice"
        self.error = error

    def on_stage(self, stage):
        self.stages.append(stage["name"])


def run_resolve(value, fetcher=None, is_cancelled=lambda: False):
    sink = Sink()
    resolve_source(
        value,
        fetcher=fetcher or FakeFetcher(),
        on_success=sink.on_success,
        on_failure=sink.on_failure,
        on_stage=sink.on_stage,
        is_cancelled=is_cancelled,
    )
    return sink


class TestDetection(unittest.TestCase):
    def test_url_input_matches_rss_resolver(self):
        resolver = detect_resolver("https://example.com/feed/")
        self.assertIsNotNone(resolver)
        self.assertEqual(resolver.id, "rss")

    def test_pasted_body_matches_rss_resolver(self):
        resolver = detect_resolver(ResolveInput(pasted_body=VALID_RSS))
        self.assertIsNotNone(resolver)
        self.assertEqual(resolver.id, "rss")

    def test_markup_as_url_is_rejected(self):
        self.assertFalse(can_resolve_source("<?xml version=\"1.0\"?><rss>"))

    def test_bare_word_is_rejected(self):
        self.assertFalse(can_resolve_source("feed"))

    def test_empty_input_is_rejected(self):
        self.assertFalse(can_resolve_source(""))
        self.assertFalse(can_resolve_source(None))
        self.assertFalse(can_resolve_source(ResolveInput()))


class TestUnsupportedSource(unittest.TestCase):
    def test_resolve_of_unsupported_input_fails_with_code(self):
        sink = run_resolve(ResolveInput())
        self.assertIsNotNone(sink.error)
        self.assertEqual(sink.error.code, ERROR_CODES.UNSUPPORTED_SOURCE)


class TestPastedBody(unittest.TestCase):
    def test_pasted_feed_parses_without_network(self):
        fetcher = FakeFetcher()
        sink = run_resolve(ResolveInput(pasted_body=VALID_RSS), fetcher)
        self.assertEqual(fetcher.calls, [])
        self.assertIsNotNone(sink.result)
        self.assertEqual(sink.result.feed.title, "Pasted Blog")
        self.assertEqual(len(sink.result.feed.items), 1)
        self.assertIn("done", sink.stages)

    def test_pasted_garbage_fails_as_not_a_feed(self):
        sink = run_resolve(ResolveInput(pasted_body="complete garbage"))
        self.assertIsNotNone(sink.error)
        self.assertEqual(sink.error.code, ERROR_CODES.NOT_A_FEED)


class TestUrlResolution(unittest.TestCase):
    def test_direct_feed_url_resolves(self):
        fetcher = FakeFetcher({"https://example.com/feed/": ("ok", VALID_RSS)})
        sink = run_resolve("https://example.com/feed/", fetcher)
        self.assertIsNotNone(sink.result)
        self.assertEqual(sink.result.url, "https://example.com/feed/")

    def test_network_only_failure_keeps_transport_reason(self):
        fetcher = FakeFetcher({"https://example.com/feed/": ("err", "Host not found")})
        sink = run_resolve("https://example.com/feed/", fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.FETCH_ERROR)
        self.assertIn("Host not found", sink.error.message)

    def test_cancelled_resolution_issues_no_fetches(self):
        fetcher = FakeFetcher({"https://example.com/feed/": ("ok", VALID_RSS)})
        sink = run_resolve("https://example.com/feed/", fetcher,
                           is_cancelled=lambda: True)
        self.assertEqual(fetcher.calls, [])
        self.assertIsNone(sink.result)
        self.assertIsNone(sink.error)


class TestRssDiscoveryFlow(unittest.TestCase):
    """The discovery BFS, driven at resolver level via the registry."""

    def _fixtures(self):
        from tests.imports_fakes import (
            HTML_NO_HINT, HTML_WITH_HINT, TWO_ITEM_FEED, FakeFetcher,
        )
        return HTML_NO_HINT, HTML_WITH_HINT, TWO_ITEM_FEED, FakeFetcher

    def test_follows_alternate_hint(self):
        _, html_hint, feed, FakeFetcher = self._fixtures()
        fetcher = FakeFetcher({
            "https://example.com": ("ok", html_hint),
            "https://example.com/feed.xml": ("ok", feed),
        })
        sink = run_resolve("https://example.com", fetcher)
        self.assertEqual(fetcher.calls,
                         ["https://example.com", "https://example.com/feed.xml"])
        self.assertIsNotNone(sink.result)
        self.assertEqual(sink.result.url, "https://example.com/feed.xml")
        self.assertEqual(len(sink.result.feed.items), 2)

    def test_first_hint_failure_falls_through_to_next_hint(self):
        _, _, feed, FakeFetcher = self._fixtures()
        html = (
            "<!doctype html><html><head>"
            "<link rel=\"alternate\" type=\"application/rss+xml\" href=\"/a.xml\">"
            "<link rel=\"alternate\" type=\"application/atom+xml\" href=\"/b.xml\">"
            "</head><body></body></html>"
        )
        fetcher = FakeFetcher({
            "https://example.com": ("ok", html),
            "https://example.com/a.xml": ("err", "HTTP 500"),
            "https://example.com/b.xml": ("ok", feed),
        })
        sink = run_resolve("https://example.com", fetcher)
        self.assertEqual(fetcher.calls, [
            "https://example.com",
            "https://example.com/a.xml",
            "https://example.com/b.xml",
        ])
        self.assertIsNotNone(sink.result)

    def test_palette_probes_pasted_path_before_origin(self):
        html_no_hint, _, feed, FakeFetcher = self._fixtures()
        fetcher = FakeFetcher({
            "https://example.com/blog/post": ("ok", html_no_hint),
            "https://example.com/blog/post/feed/": ("ok", feed),
        })
        sink = run_resolve("https://example.com/blog/post", fetcher)
        self.assertEqual(fetcher.calls, [
            "https://example.com/blog/post",
            "https://example.com/blog/post/feed/",
        ])
        self.assertIsNotNone(sink.result)

    def test_gives_up_cleanly_when_every_candidate_is_html(self):
        html_no_hint, _, _, FakeFetcher = self._fixtures()
        fetcher = FakeFetcher({
            "https://example.com": ("ok", html_no_hint),
            "https://example.com/feed/": ("ok", html_no_hint),
        })
        sink = run_resolve("https://example.com", fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.NO_FEED_FOUND)
        # The origin plus the full 8-path palette, each tried exactly
        # once. The /feed/ HTML answer (a soft-404 page) must NOT spawn
        # deeper palette probes of its own.
        self.assertEqual(fetcher.calls[0], "https://example.com")
        self.assertEqual(fetcher.calls[1], "https://example.com/feed/")
        self.assertEqual(len(set(fetcher.calls)), len(fetcher.calls))
        discovery_calls = fetcher.calls[:9]
        self.assertEqual(len(set(discovery_calls)), 9)
        # After discovery exhausts, only the sitemap fallback probes
        # remain (robots.txt + the conventional sitemap paths).
        tail = fetcher.calls[9:]
        self.assertEqual(tail[0], "https://example.com/robots.txt")
        self.assertTrue(all("sitemap" in u or "robots" in u for u in tail))

    def test_hint_pointing_back_at_page_does_not_loop(self):
        _, _, _, FakeFetcher = self._fixtures()
        html_self_hint = (
            "<!doctype html><html><head>"
            "<link rel=\"alternate\" type=\"application/rss+xml\" "
            "href=\"https://example.com\">"
            "</head><body></body></html>"
        )
        fetcher = FakeFetcher({"https://example.com": ("ok", html_self_hint)})
        sink = run_resolve("https://example.com", fetcher)
        self.assertIsNotNone(sink.error)
        # The self-referencing hint is never refetched.
        self.assertEqual(
            fetcher.calls.count("https://example.com"), 1)

    def test_garbage_body_yields_not_a_feed(self):
        _, _, _, FakeFetcher = self._fixtures()
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", "garbage")})
        sink = run_resolve("https://example.com/feed", fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.NOT_A_FEED)


class TestFriendlyMessages(unittest.TestCase):
    def test_fetch_error_embeds_reason(self):
        err = SourceError("Host not found.", ERROR_CODES.FETCH_ERROR)
        self.assertEqual(
            friendly_message(err),
            "Couldn't reach that URL: Host not found.",
        )

    def test_mapped_codes_use_fixed_copy(self):
        err = SourceError("raw detail", ERROR_CODES.NO_FEED_FOUND)
        self.assertIn("No feed found", friendly_message(err))
        err = SourceError("raw detail", ERROR_CODES.NOT_A_FEED)
        self.assertIn("isn't a feed", friendly_message(err))

    def test_no_first_person_plural_in_friendly_copy(self):
        # Platform writing guidance: never "we" in user-facing errors.
        from nostr.imports.errors import _FRIENDLY
        for code, copy in _FRIENDLY.items():
            lowered = f" {copy.lower()} "
            self.assertNotIn(" we ", lowered, code)
            self.assertNotIn(" we'", lowered, code)
            self.assertNotIn(" our ", lowered, code)

    def test_unknown_code_falls_back_to_raw_message(self):
        err = SourceError("Feed exceeds the 16 MiB size limit", ERROR_CODES.TOO_LARGE)
        self.assertEqual(friendly_message(err),
                         "Feed exceeds the 16 MiB size limit")

    def test_empty_message_never_yields_empty_copy(self):
        err = SourceError("", ERROR_CODES.UNKNOWN)
        self.assertTrue(friendly_message(err))


if __name__ == "__main__":
    unittest.main()
