"""Unit tests for ``nostr.rss.parser``.

Samples are kept inline (small, readable, no fixture directory) and shaped
after real-world feeds from WordPress, Ghost, Hugo, and JSON Feed
publishers.
"""

from __future__ import annotations

import textwrap
import unittest

from nostr.rss.parser import RssError, parse_feed


def _wp_rss() -> str:
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"
             xmlns:content="http://purl.org/rss/1.0/modules/content/"
             xmlns:dc="http://purl.org/dc/elements/1.1/"
             xmlns:media="http://search.yahoo.com/mrss/">
        <channel>
            <title>Alice's Blog</title>
            <link>https://alice.example.com/</link>
            <description>Long-form posts</description>
            <item>
                <title>Bitcoin and Nostr</title>
                <link>https://alice.example.com/btc-nostr</link>
                <guid isPermaLink="false">post-1001</guid>
                <pubDate>Wed, 12 Mar 2025 10:00:00 GMT</pubDate>
                <dc:creator>Alice</dc:creator>
                <category>bitcoin</category>
                <category>nostr</category>
                <description>A teaser summary.</description>
                <content:encoded><![CDATA[<p>The full body of the post with <img src="https://alice.example.com/cover.jpg"/> an image.</p>]]></content:encoded>
                <enclosure url="https://alice.example.com/enc.jpg" type="image/jpeg" length="2000"/>
            </item>
            <item>
                <title>Just a summary</title>
                <link>https://alice.example.com/sum-only</link>
                <guid>https://alice.example.com/sum-only</guid>
                <pubDate>Mon, 10 Mar 2025 09:00:00 GMT</pubDate>
                <description>This post has no content:encoded.</description>
            </item>
        </channel>
        </rss>
    """)


def _atom_feed() -> str:
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <title>Hugo Blog</title>
            <link href="https://hugo.example.com/"/>
            <subtitle>Notes from a static site</subtitle>
            <entry>
                <title>Atom post</title>
                <id>tag:hugo.example.com,2025:1</id>
                <link rel="alternate" href="https://hugo.example.com/p1"/>
                <published>2025-03-12T10:00:00Z</published>
                <updated>2025-03-13T10:00:00Z</updated>
                <author><name>Bob</name></author>
                <category term="meta"/>
                <summary type="html">A short summary.</summary>
                <content type="html">&lt;p&gt;Body with &lt;img src="https://hugo.example.com/i.png"/&gt;&lt;/p&gt;</content>
            </entry>
        </feed>
    """)


def _json_feed() -> str:
    return textwrap.dedent("""\
        {
          "version": "https://jsonfeed.org/version/1.1",
          "title": "JSON Blog",
          "home_page_url": "https://json.example.com/",
          "description": "Modern blog",
          "items": [
            {
              "id": "post-42",
              "url": "https://json.example.com/p/42",
              "title": "JSON Feed post",
              "summary": "Short summary",
              "content_html": "<p>Hello with <img src=\\"https://json.example.com/img.png\\"/> image.</p>",
              "date_published": "2025-03-12T10:00:00Z",
              "tags": ["a", "b"],
              "image": "https://json.example.com/cover.jpg",
              "author": {"name": "Carol"}
            }
          ]
        }
    """)


class RssParseTests(unittest.TestCase):
    def test_parses_wordpress_rss(self) -> None:
        feed = parse_feed(_wp_rss())
        self.assertEqual(feed.format, "rss")
        self.assertEqual(feed.title, "Alice's Blog")
        self.assertEqual(feed.link, "https://alice.example.com/")
        self.assertEqual(len(feed.items), 2)

    def test_rss_prefers_content_encoded_over_description(self) -> None:
        item = parse_feed(_wp_rss()).items[0]
        self.assertIn("full body", item.content_html.lower())
        self.assertEqual(item.summary, "A teaser summary.")

    def test_rss_falls_back_to_description_when_no_content(self) -> None:
        item = parse_feed(_wp_rss()).items[1]
        self.assertEqual(item.content_html, "This post has no content:encoded.")
        self.assertIsNone(item.summary)

    def test_rss_image_priority_enclosure(self) -> None:
        item = parse_feed(_wp_rss()).items[0]
        self.assertEqual(item.image, "https://alice.example.com/enc.jpg")

    def test_rss_image_falls_back_to_body_image_when_no_enclosure(self) -> None:
        rss = _wp_rss().replace(
            '<enclosure url="https://alice.example.com/enc.jpg" type="image/jpeg" length="2000"/>',
            "",
        )
        item = parse_feed(rss).items[0]
        self.assertEqual(item.image, "https://alice.example.com/cover.jpg")

    def test_rss_categories_preserved(self) -> None:
        item = parse_feed(_wp_rss()).items[0]
        self.assertEqual(item.categories, ("bitcoin", "nostr"))

    def test_rss_pubdate_to_unix_seconds(self) -> None:
        item = parse_feed(_wp_rss()).items[0]
        # Wed, 12 Mar 2025 10:00:00 GMT = 1741773600
        self.assertEqual(item.published_at, 1741773600)

    def test_rss_dc_creator_becomes_author(self) -> None:
        item = parse_feed(_wp_rss()).items[0]
        self.assertEqual(item.author, "Alice")

    def test_rss_guid_preserved(self) -> None:
        item = parse_feed(_wp_rss()).items[0]
        self.assertEqual(item.guid, "post-1001")


