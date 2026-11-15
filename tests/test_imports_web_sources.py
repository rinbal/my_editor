# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the web-source layer: sitemap, MDX/GitHub, Bluesky.

Everything runs against the synchronous fake fetcher, so parsing,
discovery, fan-out, and thread stitching all settle deterministically
with no network.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.imports.errors import ERROR_CODES
from nostr.imports.registry import detect_resolver, resolve_source
from nostr.imports.resolvers.mdx import parse_github_tree
from nostr.imports.sources.bluesky import (
    apply_facets,
    collect_self_thread,
    derive_thread_title,
    parse_bluesky_url,
    post_to_markdown,
)
from nostr.imports.sources.mdx import mdx_to_markdown, parse_frontmatter
from nostr.imports.sources.sitemap import (
    SITEMAP_MAX_URLS,
    entries_to_items,
    filter_entries,
    is_article_url,
    is_sitemap,
    parse_sitemap,
    title_from_url,
)

from tests.imports_fakes import FakeFetcher


class Sink:
    def __init__(self):
        self.result = None
        self.error = None

    def on_success(self, result):
        assert self.result is None and self.error is None
        self.result = result

    def on_failure(self, error):
        assert self.result is None and self.error is None
        self.error = error


def run(value, fetcher=None, pasted_body=None):
    from nostr.imports.registry import ResolveInput
    sink = Sink()
    resolve_source(
        ResolveInput(url=value, pasted_body=pasted_body),
        fetcher=fetcher or FakeFetcher(),
        on_success=sink.on_success,
        on_failure=sink.on_failure,
    )
    return sink


# --------------------------------------------------------------------------- #
# Sitemap                                                                     #
# --------------------------------------------------------------------------- #

def urlset(entries):
    rows = "".join(
        f"<url><loc>{loc}</loc>"
        + (f"<lastmod>{mod}</lastmod>" if mod else "")
        + "</url>"
        for loc, mod in entries
    )
    return f'<?xml version="1.0"?><urlset>{rows}</urlset>'


def sitemap_index(locs):
    rows = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex>{rows}</sitemapindex>'


class TestSitemapParsing(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_sitemap(urlset([("https://x.example/a", "")])))
        self.assertTrue(is_sitemap(sitemap_index(["https://x.example/s.xml"])))
        self.assertFalse(is_sitemap("<html><body></body></html>"))
        self.assertFalse(is_sitemap(None))

    def test_parse_urls_and_subs(self):
        urls, subs = parse_sitemap(urlset([
            ("https://x.example/post-a", "2026-01-02"),
            ("https://x.example/post-b", ""),
        ]))
        self.assertEqual([u.loc for u in urls],
                         ["https://x.example/post-a", "https://x.example/post-b"])
        self.assertEqual(subs, [])
        urls, subs = parse_sitemap(sitemap_index(["https://x.example/s1.xml"]))
        self.assertEqual(urls, [])
        self.assertEqual([s.loc for s in subs], ["https://x.example/s1.xml"])

    def test_cdata_and_entities_in_loc(self):
        urls, _ = parse_sitemap(
            "<urlset><url><loc><![CDATA[https://x.example/a?b=1&c=2]]></loc></url>"
            "<url><loc>https://x.example/caf&#233;?a=1&amp;b=2</loc></url></urlset>")
        self.assertEqual([u.loc for u in urls],
                         ["https://x.example/a?b=1&c=2", "https://x.example/café?a=1&b=2"])

    def test_publication_date_is_the_date_lastmod_is_not(self):
        urls, _ = parse_sitemap(
            '<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
            "<url><loc>https://x.example/news-post</loc><lastmod>2026-02-01</lastmod>"
            "<news:news><news:publication_date>2025-12-24T08:00:00Z"
            "</news:publication_date></news:news></url>"
            "<url><loc>https://x.example/edited-post</loc>"
            "<lastmod>2026-02-01</lastmod></url></urlset>")
        news, edited = entries_to_items(urls)
        self.assertEqual(news.published_at, 1766563200)
        # A lastmod is when the page changed: the page itself tells the
        # date when the post is imported.
        self.assertIsNone(edited.published_at)

    def test_article_url_filter(self):
        self.assertTrue(is_article_url("https://x.example/2026/my-post"))
        self.assertFalse(is_article_url("https://x.example/"))
        self.assertFalse(is_article_url("https://x.example/tag/foo/"))
        self.assertFalse(is_article_url("https://x.example/image.png"))
        self.assertFalse(is_article_url("ftp://x.example/post"))

    def test_filter_sorts_newest_first_and_caps(self):
        entries, _ = parse_sitemap(urlset(
            [(f"https://x.example/p{i}", f"2026-01-{(i % 27) + 1:02d}")
             for i in range(SITEMAP_MAX_URLS + 50)]
        ))
        kept = filter_entries(entries)
        self.assertEqual(len(kept), SITEMAP_MAX_URLS)
        firsts = [e.lastmod for e in kept[:3]]
        self.assertTrue(all(m.endswith("-27") for m in firsts))

    def test_title_from_url(self):
        self.assertEqual(
            title_from_url("https://x.example/my-great-post.html"),
            "My great post")
        self.assertEqual(title_from_url("https://x.example/"), "Untitled")


