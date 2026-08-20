# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the file-import sources: WXR, Ghost, Medium/Substack ZIPs,
OPML, and their resolvers/registry wiring."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.imports.errors import ERROR_CODES
from nostr.imports.registry import ResolveInput, detect_resolver, resolve_source
from nostr.imports.sources.archive import (
    ArchiveError,
    extract_archive,
    looks_like_zip,
)
from nostr.imports.sources.ghost import is_ghost_export, parse_ghost_export
from nostr.imports.sources.opml import is_opml, parse_opml
from nostr.imports.sources.wxr import is_wxr, parse_wxr

from tests.imports_fakes import FakeFetcher


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

WXR = """<?xml version="1.0"?>
<rss version="2.0" xmlns:wp="http://wordpress.org/export/1.2/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>My WP Blog</title>
<item>
  <title>Published Post</title>
  <link>https://wp.example/post-1</link>
  <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
  <dc:creator><![CDATA[alice]]></dc:creator>
  <category><![CDATA[Bitcoin]]></category>
  <content:encoded><![CDATA[<p>Body with <b>markup</b> and a literal
  &lt;/item&gt; is fine inside CDATA: </item> see?</p>]]></content:encoded>
  <excerpt:encoded><![CDATA[The excerpt]]></excerpt:encoded>
  <wp:post_id>11</wp:post_id>
  <wp:post_date_gmt>2024-01-01 00:00:00</wp:post_date_gmt>
  <wp:post_name>published-post</wp:post_name>
  <wp:status>publish</wp:status>
  <wp:post_type>post</wp:post_type>
</item>
<item>
  <title>A Draft</title>
  <wp:post_id>12</wp:post_id>
  <wp:status>draft</wp:status>
  <wp:post_type>post</wp:post_type>
</item>
<item>
  <title>An Attachment</title>
  <wp:post_id>13</wp:post_id>
  <wp:status>publish</wp:status>
  <wp:post_type>attachment</wp:post_type>
</item>
<item>
  <title>A Page</title>
  <wp:post_id>14</wp:post_id>
  <wp:post_date_gmt>0000-00-00 00:00:00</wp:post_date_gmt>
  <wp:status>publish</wp:status>
  <wp:post_type>page</wp:post_type>
  <content:encoded><![CDATA[<p>Page body</p>]]></content:encoded>
</item>
</channel></rss>
"""

GHOST = json.dumps({"db": [{"data": {"posts": [
    {"id": "p1", "title": "Live Post", "status": "published",
     "html": "<p>Ghost body</p>", "published_at": "2024-01-01T10:00:00.000Z",
     "feature_image": "https://g.example/hero.png",
     "custom_excerpt": "An excerpt"},
    {"id": "p2", "title": "Draft", "status": "draft", "html": "<p>x</p>"},
    {"id": "p3", "title": "Lexical only", "status": "published", "html": ""},
]}}]})

OPML = """<?xml version="1.0"?>
<opml version="2.0"><head><title>My Subscriptions</title></head>
<body>
  <outline text="Tech">
    <outline text="Blog A" title="Blog A"
             xmlUrl="https://a.example/feed" htmlUrl="https://a.example"/>
    <outline text="Blog B" xmlUrl="https://b.example/rss.xml"/>
  </outline>
  <outline text="Blog A dupe" xmlUrl="https://A.EXAMPLE/feed"/>
</body></opml>
"""


