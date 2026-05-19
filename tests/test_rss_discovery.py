"""Unit tests for ``nostr.rss.discovery``."""

from __future__ import annotations

import textwrap
import unittest

from nostr.rss.discovery import (
    COMMON_FEED_PATHS,
    FeedHint,
    candidate_root_feed,
    extract_feeds_from_html,
    looks_like_html,
    normalize_user_url,
)


def _wordpress_head() -> str:
    return textwrap.dedent("""\
        <!doctype html>
        <html>
        <head>
            <title>Alice's Blog</title>
            <link rel="alternate" type="application/rss+xml"
                  title="Alice's Blog &raquo; Feed" href="/feed/" />
            <link rel="alternate" type="application/rss+xml"
                  title="Comments Feed" href="/comments/feed/" />
            <meta charset="utf-8" />
        </head>
        <body>
            <h1>Posts</h1>
        </body>
        </html>
    """)


def _atom_head() -> str:
    return textwrap.dedent("""\
        <html>
        <head>
            <link rel="alternate" type="application/atom+xml" href="/atom.xml" />
        </head>
        <body></body>
        </html>
    """)


def _jsonfeed_head() -> str:
    return textwrap.dedent("""\
        <html>
        <head>
            <link rel="alternate" type="application/feed+json"
                  title="JSON Feed" href="https://other.example.com/feed.json" />
        </head>
        <body></body>
        </html>
    """)


def _multi_feed_head() -> str:
    return textwrap.dedent("""\
        <html>
        <head>
            <link rel="alternate" type="application/rss+xml" href="/rss/" />
            <link rel="alternate" type="application/atom+xml" href="/atom/" />
            <link rel="alternate" type="application/feed+json" href="/feed.json" />
        </head>
        </html>
    """)


def _no_feed_head() -> str:
    return textwrap.dedent("""\
        <html>
        <head>
            <title>Login</title>
            <link rel="canonical" href="https://example.com/login" />
            <link rel="alternate" hreflang="de" href="/de/" />
            <link rel="alternate" type="image/png" href="/icon.png" />
        </head>
        <body><form>...</form></body>
        </html>
    """)


class NormalizeUserUrlTests(unittest.TestCase):
    def test_passes_through_https(self) -> None:
        self.assertEqual(
            normalize_user_url("https://example.com/feed/"),
            "https://example.com/feed/",
        )

    def test_passes_through_http(self) -> None:
        self.assertEqual(
            normalize_user_url("http://example.com/feed/"),
            "http://example.com/feed/",
        )

    def test_adds_https_to_bare_domain(self) -> None:
        self.assertEqual(
            normalize_user_url("example.com"),
            "https://example.com",
        )

    def test_adds_https_to_domain_with_path(self) -> None:
        self.assertEqual(
            normalize_user_url("example.com/feed/"),
            "https://example.com/feed/",
        )

    def test_localhost_defaults_to_http(self) -> None:
        self.assertEqual(
            normalize_user_url("localhost:5006/r/blog"),
            "http://localhost:5006/r/blog",
        )

    def test_loopback_ip_defaults_to_http(self) -> None:
        self.assertEqual(
            normalize_user_url("127.0.0.1:8080/feed"),
            "http://127.0.0.1:8080/feed",
        )

    def test_protocol_relative_url(self) -> None:
        self.assertEqual(
            normalize_user_url("//example.com/feed"),
            "https://example.com/feed",
        )

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            normalize_user_url("   example.com   "),
            "https://example.com",
        )

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(normalize_user_url(""), "")
        self.assertEqual(normalize_user_url("   "), "")


class LooksLikeHtmlTests(unittest.TestCase):
    def test_detects_doctype_html(self) -> None:
        self.assertTrue(looks_like_html("<!doctype html><html>..."))

    def test_detects_html_tag(self) -> None:
        self.assertTrue(looks_like_html("<html><head></head></html>"))

    def test_detects_html_with_leading_whitespace(self) -> None:
        self.assertTrue(looks_like_html("\n\n  <!DOCTYPE HTML>"))

    def test_detects_head_only_fragment(self) -> None:
        self.assertTrue(looks_like_html("<head><title>x</title>"))

    def test_rss_xml_is_not_html(self) -> None:
        self.assertFalse(looks_like_html('<?xml version="1.0"?><rss><channel>'))

    def test_atom_is_not_html(self) -> None:
        self.assertFalse(looks_like_html("<feed xmlns='http://www.w3.org/2005/Atom'>"))

    def test_json_feed_is_not_html(self) -> None:
        self.assertFalse(looks_like_html('{"version":"https://jsonfeed.org/v/1.1"}'))

    def test_empty_is_not_html(self) -> None:
        self.assertFalse(looks_like_html(""))