class TestSitemapResolver(unittest.TestCase):
    def test_explicit_sitemap_url(self):
        fetcher = FakeFetcher({
            "https://x.example/sitemap.xml": ("ok", urlset([
                ("https://x.example/first-post", "2026-01-02"),
                ("https://x.example/second-post", "2026-01-01"),
            ])),
        })
        self.assertEqual(
            detect_resolver("https://x.example/sitemap.xml").id, "sitemap")
        sink = run("https://x.example/sitemap.xml", fetcher)
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(feed.format, "sitemap")
        self.assertEqual(len(feed.items), 2)
        item = feed.items[0]
        self.assertTrue(item.title_from_url)
        self.assertEqual(item.title, "First post")
        self.assertEqual(item.content_html, "")

    def test_index_follows_ranked_subs(self):
        fetcher = FakeFetcher({
            "https://x.example/sitemap.xml": ("ok", sitemap_index([
                "https://x.example/page-sitemap.xml",
                "https://x.example/post-sitemap.xml",
            ])),
            "https://x.example/post-sitemap.xml": ("ok", urlset([
                ("https://x.example/a-post", ""),
            ])),
            "https://x.example/page-sitemap.xml": ("ok", urlset([
                ("https://x.example/about-page", ""),
            ])),
        })
        sink = run("https://x.example/sitemap.xml", fetcher)
        self.assertIsNone(sink.error)
        # post-sitemap ranked first, both collected.
        self.assertEqual(fetcher.calls[1], "https://x.example/post-sitemap.xml")
        self.assertEqual(len(sink.result.feed.items), 2)

    def test_empty_sitemap_is_a_clear_error(self):
        fetcher = FakeFetcher({
            "https://x.example/sitemap.xml": ("ok", urlset([])),
        })
        sink = run("https://x.example/sitemap.xml", fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.SITEMAP_EMPTY)


class TestRssSitemapFallback(unittest.TestCase):
    HTML = "<!doctype html><html><head></head><body>no feed here</body></html>"

    def test_feedless_site_falls_back_to_sitemap(self):
        responses = {"https://x.example": ("ok", self.HTML)}
        # Every palette probe answers HTML too (soft-404 style).
        for leaf in ("/feed/", "/feed", "/rss", "/rss.xml", "/atom.xml",
                     "/feed.xml", "/index.xml", "/feed.json"):
            responses[f"https://x.example{leaf}"] = ("ok", self.HTML)
        responses["https://x.example/robots.txt"] = (
            "ok", "User-agent: *\nSitemap: https://x.example/wp-sitemap.xml\n")
        responses["https://x.example/wp-sitemap.xml"] = ("ok", urlset([
            ("https://x.example/2026/big-news", "2026-01-01"),
        ]))
        fetcher = FakeFetcher(responses)
        sink = run("https://x.example", fetcher)
        self.assertIsNone(sink.error)
        self.assertEqual(sink.result.feed.format, "sitemap")
        self.assertEqual(len(sink.result.feed.items), 1)

    def test_feedless_site_without_sitemap_still_fails_cleanly(self):
        fetcher = FakeFetcher({"https://x.example": ("ok", self.HTML)})
        sink = run("https://x.example", fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.NO_FEED_FOUND)