def medium_zip() -> bytes:
    post = (
        "<html><body>"
        '<h1 class="p-name">Medium Title</h1>'
        '<section class="p-summary">Sub</section>'
        '<time class="dt-published" datetime="2024-02-01T00:00:00Z"></time>'
        '<a class="p-canonical" href="https://medium.com/@a/post-1"></a>'
        '<section class="e-content"><p>Medium body</p></section>'
        "</body></html>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("posts/2024-02-01_post.html", post)
        zf.writestr("posts/draft_unpublished.html", post)
        zf.writestr("profile/about.html", "<html></html>")
    return buffer.getvalue()


def substack_zip() -> bytes:
    csv_text = (
        "post_id,post_date,is_published,email_sent_at,type,audience,"
        "title,subtitle,podcast_url\n"
        "101,2024-03-01T00:00:00Z,TRUE,,newsletter,everyone,"
        "Sub Title,Sub subtitle,\n"
        "102,2024-03-02T00:00:00Z,FALSE,,newsletter,everyone,"
        "Unpublished,,\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("posts.csv", csv_text)
        zf.writestr("posts/101.html", "<p>Substack body</p>")
        zf.writestr("posts/102.html", "<p>hidden</p>")
    return buffer.getvalue()


class Sink:
    def __init__(self):
        self.result = None
        self.error = None

    def on_success(self, result):
        self.result = result

    def on_failure(self, error):
        self.error = error


def run_paste(body):
    sink = Sink()
    resolve_source(
        ResolveInput(url="", pasted_body=body),
        fetcher=FakeFetcher(),
        on_success=sink.on_success,
        on_failure=sink.on_failure,
    )
    return sink


# --------------------------------------------------------------------------- #
# WXR                                                                         #
# --------------------------------------------------------------------------- #

class TestWxr(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_wxr(WXR))
        self.assertFalse(is_wxr("<rss version=\"2.0\"><channel/></rss>"))
        self.assertFalse(is_wxr(None))

    def test_parse_keeps_published_posts_and_pages_only(self):
        export = parse_wxr(WXR)
        self.assertEqual(export.title, "My WP Blog")
        self.assertEqual([i.guid for i in export.items], ["wp-11", "wp-14"])

    def test_cdata_body_survives_including_literal_item_close(self):
        export = parse_wxr(WXR)
        post = export.items[0]
        self.assertIn("<b>markup</b>", post.content_html)
        self.assertIn("</item> see?", post.content_html)
        self.assertEqual(post.summary, "The excerpt")
        self.assertEqual(post.author, "alice")
        self.assertEqual(post.categories, ("Bitcoin",))
        self.assertEqual(post.published_at, 1704067200)

    def test_zero_date_falls_back_or_none(self):
        export = parse_wxr(WXR)
        page = export.items[1]
        self.assertIsNone(page.published_at)

    def test_pages_can_be_excluded(self):
        export = parse_wxr(WXR, include_pages=False)
        self.assertEqual([i.guid for i in export.items], ["wp-11"])

    def test_registry_claims_wxr_paste(self):
        self.assertEqual(
            detect_resolver(ResolveInput(pasted_body=WXR)).id, "wxr")
        sink = run_paste(WXR)
        self.assertIsNone(sink.error)
        self.assertEqual(sink.result.feed.format, "wxr")
        self.assertEqual(len(sink.result.feed.items), 2)

    def test_empty_export_is_a_clear_error(self):
        empty = WXR.split("<item>")[0] + "</channel></rss>"
        sink = run_paste(empty)
        self.assertEqual(sink.error.code, ERROR_CODES.WXR_EMPTY)


# --------------------------------------------------------------------------- #
# Ghost                                                                       #
# --------------------------------------------------------------------------- #

class TestGhost(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_ghost_export(GHOST))
        self.assertFalse(is_ghost_export('{"items": []}'))

    def test_parse_keeps_published_html_posts(self):
        export = parse_ghost_export(GHOST)
        self.assertEqual(len(export.items), 1)
        post = export.items[0]
        self.assertEqual(post.guid, "ghost-p1")
        self.assertEqual(post.title, "Live Post")
        self.assertEqual(post.summary, "An excerpt")
        self.assertEqual(post.image, "https://g.example/hero.png")
        self.assertEqual(post.published_at, 1704103200)

    def test_garbage_never_throws(self):
        self.assertEqual(parse_ghost_export("not json").items, ())
        self.assertEqual(parse_ghost_export("").items, ())

    def test_registry_claims_ghost_paste(self):
        self.assertEqual(
            detect_resolver(ResolveInput(pasted_body=GHOST)).id, "ghost")
        sink = run_paste(GHOST)
        self.assertIsNone(sink.error)
        self.assertEqual(sink.result.feed.format, "ghost")

    def test_plain_feed_paste_still_goes_to_rss(self):
        from tests.imports_fakes import TWO_ITEM_FEED
        self.assertEqual(
            detect_resolver(ResolveInput(pasted_body=TWO_ITEM_FEED)).id, "rss")


# --------------------------------------------------------------------------- #
# Archives                                                                    #
# --------------------------------------------------------------------------- #

class TestArchives(unittest.TestCase):
    def test_zip_sniffing(self):
        self.assertTrue(looks_like_zip(medium_zip()))
        self.assertFalse(looks_like_zip(b"<?xml version=\"1.0\"?>"))

    def test_medium_extraction(self):
        result = extract_archive(medium_zip())
        self.assertEqual(result.platform, "medium")
        self.assertEqual(len(result.items), 1)  # draft skipped
        post = result.items[0]
        self.assertEqual(post.title, "Medium Title")
        self.assertEqual(post.link, "https://medium.com/@a/post-1")
        self.assertIn("Medium body", post.content_html)
        self.assertEqual(post.published_at, 1706745600)

    def test_substack_extraction(self):
        result = extract_archive(substack_zip())
        self.assertEqual(result.platform, "substack")
        self.assertEqual(len(result.items), 1)  # unpublished row skipped
        post = result.items[0]
        self.assertEqual(post.guid, "substack-101")
        self.assertEqual(post.title, "Sub Title")
        self.assertIn("Substack body", post.content_html)

    def test_unknown_zip_layout(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("random.txt", "hello")
        result = extract_archive(buffer.getvalue())
        self.assertIsNone(result.platform)

    def test_invalid_zip_raises_archive_error(self):
        with self.assertRaises(ArchiveError):
            extract_archive(b"PK\x03\x04 broken")
        with self.assertRaises(ArchiveError):
            extract_archive(b"not a zip at all")


# --------------------------------------------------------------------------- #
# OPML                                                                        #
# --------------------------------------------------------------------------- #

class TestOpml(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_opml(OPML))
        self.assertFalse(is_opml("<rss version=\"2.0\"/>"))

    def test_parse_flattens_and_dedupes(self):
        doc = parse_opml(OPML)
        self.assertEqual(doc.title, "My Subscriptions")
        self.assertEqual(
            [f.xml_url for f in doc.feeds],
            ["https://a.example/feed", "https://b.example/rss.xml"])
        self.assertEqual(doc.feeds[0].title, "Blog A")
        self.assertEqual(doc.feeds[0].html_url, "https://a.example")

    def test_garbage_never_throws(self):
        self.assertEqual(parse_opml("<opml><outline text='x'>").feeds, ())
        self.assertEqual(parse_opml("").feeds, ())


if __name__ == "__main__":
    unittest.main()
