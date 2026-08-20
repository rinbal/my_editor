# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nostr.imports.images``: scans, rewrite, mirror loop.

The mirror transport is a synchronous fake, so the whole loop settles
deterministically without network, Blossom, or a signer.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.imports.images import (
    image_label,
    rehost_images,
    rewrite_markdown_images,
    scan_html_images,
    scan_markdown_images,
)


MD = (
    "# Post\n\n"
    "![banner](https://a.example/banner.png)\n\n"
    "Some prose.\n\n"
    "![inline](https://a.example/photo%20one.jpg)\n\n"
    "![banner again](https://a.example/banner.png)\n\n"
    "![inline data](data:image/png;base64,AAAA)\n"
)


def fake_mirror(results=None):
    """url -> ("ok", mirrored_url) | ("err", reason); default mirrors."""
    table = dict(results or {})
    calls = []

    def mirror(url, on_success, on_failure):
        calls.append(url)
        kind, payload = table.get(
            url, ("ok", f"https://blossom.example/{len(calls)}"))
        if kind == "ok":
            on_success(payload)
        else:
            on_failure(payload)

    return mirror, calls


def run_rehost(markdown, mirror, **kwargs):
    out = {}
    progress = []
    rehost_images(
        markdown,
        mirror=mirror,
        on_progress=progress.append,
        on_done=lambda o: out.update(outcome=o),
        **kwargs,
    )
    assert "outcome" in out, "on_done never fired"
    return out["outcome"], progress


class TestScans(unittest.TestCase):
    def test_markdown_scan_unique_in_order_skipping_data(self):
        self.assertEqual(scan_markdown_images(MD), [
            "https://a.example/banner.png",
            "https://a.example/photo%20one.jpg",
        ])

    def test_html_scan(self):
        html = (
            '<p><img src="https://a.example/x.png"></p>'
            '<img alt="y" src="https://a.example/y.png"/>'
            '<img src="https://a.example/x.png">'
            '<img src="data:image/gif;base64,AA">'
        )
        self.assertEqual(scan_html_images(html), [
            "https://a.example/x.png",
            "https://a.example/y.png",
        ])

    def test_empty_inputs(self):
        self.assertEqual(scan_markdown_images(""), [])
        self.assertEqual(scan_html_images(None), [])

    def test_image_label(self):
        self.assertEqual(
            image_label("https://a.example/photo%20one.jpg"), "photo one.jpg")
        self.assertEqual(image_label("https://a.example"), "a.example")
        self.assertLessEqual(len(image_label("https://a.example/" + "x" * 200)), 80)


class TestRewrite(unittest.TestCase):
    def test_rewrites_mapped_and_keeps_unmapped(self):
        mapping = {"https://a.example/banner.png": "https://b.example/h1"}
        out = rewrite_markdown_images(MD, mapping)
        self.assertIn("![banner](https://b.example/h1)", out)
        self.assertIn("![banner again](https://b.example/h1)", out)
        self.assertIn("![inline](https://a.example/photo%20one.jpg)", out)

    def test_empty_mapping_is_identity(self):
        self.assertEqual(rewrite_markdown_images(MD, {}), MD)


class TestRehostLoop(unittest.TestCase):
    def test_happy_path_mirrors_unique_urls_once(self):
        mirror, calls = fake_mirror()
        outcome, progress = run_rehost(MD, mirror)
        # Two unique http URLs; the duplicate and the data: URI don't fetch.
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome.mirrored, 2)
        self.assertEqual(outcome.failed, [])
        self.assertIn("https://blossom.example/1", outcome.markdown)
        self.assertIn("https://blossom.example/2", outcome.markdown)
        self.assertNotIn("![banner](https://a.example/banner.png)",
                         outcome.markdown)

    def test_progress_lifecycle(self):
        mirror, _calls = fake_mirror()
        _outcome, progress = run_rehost(MD, mirror)
        statuses = [(p.index, p.status) for p in progress]
        # Seeded queued for both, then per-image mirroring/mirrored.
        self.assertEqual(statuses[:2], [(0, "queued"), (1, "queued")])
        self.assertIn((0, "mirroring"), statuses)
        self.assertIn((0, "mirrored"), statuses)
        self.assertIn((1, "mirrored"), statuses)
        self.assertTrue(all(p.total == 2 for p in progress))
        self.assertTrue(all(p.label for p in progress))

    def test_one_failure_keeps_original_and_continues(self):
        mirror, _ = fake_mirror({
            "https://a.example/banner.png": ("err", "server said no"),
        })
        outcome, progress = run_rehost(MD, mirror)
        self.assertEqual(outcome.mirrored, 1)
        self.assertEqual(outcome.failed, ["https://a.example/banner.png"])
        self.assertIn("![banner](https://a.example/banner.png)", outcome.markdown)
        self.assertNotIn("![inline](https://a.example/photo%20one.jpg)",
                         outcome.markdown)
        failed = [p for p in progress if p.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error, "server said no")

    def test_empty_mirror_response_counts_as_failure(self):
        mirror, _ = fake_mirror({
            "https://a.example/banner.png": ("ok", ""),
        })
        outcome, _ = run_rehost(MD, mirror)
        self.assertEqual(outcome.failed, ["https://a.example/banner.png"])

    def test_mirror_raising_counts_as_failure_and_continues(self):
        def broken(url, on_success, on_failure):
            if "banner" in url:
                raise RuntimeError("transport bug")
            on_success("https://blossom.example/ok")

        outcome, _ = run_rehost(MD, broken)
        self.assertEqual(outcome.mirrored, 1)
        self.assertEqual(len(outcome.failed), 1)

    def test_skip_urls_never_mirrored(self):
        mirror, calls = fake_mirror()
        outcome, _ = run_rehost(
            MD, mirror, skip_urls={"https://a.example/banner.png"})
        self.assertEqual(calls, ["https://a.example/photo%20one.jpg"])
        self.assertIn("![banner](https://a.example/banner.png)", outcome.markdown)

    def test_no_images_short_circuits(self):
        mirror, calls = fake_mirror()
        outcome, progress = run_rehost("plain text, no images", mirror)
        self.assertEqual(calls, [])
        self.assertEqual(progress, [])
        self.assertEqual(outcome.markdown, "plain text, no images")

    def test_cancellation_stops_but_still_delivers(self):
        cancelled = {"flag": False}

        def mirror(url, on_success, on_failure):
            cancelled["flag"] = True  # cancel after the first mirror
            on_success("https://blossom.example/first")

        outcome, _ = run_rehost(
            MD, mirror, is_cancelled=lambda: cancelled["flag"])
        # First image mirrored and rewritten; second never attempted.
        self.assertEqual(outcome.mirrored, 1)
        self.assertIn("https://blossom.example/first", outcome.markdown)
        self.assertIn("![inline](https://a.example/photo%20one.jpg)",
                      outcome.markdown)


if __name__ == "__main__":
    unittest.main()