# --------------------------------------------------------------------------- #
# MDX                                                                         #
# --------------------------------------------------------------------------- #

MDX_DOC = """---
title: "My Guide"
author: alice
---
import Widget from '../components/Widget.astro'

# Intro

<Widget prop="x" />

Prose paragraph with **bold** text.{' '}

<div class="note">
  <p>HTML island paragraph.</p>
</div>

```js
export const keep = "<NotAComponent> stays";
```

{/* an mdx comment */}

| a | b |
| - | - |
| 1 | 2 |
"""


class TestMdxConversion(unittest.TestCase):
    def test_frontmatter(self):
        fm, body = parse_frontmatter(MDX_DOC)
        self.assertEqual(fm["title"], "My Guide")
        self.assertEqual(fm["author"], "alice")
        self.assertFalse(body.startswith("---"))

    def test_full_pipeline(self):
        doc = mdx_to_markdown(MDX_DOC)
        self.assertEqual(doc.title, "My Guide")
        self.assertIn("# Intro", doc.markdown)
        self.assertIn("Prose paragraph with **bold** text.", doc.markdown)
        self.assertIn("HTML island paragraph.", doc.markdown)
        self.assertNotIn("Widget", doc.markdown)          # JSX stripped
        self.assertNotIn("import ", doc.markdown)          # ESM stripped
        self.assertNotIn("mdx comment", doc.markdown)      # comments stripped
        self.assertIn("<NotAComponent> stays", doc.markdown)  # fence protected
        self.assertIn("| a | b |", doc.markdown)           # table survives

    def test_prose_starting_with_import_survives(self):
        doc = mdx_to_markdown("import duties are a tax matter, not code.")
        self.assertIn("import duties", doc.markdown)


    def test_front_matter_details(self):
        doc = mdx_to_markdown(
            "---\ntitle: Hi\npubDate: 2024-03-05T10:00:00Z\n"
            "categories: [Bitcoin, Lightning]\nexcerpt: In short.\n"
            "heroImage: ./hero.png\nauthors: [Ann, Ben]\n---\nBody text.",
            base_url="https://raw.githubusercontent.com/o/r/main/blog/hi.md")
        self.assertEqual(doc.published_at, 1709632800)
        self.assertEqual(doc.categories, ("Bitcoin", "Lightning"))
        self.assertEqual(doc.summary, "In short.")
        self.assertEqual(doc.image,
                         "https://raw.githubusercontent.com/o/r/main/blog/hero.png")
        self.assertEqual(doc.author, "Ann, Ben")

    def test_hugo_cover_and_images_list(self):
        papermod = mdx_to_markdown("---\ntitle: A\ncover:\n  image: https://x.example/c.png"
                                   "\n  alt: c\n---\nText")
        self.assertEqual(papermod.image, "https://x.example/c.png")
        listed = mdx_to_markdown("---\ntitle: A\nimages:\n  - https://x.example/i.png\n---\nT")
        self.assertEqual(listed.image, "https://x.example/i.png")
        # A path from the site's root cannot be placed from a raw GitHub file.
        rooted = mdx_to_markdown(
            "---\nimage: /static/c.png\n---\nT",
            base_url="https://raw.githubusercontent.com/o/r/main/c.md")
        self.assertEqual(rooted.image, "")

    def test_title_heading_not_repeated(self):
        from nostr.rss.normalize import item_to_article
        from nostr.imports.resolvers.mdx import _doc_to_item
        item = _doc_to_item("---\ntitle: My Guide\n---\n# My Guide\n\nBody.",
                            fallback_name="guide.md", link="https://example.com/guide.md",
                            guid="")
        article = item_to_article(item, append_source_link=False)
        self.assertEqual(article.content, "Body.")

    def test_inline_code_survives(self):
        doc = mdx_to_markdown("Use `<Button>` with `List<String>` here.")
        self.assertEqual(doc.markdown, "Use `<Button>` with `List<String>` here.")

    def test_paragraph_opening_with_inline_html_keeps_its_markdown(self):
        doc = mdx_to_markdown("<strong>Note:</strong> some **bold** and a [link](https://x.example).")
        self.assertEqual(doc.markdown,
                         "**Note:** some **bold** and a [link](https://x.example).")

    def test_indented_code_kept_in_markdown_not_mdx(self):
        source = "Code:\n\n    <div>kept</div>\n    <Tag />\n\nAfter."
        self.assertIn("    <div>kept</div>\n    <Tag />", mdx_to_markdown(source).markdown)
        # In MDX, indentation nests components: no code block there.
        self.assertNotIn("<Tag", mdx_to_markdown(source, indented_code=False).markdown)

    def test_documented_embed_attributes(self):
        doc = mdx_to_markdown(
            '<YouTube id="https://youtu.be/dQw4w9WgXcQ" />\n\n'
            '<LiteYouTubeEmbed id="abc123XYZ" title="t" />\n\n'
            '<Vimeo vimeoId="76979871" />\n\n<Tweet tweetLink="jack/status/20" />')
        for address in ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=abc123XYZ",
                        "https://vimeo.com/76979871", "https://twitter.com/jack/status/20"):
            self.assertIn(address, doc.markdown)

    def test_ananke_featured_image(self):
        doc = mdx_to_markdown("---\ntitle: A\nfeatured_image: https://x.example/f.png\n---\nT")
        self.assertEqual(doc.image, "https://x.example/f.png")

    def test_embed_components_become_links_and_images(self):
        doc = mdx_to_markdown(
            '<YouTube id="dQw4w9WgXcQ" />\n\n<Vimeo id="76979871" />\n\n'
            "<Tweet id={1234} />\n\n"
            '<Image src="./diagram.png" alt="A diagram" />',
            base_url="https://raw.githubusercontent.com/o/r/main/docs/a.mdx")
        self.assertIn("https://www.youtube.com/watch?v=dQw4w9WgXcQ", doc.markdown)
        self.assertIn("https://vimeo.com/76979871", doc.markdown)
        self.assertIn("https://twitter.com/i/web/status/1234", doc.markdown)
        self.assertIn("![A diagram](https://raw.githubusercontent.com/o/r/main/docs/diagram.png)",
                      doc.markdown)

    def test_relative_images_made_whole(self):
        doc = mdx_to_markdown(
            "![Flow](<./img/flow chart.png>)\n\n![Abs](https://cdn.example/a.png)",
            base_url="https://raw.githubusercontent.com/o/r/main/docs/a.md")
        self.assertIn("![Flow](<https://raw.githubusercontent.com/o/r/main/docs/img/"
                      "flow%20chart.png>)", doc.markdown)
        self.assertIn("![Abs](https://cdn.example/a.png)", doc.markdown)


