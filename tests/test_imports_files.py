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
from nostr.imports.sources.ghost import (
    is_ghost_export,
    needs_site_address,
    parse_ghost_export,
)
from nostr.imports.sources.opml import is_opml, parse_opml
from nostr.imports.sources.wxr import is_wxr, parse_wxr, post_html

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


MEDIUM_EXPORT_POST = (
    '<html><body><article class="h-entry">'
    '<header><h1 class="p-name">Sound Money</h1></header>'
    '<section data-field="subtitle" class="p-summary">A snippet</section>'
    '<section data-field="body" class="e-content">'
    '<section class="section section--body section--first">'
    '<div class="section-divider"><hr class="section-divider"></div>'
    '<div class="section-content"><div class="section-inner">'
    '<h3 class="graf graf--h3 graf--leading graf--title">Sound Money</h3>'
    '<h4 class="graf graf--h4 graf--subtitle">Why it matters</h4>'
    '<figure class="graf graf--figure"><img class="graf-image" '
    'src="https://cdn-images-1.medium.com/max/800/1.png"></figure>'
    '<p class="graf graf--p">First words.</p></div></div></section>'
    '<section class="section section--body">'
    '<div class="section-divider"><hr class="section-divider"></div>'
    '<div class="section-content"><p>Second part.</p></div></section></section>'
    '<footer><p>By <a class="p-author h-card" href="https://medium.com/@jane">Jane Doe</a>'
    ' on <a href="https://medium.com/p/1"><time class="dt-published" '
    'datetime="2020-01-01T10:00:00.000Z">January 1, 2020</time></a>.</p>'
    '<p><a class="p-canonical" href="https://medium.com/@jane/sound-money-1">'
    'Canonical link</a></p><p class="p-tags">Tagged in '
    '<a class="p-tag" href="https://medium.com/tag/bitcoin">Bitcoin</a>, '
    '<a class="p-tag" href="https://medium.com/tag/money">Money</a></p>'
    '</footer></article></body></html>'
)