class ExtractFeedsFromHtmlTests(unittest.TestCase):
    def test_finds_rss_link_rel(self) -> None:
        hints = extract_feeds_from_html(
            _wordpress_head(),
            base_url="https://alice.example.com/post/foo/",
        )
        self.assertGreaterEqual(len(hints), 1)
        self.assertEqual(hints[0].url, "https://alice.example.com/feed/")
        self.assertEqual(hints[0].mime_type, "application/rss+xml")

    def test_preserves_document_order_with_multiple_feeds(self) -> None:
        hints = extract_feeds_from_html(
            _multi_feed_head(),
            base_url="https://example.com/",
        )
        self.assertEqual(
            [h.url for h in hints],
            [
                "https://example.com/rss/",
                "https://example.com/atom/",
                "https://example.com/feed.json",
            ],
        )

    def test_resolves_absolute_href_unchanged(self) -> None:
        hints = extract_feeds_from_html(
            _jsonfeed_head(),
            base_url="https://example.com/page",
        )
        self.assertEqual(hints[0].url, "https://other.example.com/feed.json")

    def test_extracts_title(self) -> None:
        hints = extract_feeds_from_html(
            _wordpress_head(),
            base_url="https://alice.example.com/",
        )
        self.assertIn("Alice", hints[0].title or "")

    def test_atom_link_rel_picked_up(self) -> None:
        hints = extract_feeds_from_html(
            _atom_head(),
            base_url="https://example.com/",
        )
        self.assertEqual(hints[0].mime_type, "application/atom+xml")

    def test_ignores_non_feed_alternates(self) -> None:
        hints = extract_feeds_from_html(
            _no_feed_head(),
            base_url="https://example.com/",
        )
        self.assertEqual(hints, [])

    def test_ignores_links_in_body(self) -> None:
        html = (
            "<html><head></head><body>"
            "<link rel='alternate' type='application/rss+xml' href='/feed/'>"
            "</body></html>"
        )
        self.assertEqual(
            extract_feeds_from_html(html, base_url="https://example.com/"),
            [],
        )

    def test_handles_malformed_html_gracefully(self) -> None:
        broken = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed/">'
        hints = extract_feeds_from_html(broken, base_url="https://example.com/")
        self.assertEqual(hints[0].url, "https://example.com/feed/")

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(extract_feeds_from_html("", base_url="https://example.com/"), [])

    def test_ignores_alternate_without_explicit_feed_mime(self) -> None:
        # rel=alternate without a feed-shaped type is almost always an
        # hreflang pointer or another resource link. Skip it; the
        # importer's common-path fallback handles truly typeless feeds.
        html = (
            '<html><head><link rel="alternate" href="/feed/"></head></html>'
        )
        self.assertEqual(
            extract_feeds_from_html(html, base_url="https://example.com/"),
            [],
        )


class CandidateRootFeedTests(unittest.TestCase):
    def test_returns_feed_path_for_simple_url(self) -> None:
        self.assertEqual(
            candidate_root_feed("https://example.com/some/article/"),
            "https://example.com/feed/",
        )

    def test_preserves_scheme(self) -> None:
        self.assertEqual(
            candidate_root_feed("http://example.com/blog/"),
            "http://example.com/feed/",
        )

    def test_preserves_port(self) -> None:
        self.assertEqual(
            candidate_root_feed("http://localhost:5006/r/blog/post/"),
            "http://localhost:5006/feed/",
        )

    def test_returns_none_for_unparseable_input(self) -> None:
        self.assertIsNone(candidate_root_feed(""))
        self.assertIsNone(candidate_root_feed("not a url"))

    def test_common_feed_paths_first_entry_is_feed_slash(self) -> None:
        # candidate_root_feed uses the first entry of COMMON_FEED_PATHS;
        # the constant ordering is part of the public contract.
        self.assertEqual(COMMON_FEED_PATHS[0], "/feed/")


if __name__ == "__main__":
    unittest.main()