class TestGithubUrlParsing(unittest.TestCase):
    def test_tree(self):
        self.assertEqual(
            parse_github_tree("https://github.com/o/r/tree/main/docs/guide"),
            ("o", "r", "main", "docs/guide"))
        self.assertIsNone(parse_github_tree("https://github.com/o/r"))


class TestMdxResolver(unittest.TestCase):
    def test_single_document(self):
        fetcher = FakeFetcher({
            "https://example.com/docs/intro.md":
                ("ok", "---\ntitle: Intro\n---\n# Hello\n\nBody."),
        })
        self.assertEqual(
            detect_resolver("https://example.com/docs/intro.md").id, "mdx")
        sink = run("https://example.com/docs/intro.md", fetcher)
        self.assertIsNone(sink.error)
        item = sink.result.feed.items[0]
        self.assertEqual(item.title, "Intro")
        self.assertEqual(item.content_markdown, "# Hello\n\nBody.")

    def test_document_details_and_images_from_raw_address(self):
        fetcher = FakeFetcher({
            "https://raw.githubusercontent.com/o/r/main/docs/a.md":
                ("ok", "---\ntitle: A\ndate: 2024-01-02\ntags: [x]\n"
                       "image: ./cover.png\n---\n![d](./d.png)"),
        })
        sink = run("https://github.com/o/r/blob/main/docs/a.md", fetcher)
        item = sink.result.feed.items[0]
        self.assertEqual(item.published_at, 1704153600)
        self.assertEqual(item.categories, ("x",))
        self.assertEqual(item.image, "https://raw.githubusercontent.com/o/r/main/docs/cover.png")
        self.assertEqual(item.content_markdown,
                         "![d](https://raw.githubusercontent.com/o/r/main/docs/d.png)")

    def test_github_blob_rewritten_to_raw(self):
        fetcher = FakeFetcher({
            "https://raw.githubusercontent.com/o/r/main/docs/a.md":
                ("ok", "# A"),
        })
        sink = run("https://github.com/o/r/blob/main/docs/a.md", fetcher)
        self.assertIsNone(sink.error)
        self.assertEqual(fetcher.calls,
                         ["https://raw.githubusercontent.com/o/r/main/docs/a.md"])
        # The stored source URL stays the human-facing blob URL.
        self.assertEqual(sink.result.url,
                         "https://github.com/o/r/blob/main/docs/a.md")

    def test_github_folder(self):
        listing = [
            {"type": "file", "name": "10-outro.md", "path": "docs/10-outro.md",
             "download_url": "https://raw.example/10-outro.md",
             "html_url": "https://github.com/o/r/blob/main/docs/10-outro.md"},
            {"type": "file", "name": "2-intro.md", "path": "docs/2-intro.md",
             "download_url": "https://raw.example/2-intro.md",
             "html_url": "https://github.com/o/r/blob/main/docs/2-intro.md"},
            {"type": "file", "name": "img.png", "path": "docs/img.png",
             "download_url": "https://raw.example/img.png"},
            {"type": "dir", "name": "sub", "path": "docs/sub"},
        ]
        fetcher = FakeFetcher({
            "https://api.github.com/repos/o/r/contents/docs?ref=main":
                ("ok", json.dumps(listing)),
            "https://raw.example/2-intro.md": ("ok", "# Two"),
            "https://raw.example/10-outro.md": ("ok", "# Ten"),
        })
        self.assertEqual(
            detect_resolver("https://github.com/o/r/tree/main/docs").id, "mdx")
        sink = run("https://github.com/o/r/tree/main/docs", fetcher)
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(feed.title, "r/docs")
        # Numeric-aware ordering: 2 before 10; non-md entries skipped.
        self.assertEqual([i.content_markdown for i in feed.items],
                         ["# Two", "# Ten"])

    def test_folder_with_one_broken_file_keeps_the_rest(self):
        listing = [
            {"type": "file", "name": "a.md", "path": "a.md",
             "download_url": "https://raw.example/a.md"},
            {"type": "file", "name": "b.md", "path": "b.md",
             "download_url": "https://raw.example/b.md"},
        ]
        fetcher = FakeFetcher({
            "https://api.github.com/repos/o/r/contents/docs?ref=main":
                ("ok", json.dumps(listing)),
            "https://raw.example/a.md": ("err", "HTTP 500"),
            "https://raw.example/b.md": ("ok", "# B"),
        })
        sink = run("https://github.com/o/r/tree/main/docs", fetcher)
        self.assertIsNone(sink.error)
        self.assertEqual(len(sink.result.feed.items), 1)

    def test_github_error_message_surfaced(self):
        fetcher = FakeFetcher({
            "https://api.github.com/repos/o/r/contents/docs?ref=main":
                ("ok", json.dumps({"message": "API rate limit exceeded"})),
        })
        with self.assertLogs("nostr.imports.resolvers.mdx", level="INFO") as logged:
            sink = run("https://github.com/o/r/tree/main/docs", fetcher)
        # Lessons 36: GitHub's own words in the log, what to do on screen.
        self.assertEqual(sink.error.message,
                         "GitHub didn't list that folder: check its address and try again.")
        self.assertIn("API rate limit exceeded", "\n".join(logged.output))