def substack_export_zip() -> bytes:
    csv_text = (
        "post_id,post_date,is_published,email_sent_at,inbox_sent_at,type,audience,"
        "title,subtitle,podcast_url\n"
        "101.sound-money,2024-03-01T00:00:00.000Z,true,,,newsletter,everyone,"
        "Sound Money,Why it matters,\n"
        "102.episode-one,2024-03-02T00:00:00.000Z,true,,,podcast,everyone,"
        "Episode One,,https://api.substack.com/feed/podcast/102/ep.mp3\n"
    )
    body = (
        "<p>The text.</p>"
        '<div class="subscription-widget-wrap"><div class="subscription-widget">'
        '<div class="preamble"><p>Thanks for reading! Subscribe for free.</p></div>'
        '<form><input type="email"></form></div></div>'
        '<p class="button-wrapper"><a class="button primary" '
        'href="https://jane.example.com/subscribe?"><span>Subscribe now</span></a></p>'
        '<p class="button-wrapper"><a class="button primary" '
        'href="https://jane.example.com/p/sound-money?action=share">'
        "<span>Share</span></a></p><p>The end.</p>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("posts.csv", csv_text)
        zf.writestr("posts/101.sound-money.html", body)
        zf.writestr("posts/102.episode-one.html", "<p>Show notes</p>")
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

WXR_CLASSIC = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:wp="http://wordpress.org/export/1.2/">
<channel><title>Blog</title><wp:wxr_version>1.2</wp:wxr_version>
<wp:author><wp:author_login><![CDATA[admin]]></wp:author_login>
<wp:author_display_name><![CDATA[Alice Example]]></wp:author_display_name></wp:author>
<item><title>Tom &amp; Jerry</title><link>https://b.example/tj</link>
<dc:creator><![CDATA[admin]]></dc:creator>
<wp:post_id>5</wp:post_id><wp:status>publish</wp:status><wp:post_type>post</wp:post_type>
<category domain="category" nicename="uncategorized"><![CDATA[Uncategorized]]></category>
<category domain="category" nicename="lightning"><![CDATA[Lightning]]></category>
<category domain="post_tag" nicename="self-custody"><![CDATA[self custody]]></category>
<category domain="post_format" nicename="post-format-aside"><![CDATA[Aside]]></category>
<content:encoded><![CDATA[First line.
still the first paragraph.

Second paragraph.

[caption id="attachment_9" align="alignnone" width="300"]<img src="https://b.example/i.jpg" /> A caption[/caption]]]></content:encoded>
<wp:postmeta><wp:meta_key><![CDATA[_thumbnail_id]]></wp:meta_key><wp:meta_value><![CDATA[9]]></wp:meta_value></wp:postmeta>
</item>
<item><title>cover</title><wp:post_id>9</wp:post_id><wp:status>inherit</wp:status>
<wp:post_type>attachment</wp:post_type><wp:attachment_url><![CDATA[https://b.example/cover.jpg]]></wp:attachment_url></item>
</channel></rss>"""


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

    def test_a_wordpress_export_reads_as_its_site_shows_it(self):
        export = parse_wxr(WXR_CLASSIC)
        post = export.items[0]
        self.assertEqual(post.title, "Tom & Jerry")
        self.assertEqual(post.author, "Alice Example")
        self.assertEqual(post.image, "https://b.example/cover.jpg")
        self.assertEqual(post.categories, ("Lightning", "self custody"))
        self.assertIn("<p>First line.<br>\nstill the first paragraph.</p>", post.content_html)
        self.assertIn("<p>Second paragraph.</p>", post.content_html)
        self.assertIn("<figcaption>A caption</figcaption>", post.content_html)
        self.assertNotIn("[caption", post.content_html)

    def test_shortcodes_become_what_the_page_shows(self):
        body = post_html(
            "[embed]https://www.youtube.com/watch?v=abc[/embed]\n\n"
            '[video src="https://b.example/v.mp4"][/video]\n\n'
            '[gallery ids="9,10"]\n\n'
            "[et_pb_section][et_pb_text]Builder words[/et_pb_text][/et_pb_section]\n\n"
            "Text [see below] stays, and [[gallery]] is written as text.",
            attachments={"9": "https://b.example/g9.jpg"})
        self.assertIn('<a href="https://www.youtube.com/watch?v=abc">', body)
        self.assertIn('<a href="https://b.example/v.mp4">', body)
        self.assertIn('<img src="https://b.example/g9.jpg">', body)
        self.assertIn("<p>Builder words</p>", body)
        self.assertIn("Text [see below] stays, and [gallery] is written as text.", body)

    def test_jetpack_embeds_are_their_pages_and_never_lost(self):
        cases = {
            "[youtube=https://www.youtube.com/watch?v=x]": "https://www.youtube.com/watch?v=x",
            "[youtube https://youtu.be/x&w=640&h=360]": "https://youtu.be/x",
            "[vimeo 141358]": "https://vimeo.com/141358",
            "[tweet 1234567]": "https://twitter.com/i/web/status/1234567",
            '[soundcloud url="https://api.soundcloud.com/tracks/1"]':
                "https://api.soundcloud.com/tracks/1",
            '[spotify id="spotify:track:abc"]': "https://open.spotify.com/track/abc",
        }
        for shortcode, address in cases.items():
            self.assertIn(f'<a href="{address}">', post_html(shortcode), shortcode)
        self.assertIn("[vimeo not-an-id]", post_html("[vimeo not-an-id]"))

    def test_code_and_bracketed_words_are_left_alone(self):
        self.assertIn('<pre>[my_code x="1"]\n\nmore</pre>',
                      post_html('<pre>[my_code x="1"]\n\nmore</pre>'))
        self.assertIn("<code>[gallery]</code>", post_html("Write <code>[gallery]</code>."))
        self.assertIn("Write [e-mail] to me.", post_html("Write [e-mail] to me."))

    def test_a_shortcode_written_as_text_reads_as_in_wordpress(self):
        self.assertIn("<code>[gallery]</code>", post_html("<code>[[gallery]]</code>"))
        out = post_html("[[<code>x</code>]]")
        self.assertNotIn("\x00", out)
        self.assertIn("[<code>x</code>]", out)

    def test_block_editor_posts_are_left_as_they_are(self):
        body = post_html("<!-- wp:paragraph -->\n<p>One</p>\n<!-- /wp:paragraph -->\n\n"
                         "<!-- wp:paragraph -->\n<p>Two</p>\n<!-- /wp:paragraph -->")
        self.assertNotIn("<p><p>", body)
        self.assertNotIn("<br>", body)

    def test_preformatted_code_keeps_its_blank_lines(self):
        self.assertIn("<pre>a\n\nb</pre>", post_html("Intro\n\n<pre>a\n\nb</pre>"))

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

GHOST_4 = json.dumps({"db": [{"meta": {"version": "5.80.0"}, "data": {
    "posts": [{"id": "p1", "title": "First", "slug": "first-post", "status": "published",
               "html": '<p>Body <img src="__GHOST_URL__/content/images/in.jpg"></p>',
               "feature_image": "__GHOST_URL__/content/images/hero.jpg",
               "published_at": "2024-01-01T10:00:00.000Z"}],
    "posts_meta": [{"post_id": "p1", "meta_description": "Meta description"}],
    "tags": [{"id": "t1", "name": "Bitcoin"}, {"id": "t2", "name": "#hidden"},
             {"id": "t3", "name": "Self Custody"}],
    "posts_tags": [{"post_id": "p1", "tag_id": "t3", "sort_order": 2},
                   {"post_id": "p1", "tag_id": "t1", "sort_order": 0},
                   {"post_id": "p1", "tag_id": "t2", "sort_order": 1}],
    "users": [{"id": "u1", "name": "Jane Doe"}],
    "posts_authors": [{"post_id": "p1", "author_id": "u1", "sort_order": 0}],
}}]})


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

    def test_a_ghost_4_export_takes_the_blog_address(self):
        self.assertTrue(needs_site_address(GHOST_4))
        self.assertFalse(needs_site_address(GHOST))
        post = parse_ghost_export(GHOST_4, site_url="https://blog.example/").items[0]
        self.assertEqual(post.image, "https://blog.example/content/images/hero.jpg")
        self.assertIn('src="https://blog.example/content/images/in.jpg"', post.content_html)
        self.assertEqual(post.link, "https://blog.example/first-post/")
        self.assertEqual(post.categories, ("Bitcoin", "Self Custody"))
        self.assertEqual(post.author, "Jane Doe")
        self.assertEqual(post.summary, "Meta description")

    def test_without_an_address_nothing_points_at_the_placeholder(self):
        post = parse_ghost_export(GHOST_4).items[0]
        self.assertNotIn("__GHOST_URL__", post.content_html + (post.image or ""))
        self.assertIsNone(post.link)

    def test_garbage_never_throws(self):
        self.assertEqual(parse_ghost_export("not json").items, ())
        self.assertEqual(parse_ghost_export("").items, ())

    def test_registry_claims_ghost_paste(self):
        self.assertEqual(
            detect_resolver(ResolveInput(pasted_body=GHOST)).id, "ghost")
        sink = run_paste(GHOST)
        self.assertIsNone(sink.error)
        self.assertEqual(sink.result.feed.format, "ghost")

    def test_an_export_that_cannot_be_read_says_what_to_do(self):
        # Lessons 36: what went wrong is the log's, never in the sentence.
        from unittest import mock
        from nostr.imports.errors import ERROR_CODES, failure_words
        with mock.patch("nostr.imports.resolvers.ghost.parse_ghost_export",
                        side_effect=RuntimeError("bad byte 0x9c")):
            sink = run_paste(GHOST)
        self.assertEqual(sink.error.code, ERROR_CODES.ARCHIVE_UNREADABLE)
        self.assertEqual(failure_words(sink.error), "Couldn't read that archive: export it "
                                                    "again from the platform and try again.")

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

    def test_medium_title_subtitle_and_divider_left_out_of_the_body(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("posts/2020-01-01_Sound-Money-1.html", MEDIUM_EXPORT_POST)
        (post,) = extract_archive(buffer.getvalue()).items
        self.assertNotIn("Sound Money", post.content_html)
        self.assertNotIn("Why it matters", post.content_html)
        # Only the rule above the first words goes; the one between the
        # two sections is a break the writer made.
        self.assertEqual(post.content_html.count("section-divider"), 2)
        self.assertLess(post.content_html.index("First words"),
                        post.content_html.index("section-divider"))
        self.assertEqual(post.summary, "Why it matters")
        self.assertEqual(post.image, "https://cdn-images-1.medium.com/max/800/1.png")
        self.assertEqual(post.link, "https://medium.com/@jane/sound-money-1")
        self.assertEqual(post.author, "Jane Doe")
        self.assertEqual(post.published_at, 1577872800)
        self.assertEqual(post.categories, ("Bitcoin", "Money"))

    def test_substack_details_and_furniture(self):
        posts = extract_archive(substack_export_zip()).items
        article, episode = posts
        # "newsletter" says what Substack made of it, not what it is about.
        self.assertEqual(article.categories, ())
        self.assertEqual(article.summary, "Why it matters")
        self.assertEqual(article.published_at, 1709251200)
        # The publication's address comes from its own subscribe links.
        self.assertEqual(article.link, "https://jane.example.com/p/sound-money")
        self.assertIn("The text.", article.content_html)
        self.assertIn("The end.", article.content_html)
        for furniture in ("Subscribe", "Share", "Thanks for reading", "<form"):
            self.assertNotIn(furniture, article.content_html)
        # A podcast row's audio comes along as the episode.
        self.assertEqual(episode.podcast.audio,
                         "https://api.substack.com/feed/podcast/102/ep.mp3")
        self.assertEqual(episode.categories, ())

    def test_substack_link_left_out_without_the_publications_address(self):
        (post,) = extract_archive(substack_zip()).items
        self.assertIsNone(post.link)

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

    def test_angle_bracket_in_an_attribute_keeps_the_feed(self):
        doc = parse_opml(
            '<opml><body><outline text="News > Tech" title="News > Tech" '
            'xmlUrl="https://a.example/feed"/></body></opml>')
        self.assertEqual([(f.title, f.xml_url) for f in doc.feeds],
                         [("News > Tech", "https://a.example/feed")])

    def test_numeric_entities_read_in_titles_and_addresses(self):
        doc = parse_opml(
            '<opml><head><title>Joe&#8217;s list</title></head><body>'
            '<outline text="Joe&#8217;s Blog" '
            'xmlUrl="https://b.example/caf&#233;/feed?a=1&amp;b=2"/></body></opml>')
        self.assertEqual(doc.title, "Joe\u2019s list")
        self.assertEqual(doc.feeds[0].title, "Joe\u2019s Blog")
        self.assertEqual(doc.feeds[0].xml_url, "https://b.example/caf\u00e9/feed?a=1&b=2")

    def test_garbage_never_throws(self):
        self.assertEqual(parse_opml("<opml><outline text='x'>").feeds, ())
        self.assertEqual(parse_opml("").feeds, ())


class TestSubstackAddress(unittest.TestCase):
    def test_the_subscribe_links_name_the_publication(self):
        from nostr.imports.sources.archive import _publication_address
        bodies = ['<a href="https://mine.example/subscribe">Subscribe</a>',
                  '<a href="https://other.substack.com/p/a">a</a>'
                  '<a href="https://other.substack.com/p/b">b</a>']
        self.assertEqual(_publication_address(bodies, []), "mine.example")

    def test_without_subscribe_links_the_subscriber_list_names_it(self):
        from nostr.imports.sources.archive import _publication_address
        self.assertEqual(_publication_address(
            ['<a href="https://www.instagram.com/p/abc/">a post</a>'],
            ["posts.csv", "email_list.mypub.csv"]), "mypub.substack.com")

    def test_only_substack_post_links_name_a_publication(self):
        from nostr.imports.sources.archive import _publication_address
        self.assertIsNone(_publication_address(
            ['<a href="https://www.instagram.com/p/abc/">a post</a>'], []))
        self.assertEqual(_publication_address(
            ['<a href="https://mine.substack.com/p/x">x</a>'], []), "mine.substack.com")


if __name__ == "__main__":
    unittest.main()