class AtomParseTests(unittest.TestCase):
    def test_format_and_envelope(self) -> None:
        feed = parse_feed(_atom_feed())
        self.assertEqual(feed.format, "atom")
        self.assertEqual(feed.title, "Hugo Blog")
        self.assertEqual(feed.description, "Notes from a static site")

    def test_content_prefers_content_over_summary(self) -> None:
        item = parse_feed(_atom_feed()).items[0]
        self.assertIn("Body with", item.content_html)
        self.assertEqual(item.summary, "A short summary.")

    def test_image_from_body_only(self) -> None:
        item = parse_feed(_atom_feed()).items[0]
        self.assertEqual(item.image, "https://hugo.example.com/i.png")

    def test_published_prefers_published_over_updated(self) -> None:
        item = parse_feed(_atom_feed()).items[0]
        self.assertEqual(item.published_at, 1741773600)  # 2025-03-12T10:00:00Z

    def test_link_resolves_to_alternate_href(self) -> None:
        item = parse_feed(_atom_feed()).items[0]
        self.assertEqual(item.link, "https://hugo.example.com/p1")

    def test_author_from_author_element(self) -> None:
        item = parse_feed(_atom_feed()).items[0]
        self.assertEqual(item.author, "Bob")


class JsonFeedParseTests(unittest.TestCase):
    def test_format_and_envelope(self) -> None:
        feed = parse_feed(_json_feed())
        self.assertEqual(feed.format, "jsonfeed")
        self.assertEqual(feed.title, "JSON Blog")
        self.assertEqual(feed.link, "https://json.example.com/")

    def test_content_html_preferred(self) -> None:
        item = parse_feed(_json_feed()).items[0]
        self.assertIn("Hello with", item.content_html)

    def test_image_field_wins_over_body_image(self) -> None:
        item = parse_feed(_json_feed()).items[0]
        self.assertEqual(item.image, "https://json.example.com/cover.jpg")

    def test_falls_back_to_body_image_when_image_field_missing(self) -> None:
        payload = _json_feed().replace(
            '"image": "https://json.example.com/cover.jpg",', ""
        )
        item = parse_feed(payload).items[0]
        self.assertEqual(item.image, "https://json.example.com/img.png")

    def test_date_published_parsed(self) -> None:
        item = parse_feed(_json_feed()).items[0]
        self.assertEqual(item.published_at, 1741773600)

    def test_author_from_author_object(self) -> None:
        item = parse_feed(_json_feed()).items[0]
        self.assertEqual(item.author, "Carol")

    def test_tags_become_categories(self) -> None:
        item = parse_feed(_json_feed()).items[0]
        self.assertEqual(item.categories, ("a", "b"))


class AutoDetectTests(unittest.TestCase):
    def test_empty_input_raises(self) -> None:
        with self.assertRaises(RssError):
            parse_feed("")

    def test_garbage_input_raises(self) -> None:
        with self.assertRaises(RssError):
            parse_feed("not a feed at all")

    def test_invalid_json_feed_raises(self) -> None:
        with self.assertRaises(RssError):
            parse_feed('{"not": "a feed"}')

    def test_leading_whitespace_does_not_confuse_detection(self) -> None:
        feed = parse_feed("\n\n  " + _json_feed())
        self.assertEqual(feed.format, "jsonfeed")


if __name__ == "__main__":
    unittest.main()