# --------------------------------------------------------------------------- #
# Bluesky                                                                     #
# --------------------------------------------------------------------------- #

DID = "did:plc:abc123"


def bsky_post(rkey, text, *, did=DID, handle="alice.bsky.social",
              created="2026-01-01T10:00:00Z", facets=None, embed=None):
    return {
        "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
        "author": {"did": did, "handle": handle, "displayName": "Alice"},
        "record": {"text": text, "createdAt": created, "facets": facets or []},
        **({"embed": embed} if embed else {}),
    }


class TestBlueskyPure(unittest.TestCase):
    def test_url_parsing(self):
        self.assertEqual(
            parse_bluesky_url("https://bsky.app/profile/alice.bsky.social/post/xyz"),
            {"handle": "alice.bsky.social", "rkey": "xyz"})
        self.assertEqual(
            parse_bluesky_url(f"at://{DID}/app.bsky.feed.post/xyz"),
            {"did": DID, "rkey": "xyz"})
        self.assertIsNone(parse_bluesky_url("https://example.com/post/1"))

    def test_facets_slice_on_utf8_bytes(self):
        # "café " is 6 BYTES (the é is two); "link" spans bytes 6..10.
        # Slicing on characters would shift the range and mangle it.
        text = "café link"
        facets = [{
            "index": {"byteStart": 6, "byteEnd": 10},
            "features": [{"$type": "app.bsky.richtext.facet#link",
                          "uri": "https://x.example"}],
        }]
        self.assertEqual(apply_facets(text, facets),
                         "café [link](https://x.example)")

    def test_invalid_facets_skipped(self):
        text = "hello"
        facets = [{"index": {"byteStart": 3, "byteEnd": 99}, "features": []}]
        self.assertEqual(apply_facets(text, facets), "hello")

    def test_image_embed_rendered(self):
        post = bsky_post("a", "look", embed={
            "$type": "app.bsky.embed.images#view",
            "images": [{"fullsize": "https://cdn.example/i.jpg", "alt": "a pic"}],
        })
        self.assertEqual(post_to_markdown(post),
                         "look\n\n![a pic](https://cdn.example/i.jpg)")

    def test_post_text_is_escaped_and_keeps_its_line_breaks(self):
        post = bsky_post("a", "# not a heading\n*stars* and snake_case\n1. not a list")
        self.assertEqual(
            post_to_markdown(post),
            "\\# not a heading  \n\\*stars\\* and snake\\_case  \n1\\. not a list")
        # The title is plain text, without the escapes.
        self.assertEqual(derive_thread_title(post_to_markdown(post)), "# not a heading")

    def test_link_text_escaped_inside_the_link(self):
        text = "see my_page"
        facets = [{
            "index": {"byteStart": 4, "byteEnd": 11},
            "features": [{"$type": "app.bsky.richtext.facet#link",
                          "uri": "https://x.example/a_(b)"}],
        }]
        self.assertEqual(apply_facets(text, facets),
                         "see [my\\_page](https://x.example/a_%28b%29)")

    def test_video_embed_links_to_the_post(self):
        post = bsky_post("a", "watch", embed={
            "$type": "app.bsky.embed.video#view",
            "playlist": "https://video.bsky.app/watch/a/playlist.m3u8",
            "alt": "A cat",
        })
        self.assertEqual(
            post_to_markdown(post),
            "watch\n\n[Video: A cat](https://bsky.app/profile/alice.bsky.social/post/a)")

    def test_collect_self_thread_walks_up_and_down(self):
        root = bsky_post("1", "first")
        middle = bsky_post("2", "second")
        stranger = bsky_post("x", "not mine", did="did:plc:other")
        last = bsky_post("3", "third")
        thread = {
            "post": middle["record"] and middle,  # requested post is mid-thread
            "parent": {"post": root},
            "replies": [
                {"post": stranger},
                {"post": last, "replies": []},
            ],
        }
        posts = collect_self_thread(thread)
        self.assertEqual([p["uri"].rsplit("/", 1)[-1] for p in posts],
                         ["1", "2", "3"])


