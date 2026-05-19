"""Integration tests that walk end-to-end through the pure layers.

These tests don't touch the network or Qt event loop. They verify:

- The full pipeline (parse → normalize → coord detection) produces what
  we expect on three representative feed shapes:
  (a) a regular WordPress-style RSS feed (no Nostr involvement),
  (b) a Habla / Yakihonne / Pareto-style feed with thin bodies and
      ``nostr:naddr`` URI links,
  (c) a feed whose item link is an HTTP URL with an embedded naddr.
- The "thin body" threshold actually fires on (b) and (c), and doesn't
  fire on (a).
- ``source_link_footer`` is computed identically in importer and
  normalize layers (regression guard against the rename refactor).
"""

from __future__ import annotations

import textwrap
import unittest

from nostr.bech32 import encode_naddr
from nostr.rss.normalize import (
    item_to_article,
    source_link_footer,
)
from nostr.rss.nostr_resolver import (
    extract_nostr_coord,
    is_nostr_uri_scheme,
)
from nostr.rss.parser import parse_feed


_AUTHOR = "f" * 64
_LONGFORM_NADDR = encode_naddr(
    "deep-dive-on-bitcoin", _AUTHOR, 30023,
    ["wss://relay.example.com"],
)


def _full_wordpress_rss() -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"
             xmlns:content="http://purl.org/rss/1.0/modules/content/"
             xmlns:dc="http://purl.org/dc/elements/1.1/">
        <channel>
            <title>Full WordPress Feed</title>
            <link>https://blog.example.com/</link>
            <description>Real prose included</description>
            <item>
                <title>How I learned to love Lightning</title>
                <link>https://blog.example.com/lightning/</link>
                <guid>https://blog.example.com/lightning/</guid>
                <pubDate>Wed, 12 Mar 2025 10:00:00 GMT</pubDate>
                <category>bitcoin</category>
                <description>Teaser.</description>
                <content:encoded><![CDATA[<p>Three years ago, when I first saw a Lightning invoice scroll across my phone, I thought it was a trick. Today I run a node and route a few thousand sats a day.</p><p>Here is what I learned.</p>]]></content:encoded>
            </item>
        </channel>
        </rss>
    """)


def _habla_style_feed_with_nostr_uri() -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
        <channel>
            <title>Habla-style feed</title>
            <link>nostr:naddr1...</link>
            <description>Long-form on Nostr</description>
            <item>
                <title>Deep dive on Bitcoin</title>
                <link>nostr:{_LONGFORM_NADDR}</link>
                <guid>nostr:{_LONGFORM_NADDR}</guid>
                <pubDate>Wed, 12 Mar 2025 10:00:00 GMT</pubDate>
                <description>Teaser.</description>
            </item>
        </channel>
        </rss>
    """)


def _http_feed_with_embedded_naddr() -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
        <channel>
            <title>Self-hosted Nostr blog</title>
            <link>https://example.com/</link>
            <description>Nostr-anchored</description>
            <item>
                <title>Short teaser only</title>
                <link>https://example.com/s/alice/post-{_LONGFORM_NADDR[:12]}/{_LONGFORM_NADDR}</link>
                <guid>{_LONGFORM_NADDR}</guid>
                <pubDate>Wed, 12 Mar 2025 10:00:00 GMT</pubDate>
                <description>Tiny.</description>
            </item>
        </channel>
        </rss>
    """)


class FullPipelineTests(unittest.TestCase):
    """Verify parse → normalize on realistic feed shapes."""

    def test_wordpress_full_content_yields_thick_body(self) -> None:
        feed = parse_feed(_full_wordpress_rss())
        self.assertEqual(len(feed.items), 1)
        template = item_to_article(feed.items[0])
        # Subtract the footer when measuring "real" body length.
        footer_len = len(source_link_footer(feed.items[0].link))
        body_len = len(template.content) - footer_len
        self.assertGreater(body_len, 100, "Full body should be thick")
        self.assertIn("Lightning invoice", template.content)
        self.assertIn("bitcoin", template.hashtags)

    def test_habla_style_yields_thin_body_and_nostr_coord(self) -> None:
        feed = parse_feed(_habla_style_feed_with_nostr_uri())
        item = feed.items[0]
        template = item_to_article(item)
        # Body is tiny, just "Teaser."
        footer_len = len(source_link_footer(item.link))
        body_len = len(template.content) - footer_len
        self.assertLess(body_len, 80, "Habla-style body should be thin")
        # And we can detect both the URI scheme and the coord.
        self.assertTrue(is_nostr_uri_scheme(item.link))
        coord = extract_nostr_coord(item.link)
        self.assertIsNotNone(coord)
        self.assertEqual(coord.d_tag, "deep-dive-on-bitcoin")
        self.assertEqual(coord.pubkey_hex, _AUTHOR)

    def test_http_url_with_embedded_naddr_extracts_coord(self) -> None:
        feed = parse_feed(_http_feed_with_embedded_naddr())
        item = feed.items[0]
        coord = extract_nostr_coord(item.link)
        self.assertIsNotNone(coord)
        # Not a nostr: URI; importer would only resolve if body is thin.
        self.assertFalse(is_nostr_uri_scheme(item.link))
        template = item_to_article(item)
        footer_len = len(source_link_footer(item.link))
        body_len = len(template.content) - footer_len
        self.assertLess(body_len, 80, "Single-word body must register as thin")


class FooterContractTests(unittest.TestCase):
    """Regression guard: importer measures the same footer that
    normalize.item_to_article appends."""

    def test_footer_format_round_trip(self) -> None:
        link = "https://example.com/post/123"
        footer = source_link_footer(link)
        self.assertIn("Originally published at", footer)
        self.assertIn(link, footer)
        # And the body produced by item_to_article must end with that
        # exact string (modulo trailing whitespace normalisation).
        from nostr.rss.parser import FeedItem
        item = FeedItem(
            guid="g", title="t", link=link, summary=None,
            content_html="<p>Hello world.</p>",
            published_at=None, categories=(), image=None, author=None,
        )
        template = item_to_article(item)
        self.assertTrue(template.content.endswith(footer.strip()))


if __name__ == "__main__":
    unittest.main()
