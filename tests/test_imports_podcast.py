# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Podcasting 2.0 support: extraction, enrichment, chapters,
Markdown rendering, and the pipeline's podcast branch."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nostr.imports.registry import resolve_source
from nostr.imports.sources.podcast import (
    build_podcast_markdown,
    Chapter,
    enrich_feed_with_podcast,
    extract_podcast_feed,
    fetch_podcast_chapters,
    format_duration,
    looks_like_podcast_feed,
    normalize_duration,
    parse_chapters_json,
    PodcastEpisode,
)
from nostr.rss.parser import parse_feed

from tests.imports_fakes import FakeFetcher


PODCAST_XML = """<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <title>The Show</title>
  <link>https://show.example</link>
  <description>d</description>
  <itunes:image href="https://show.example/cover.png"/>
  <podcast:guid>abc-123</podcast:guid>
  <podcast:value type="lightning" method="keysend">
    <podcast:valueRecipient name="Host" type="node" address="02aabb" split="90"/>
    <podcast:valueRecipient name="Fee" type="node" address="02ffff" split="5" fee="true"/>
  </podcast:value>
  <item>
    <title>Episode One</title>
    <link>https://show.example/e1</link>
    <guid isPermaLink="false">ep-1</guid>
    <description>Show notes for one.</description>
    <enclosure url="https://cdn.example/e1.mp3" type="audio/mpeg" length="1000"/>
    <itunes:duration>1:02:03</itunes:duration>
    <itunes:episode>1</itunes:episode>
    <itunes:season>2</itunes:season>
    <podcast:chapters url="https://cdn.example/e1-chapters.json"
                      type="application/json+chapters"/>
    <podcast:transcript url="https://cdn.example/e1.srt"
                        type="application/x-subrip"/>
    <podcast:transcript url="https://cdn.example/e1.vtt" type="text/vtt"/>
  </item>
  <item>
    <title>Blog Post</title>
    <guid isPermaLink="false">blog-1</guid>
    <description>Just text, no audio.</description>
  </item>
</channel></rss>
"""

CHAPTERS_JSON = json.dumps({"version": "1.2.0", "chapters": [
    {"startTime": 754, "title": "Main topic"},
    {"startTime": 0, "title": "Intro"},
    {"startTime": 120, "title": "Hidden", "toc": False},
    {"startTime": "bad"},
]})


class TestDurations(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_duration("3480"), 3480)
        self.assertEqual(normalize_duration("58:00"), 3480)
        self.assertEqual(normalize_duration("1:02:33"), 3753)
        self.assertEqual(normalize_duration("junk"), 0)
        self.assertEqual(normalize_duration(""), 0)

    def test_format(self):
        self.assertEqual(format_duration(3753), "1:02:33")
        self.assertEqual(format_duration(754), "12:34")
        self.assertEqual(format_duration(0), "0:00")