class TestBlueskyResolver(unittest.TestCase):
    def _fetcher_for_thread(self, thread):
        from urllib.parse import quote
        thread_url = (
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
            f"?uri={quote(f'at://{DID}/app.bsky.feed.post/xyz')}"
            "&depth=80&parentHeight=80"
        )
        return FakeFetcher({
            "https://public.api.bsky.app/xrpc/com.atproto.identity."
            "resolveHandle?handle=alice.bsky.social":
                ("ok", json.dumps({"did": DID})),
            thread_url: ("ok", json.dumps({"thread": thread})),
        })

    def test_detection(self):
        self.assertEqual(
            detect_resolver(
                "https://bsky.app/profile/alice.bsky.social/post/xyz").id,
            "bluesky")

    def test_thread_stitches_to_single_item(self):
        thread = {
            "post": bsky_post("xyz", "first post of the thread"),
            "replies": [{
                "post": bsky_post("r2", "second post continues"),
                "replies": [],
            }],
        }
        sink = run("https://bsky.app/profile/alice.bsky.social/post/xyz",
                   self._fetcher_for_thread(thread))
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(feed.format, "bluesky")
        self.assertIn("Alice", feed.title)
        self.assertIn("2 posts", feed.title)
        item = feed.items[0]
        self.assertIn("first post of the thread", item.content_markdown)
        self.assertIn("second post continues", item.content_markdown)
        self.assertEqual(item.categories, ("bluesky",))

    def test_thread_hashtags_become_categories(self):
        tagged = bsky_post("xyz", "about #Bitcoin today", facets=[{
            "index": {"byteStart": 6, "byteEnd": 14},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "Bitcoin"}],
        }])
        thread = {"post": tagged, "replies": [{
            "post": bsky_post("r2", "more #bitcoin", facets=[{
                "index": {"byteStart": 5, "byteEnd": 13},
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "bitcoin"}],
            }]),
            "replies": [],
        }]}
        sink = run("https://bsky.app/profile/alice.bsky.social/post/xyz",
                   self._fetcher_for_thread(thread))
        from nostr.rss.normalize import item_to_article
        self.assertEqual(item_to_article(sink.result.feed.items[0]).hashtags,
                         ("bitcoin", "bluesky"))

    def test_single_post_rejected(self):
        thread = {"post": bsky_post("xyz", "just one"), "replies": []}
        sink = run("https://bsky.app/profile/alice.bsky.social/post/xyz",
                   self._fetcher_for_thread(thread))
        self.assertEqual(sink.error.code, ERROR_CODES.BLUESKY_NOT_THREAD)

    def test_unresolvable_handle(self):
        fetcher = FakeFetcher({
            "https://public.api.bsky.app/xrpc/com.atproto.identity."
            "resolveHandle?handle=alice.bsky.social":
                ("ok", json.dumps({"error": "nope"})),
        })
        sink = run("https://bsky.app/profile/alice.bsky.social/post/xyz",
                   fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.BLUESKY_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
