# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.imports.fulltext`` (Readability extraction)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.imports.fulltext import extract_readable_content


def _article_page(paragraphs: int = 20) -> str:
    body = "".join(
        f"<p>Paragraph {i} with plenty of ordinary prose words to satisfy "
        f"readability scoring in a deterministic way here.</p>"
        for i in range(paragraphs)
    )
    return (
        "<html><head><title>The Real Article Title</title></head><body>"
        f"<nav>site menu chrome</nav><article>{body}"
        '<img src="/images/pic.png"><a href="/other">rel link</a>'
        "</article><footer>footer chrome</footer></body></html>"
    )


class TestExtraction(unittest.TestCase):
    def test_extracts_main_content_and_title(self):
        result = extract_readable_content(_article_page())
        self.assertIsNotNone(result)
        self.assertIn("Paragraph 3", result.html)
        self.assertEqual(result.title, "The Real Article Title")
        self.assertGreater(result.text_length, 500)

    def test_strips_nav_and_footer_chrome(self):
        result = extract_readable_content(_article_page())
        self.assertNotIn("site menu chrome", result.html)
        self.assertNotIn("footer chrome", result.html)

    def test_absolutises_relative_urls_against_page_url(self):
        result = extract_readable_content(
            _article_page(), url="https://example.com/post/")
        self.assertIn("https://example.com/images/pic.png", result.html)
        self.assertIn("https://example.com/other", result.html)

    def test_empty_and_non_string_input(self):
        self.assertIsNone(extract_readable_content(""))
        self.assertIsNone(extract_readable_content(None))
        self.assertIsNone(extract_readable_content(1234))

    def test_garbage_input_returns_none_or_tiny(self):
        result = extract_readable_content("not html at all")
        # Either rejected outright or extracted as a tiny fragment the
        # pipeline's adopt-only-if-longer guard will discard.
        if result is not None:
            self.assertLess(result.text_length, 80)


if __name__ == "__main__":
    unittest.main()
