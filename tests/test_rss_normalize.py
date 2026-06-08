"""Unit tests for ``nostr.rss.normalize``."""

from __future__ import annotations

import unittest

from nostr.rss.normalize import (
    ArticleTemplate,
    html_to_markdown,
    item_to_article,
)
from nostr.rss.parser import FeedItem


def _item(**overrides) -> FeedItem:
    defaults: dict = dict(
        guid="g-1",
        title="Sample post",
        link="https://example.com/p/1",
        summary="A short summary.",
        content_html="<h1>Hello</h1><p>Body <em>here</em>.</p>",
        published_at=1741773600,
        categories=("tech", "nostr"),
        image="https://example.com/cover.jpg",
        author="Alice",
    )
    defaults.update(overrides)
    return FeedItem(**defaults)


class HtmlToMarkdownTests(unittest.TestCase):
    def test_atx_headings(self) -> None:
        self.assertEqual(html_to_markdown("<h2>Hi</h2>").strip(), "## Hi")

    def test_bullets_use_dash(self) -> None:
        md = html_to_markdown("<ul><li>a</li><li>b</li></ul>")
        self.assertIn("- a", md)
        self.assertIn("- b", md)

    def test_emphasis_uses_underscore(self) -> None:
        md = html_to_markdown("<p><em>foo</em></p>")
        self.assertIn("_foo_", md)

    def test_strong_uses_double_underscore(self) -> None:
        md = html_to_markdown("<p><strong>bar</strong></p>")
        self.assertIn("__bar__", md)

    def test_empty_input_returns_empty_string(self) -> None:
        self.assertEqual(html_to_markdown(""), "")


class ItemToArticleTests(unittest.TestCase):
    def test_returns_template_dataclass(self) -> None:
        template = item_to_article(_item())
        self.assertIsInstance(template, ArticleTemplate)
        self.assertEqual(template.title, "Sample post")
        self.assertEqual(template.summary, "A short summary.")
        self.assertEqual(template.image, "https://example.com/cover.jpg")
        self.assertEqual(template.published_at, 1741773600)

    def test_slug_is_deterministic_for_same_inputs(self) -> None:
        a = item_to_article(_item())
        b = item_to_article(_item())
        self.assertEqual(a.slug, b.slug)

    def test_slug_uses_prefix_when_provided(self) -> None:
        template = item_to_article(_item(), identifier_prefix="rss-")
        self.assertTrue(template.slug.startswith("rss-"))

    def test_categories_become_hashtags_lowercased(self) -> None:
        template = item_to_article(_item(categories=("Tech", "Nostr")))
        self.assertEqual(template.hashtags, ("tech", "nostr"))

    def test_extra_hashtags_merged_and_deduped(self) -> None:
        template = item_to_article(
            _item(categories=("tech",)),
            hashtags=("Tech", "Imported"),
        )
        self.assertEqual(template.hashtags, ("tech", "imported"))

    def test_hashtag_hash_prefix_stripped(self) -> None:
        template = item_to_article(_item(categories=("#bitcoin",)))
        self.assertEqual(template.hashtags, ("bitcoin",))

    def test_source_footer_appended_by_default(self) -> None:
        template = item_to_article(_item())
        self.assertIn("Originally published at", template.content)
        self.assertIn("https://example.com/p/1", template.content)

    def test_source_footer_skipped_when_opted_out(self) -> None:
        template = item_to_article(_item(), append_source_link=False)
        self.assertNotIn("Originally published at", template.content)

    def test_source_footer_skipped_when_no_link(self) -> None:
        template = item_to_article(_item(link=None))
        self.assertNotIn("Originally published at", template.content)

    def test_missing_summary_becomes_empty_string(self) -> None:
        template = item_to_article(_item(summary=None))
        self.assertEqual(template.summary, "")

    def test_missing_image_becomes_empty_string(self) -> None:
        template = item_to_article(_item(image=None))
        self.assertEqual(template.image, "")

    def test_empty_html_yields_just_the_footer(self) -> None:
        template = item_to_article(_item(content_html=""))
        self.assertTrue(template.content.startswith("---"))
        self.assertIn("Originally published at", template.content)


if __name__ == "__main__":
    unittest.main()