class TestExtraction(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(looks_like_podcast_feed(PODCAST_XML))
        self.assertFalse(looks_like_podcast_feed("<rss><channel/></rss>"))

    def test_channel_and_items(self):
        channel, items = extract_podcast_feed(PODCAST_XML)
        self.assertEqual(channel["title"], "The Show")
        self.assertEqual(channel["guid"], "abc-123")
        # Fee recipient excluded; proportional split kept.
        self.assertEqual(len(channel["value"]), 1)
        self.assertEqual(channel["value"][0].split, 90)

        self.assertEqual(len(items), 1)  # blog item has no audio
        episode = items[0]
        self.assertEqual(episode["audio"], "https://cdn.example/e1.mp3")
        self.assertEqual(episode["duration"], 3723)
        self.assertEqual(episode["episode"], 1)
        self.assertEqual(episode["season"], 2)
        # vtt preferred over srt regardless of order.
        self.assertEqual(episode["transcript"], "https://cdn.example/e1.vtt")

    def test_enrichment_attaches_payload_and_hero_image(self):
        feed = enrich_feed_with_podcast(parse_feed(PODCAST_XML), PODCAST_XML)
        episode_item = next(i for i in feed.items if i.guid == "ep-1")
        blog_item = next(i for i in feed.items if i.guid == "blog-1")
        self.assertIsNotNone(episode_item.podcast)
        self.assertEqual(episode_item.podcast.show, "The Show")
        self.assertEqual(episode_item.image, "https://show.example/cover.png")
        self.assertIsNone(blog_item.podcast)

    def test_non_podcast_feed_passes_through(self):
        from tests.imports_fakes import TWO_ITEM_FEED
        feed = parse_feed(TWO_ITEM_FEED)
        self.assertIs(enrich_feed_with_podcast(feed, TWO_ITEM_FEED), feed)

    def test_resolver_enriches_at_preview(self):
        fetcher = FakeFetcher({"https://show.example/feed": ("ok", PODCAST_XML)})
        out = {}
        resolve_source(
            "https://show.example/feed",
            fetcher=fetcher,
            on_success=lambda r: out.update(result=r),
            on_failure=lambda e: out.update(error=e),
        )
        feed = out["result"].feed
        episode_item = next(i for i in feed.items if i.guid == "ep-1")
        self.assertIsNotNone(episode_item.podcast)


class TestChapters(unittest.TestCase):
    def test_parse_filters_sorts_and_keeps_toc(self):
        chapters = parse_chapters_json(CHAPTERS_JSON)
        self.assertEqual([c.title for c in chapters], ["Intro", "Main topic"])
        self.assertEqual(chapters[1].start, 754)

    def test_parse_garbage(self):
        self.assertEqual(parse_chapters_json("nope"), [])
        self.assertEqual(parse_chapters_json(""), [])

    def test_fetch_happy_path(self):
        fetcher = FakeFetcher({
            "https://cdn.example/ch.json": ("ok", CHAPTERS_JSON)})
        out = {}
        fetch_podcast_chapters(fetcher, "https://cdn.example/ch.json",
                               lambda c: out.update(chapters=c))
        self.assertEqual(len(out["chapters"]), 2)

    def test_fetch_unwraps_analytics_prefix(self):
        wrapped = "https://tracker.example/x/https://cdn.example/ch.json"
        fetcher = FakeFetcher({
            wrapped: ("err", "HTTP 404"),
            "https://cdn.example/ch.json": ("ok", CHAPTERS_JSON),
        })
        out = {}
        fetch_podcast_chapters(fetcher, wrapped,
                               lambda c: out.update(chapters=c))
        self.assertEqual(len(out["chapters"]), 2)

    def test_fetch_dead_url_yields_empty(self):
        out = {}
        fetch_podcast_chapters(FakeFetcher(), "https://dead.example/ch.json",
                               lambda c: out.update(chapters=c))
        self.assertEqual(out["chapters"], [])


class TestMarkdown(unittest.TestCase):
    def test_full_render(self):
        episode = PodcastEpisode(
            audio="https://cdn.example/e1.mp3", duration=3723,
            show="The Show", episode=1, season=2,
            transcript="https://cdn.example/e1.vtt")
        markdown = build_podcast_markdown(
            episode,
            chapters=[Chapter(0, "Intro"), Chapter(754, "Main topic")],
            notes="The show notes.",
        )
        self.assertIn("**The Show**", markdown)
        self.assertIn("Season 2", markdown)
        self.assertIn("Episode 1", markdown)
        self.assertIn("1:02:03", markdown)
        self.assertIn("[Listen to the episode](https://cdn.example/e1.mp3)", markdown)
        self.assertIn("## Chapters", markdown)
        self.assertIn("- **12:34** Main topic", markdown)
        self.assertIn("[Transcript](https://cdn.example/e1.vtt)", markdown)
        self.assertTrue(markdown.endswith("The show notes."))

    def test_minimal_render(self):
        markdown = build_podcast_markdown(
            PodcastEpisode(audio="https://cdn.example/e.mp3"))
        self.assertEqual(
            markdown, "[Listen to the episode](https://cdn.example/e.mp3)")


class TestPipelineBranch(unittest.TestCase):
    def test_podcast_item_renders_episode_body(self):
        from tests.imports_fakes import (
            FakeLongFormFetcher, make_factory,
        )
        from tests.test_imports_pipeline import make_job

        feed = enrich_feed_with_podcast(parse_feed(PODCAST_XML), PODCAST_XML)
        episode_item = next(i for i in feed.items if i.guid == "ep-1")
        factory, created = make_factory()
        pages = FakeFetcher({
            "https://cdn.example/e1-chapters.json": ("ok", CHAPTERS_JSON)})
        long_form = FakeLongFormFetcher({"content": "should never be used"})
        job = make_job([episode_item], factory=factory, page_fetcher=pages,
                       long_form=long_form)
        job.start()

        content = created[0].inner_event["content"]
        self.assertIn("[Listen to the episode](https://cdn.example/e1.mp3)",
                      content)
        self.assertIn("- **0:00** Intro", content)
        self.assertIn("Show notes for one.", content)
        self.assertIn("Originally published at", content)  # footer kept
        # The podcast branch precludes naddr/full-text recovery.
        self.assertEqual(long_form.calls, [])
        self.assertEqual(pages.calls,
                         ["https://cdn.example/e1-chapters.json"])


if __name__ == "__main__":
    unittest.main()
