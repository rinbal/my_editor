# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orchestration tests for ``nostr.imports.pipeline.ImportItemsJob``.

Drives the per-item import state machine through its injectable seams
(long-form fetcher, publish-job factory, relay-list cache, executor,
pacer), so no network, relays, signer, threads, or timers are involved
and every test is deterministic. Guards:

- signal ordering and accounting (started / saved / published / failed /
  progress / completed),
- the ``rss-`` identifier prefix and its grandfathering migration,
- the ``source`` tag on the inner event,
- per-item failure never stopping the batch,
- batch pacing above the threshold (and not below it),
- cancellation (mid-item, mid-pause, and late settles after cancel),
- NIP-23 long-form resolution: nostr: URIs always resolve, HTTP links
  with embedded naddrs resolve only for thin bodies, all failure paths
  fall back to the feed body,
- relay-acceptance reporting incl. the zero-acceptance case.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from nostr.bech32 import encode_naddr
from nostr.imports import pipeline as pipeline_mod
from nostr.imports.constants import (
    BATCH_PACE_MS,
    IDENTIFIER_PREFIX,
    SOURCE_TAG,
)
from nostr.imports.pipeline import ImportItemsJob
from nostr.rss.dtag import derive_identifier

from tests.imports_fakes import (
    PROFILE,
    RESULTS_OK,
    FakeFetcher,
    FakeLongFormFetcher,
    FakeRelayListCache,
    RecordingPacer,
    inline_run_blocking,
    make_factory,
    make_item,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


FEED_URL = "https://example.com/feed"


def make_job(items, *, factory=None, long_form=None, pacer=None,
             page_fetcher=None, feed_url=FEED_URL, **kwargs):
    if factory is None:
        factory, _ = make_factory()
    return ImportItemsJob(
        items=items,
        feed_url=feed_url,
        profile=PROFILE,
        relay_pool=None,
        relay_list_cache=FakeRelayListCache(),
        session_pool=None,
        fetcher=page_fetcher or FakeFetcher(),
        long_form_fetcher=long_form or FakeLongFormFetcher(None),
        publish_job_factory=factory,
        run_blocking=inline_run_blocking,
        pacer=pacer or RecordingPacer(),
        **kwargs,
    )


class Recorder:
    def __init__(self, job):
        self.events = []
        job.item_started.connect(lambda i, t: self.events.append(("item_started", i, t)))
        job.item_resolving_from_nostr.connect(
            lambda i, t: self.events.append(("item_resolving", i, t)))
        job.item_extracting.connect(
            lambda i, t: self.events.append(("item_extracting", i, t)))
        job.item_mirroring.connect(
            lambda i, m, f, t: self.events.append(("item_mirroring", i, m, f, t)))
        job.item_succeeded.connect(
            lambda i, s: self.events.append(("item_succeeded", i, s)))
        job.item_published.connect(
            lambda i, a, t: self.events.append(("item_published", i, a, t)))
        job.item_failed.connect(lambda i, r: self.events.append(("item_failed", i, r)))
        job.progress.connect(lambda d, t: self.events.append(("progress", d, t)))
        job.completed.connect(lambda s, a: self.events.append(("completed", s, a)))

    def of(self, name):
        return [e for e in self.events if e[0] == name]


TWO_ITEMS = [
    make_item("First", guid="g1", link="https://example.com/a"),
    make_item("Second", guid="g2", link="https://example.com/b"),
]


# --------------------------------------------------------------------------- #
# Happy path + accounting                                                     #
# --------------------------------------------------------------------------- #

class TestHappyPath:
    def test_two_items_import_in_order(self):
        job = make_job(TWO_ITEMS)
        rec = Recorder(job)
        job.start()
        assert [e[1] for e in rec.of("item_started")] == [0, 1]
        assert [e[1] for e in rec.of("item_succeeded")] == [0, 1]
        assert rec.of("completed") == [("completed", 2, 2)]
        assert rec.of("progress") == [("progress", 1, 2), ("progress", 2, 2)]

    def test_saved_then_published_order_per_item(self):
        job = make_job([make_item("Only", guid="g1")])
        rec = Recorder(job)
        job.start()
        names = [e[0] for e in rec.events]
        assert names.index("item_succeeded") < names.index("item_published")

    def test_item_published_reports_relay_acceptance(self):
        job = make_job(TWO_ITEMS)
        rec = Recorder(job)
        job.start()
        assert rec.of("item_published") == [
            ("item_published", 0, 1, 2),
            ("item_published", 1, 1, 2),
        ]

    def test_zero_relay_acceptance_still_reported(self):
        factory, _ = make_factory([
            ("ok", [("wss://a.example", False, "rejected")]),
        ])
        job = make_job([make_item("Only", guid="g1")], factory=factory)
        rec = Recorder(job)
        job.start()
        assert rec.of("item_published") == [("item_published", 0, 0, 1)]
        # Stash-time accounting is unaffected (publisher.py invariant:
        # failed never fires after stashed).
        assert rec.of("completed") == [("completed", 1, 1)]

    def test_per_item_failure_continues_batch(self):
        factory, _ = make_factory([("fail", "signer said no")])
        job = make_job(TWO_ITEMS, factory=factory)
        rec = Recorder(job)
        job.start()
        assert rec.of("item_failed") == [("item_failed", 0, "signer said no")]
        assert [e[1] for e in rec.of("item_succeeded")] == [1]
        assert rec.of("completed") == [("completed", 1, 2)]

    def test_unnormalisable_item_fails_cleanly(self):
        # No guid, link, or title: nothing to derive a d-tag from.
        bad = make_item("", guid="", link=None)
        job = make_job([bad, make_item("Good", guid="g2")])
        rec = Recorder(job)
        job.start()
        (failure,) = rec.of("item_failed")
        assert failure[1] == 0
        assert "Could not normalise item" in failure[2]
        assert rec.of("completed") == [("completed", 1, 2)]

    def test_empty_item_list_completes_immediately(self):
        cache = FakeRelayListCache()
        job = ImportItemsJob(
            items=[], feed_url=FEED_URL, profile=PROFILE, relay_pool=None,
            relay_list_cache=cache, session_pool=None,
            long_form_fetcher=FakeLongFormFetcher(None),
            publish_job_factory=make_factory()[0],
            run_blocking=inline_run_blocking, pacer=RecordingPacer(),
        )
        rec = Recorder(job)
        job.start()
        assert rec.of("completed") == [("completed", 0, 0)]
        assert cache.calls == []  # no pointless relay-list round-trip


# --------------------------------------------------------------------------- #
# Inner event: prefix, migration, source tag                                  #
# --------------------------------------------------------------------------- #

class TestInnerEvent:
    def test_identifier_carries_prefix_by_default(self):
        factory, created = make_factory()
        job = make_job([make_item("Only", guid="g1")], factory=factory)
        job.start()
        expected = derive_identifier(guid="g1", prefix=IDENTIFIER_PREFIX)
        assert created[0].identifier == expected
        assert created[0].identifier.startswith(IDENTIFIER_PREFIX)
        assert ["d", expected] in created[0].inner_event["tags"]

    def test_migration_reuses_existing_bare_identifier(self):
        bare = derive_identifier(guid="g1")
        factory, created = make_factory()
        job = make_job(
            [make_item("Only", guid="g1")],
            factory=factory,
            identifier_exists=lambda d: d == bare,
        )
        job.start()
        assert created[0].identifier == bare

    def test_migration_keeps_prefix_when_no_existing_draft(self):
        factory, created = make_factory()
        job = make_job(
            [make_item("Only", guid="g1")],
            factory=factory,
            identifier_exists=lambda d: False,
        )
        job.start()
        assert created[0].identifier.startswith(IDENTIFIER_PREFIX)

    def test_migration_probe_failure_keeps_prefix(self):
        def broken(_d):
            raise RuntimeError("store unavailable")
        factory, created = make_factory()
        job = make_job(
            [make_item("Only", guid="g1")],
            factory=factory,
            identifier_exists=broken,
        )
        rec = Recorder(job)
        job.start()
        assert created[0].identifier.startswith(IDENTIFIER_PREFIX)
        assert rec.of("completed") == [("completed", 1, 1)]

    def test_source_tag_written_on_inner_event(self):
        factory, created = make_factory()
        job = make_job([make_item("Only", guid="g1")], factory=factory)
        job.start()
        assert [SOURCE_TAG, FEED_URL] in created[0].inner_event["tags"]

    def test_no_source_tag_without_feed_url(self):
        factory, created = make_factory()
        job = make_job([make_item("Only", guid="g1")], factory=factory,
                       feed_url="")
        job.start()
        tags = created[0].inner_event["tags"]
        assert not any(t[0] == SOURCE_TAG for t in tags)

    def test_inner_event_is_kind_30023(self):
        factory, created = make_factory()
        job = make_job(TWO_ITEMS, factory=factory)
        job.start()
        assert all(j.inner_event["kind"] == 30023 for j in created)


# --------------------------------------------------------------------------- #
# Pacing                                                                      #
# --------------------------------------------------------------------------- #

class TestPacing:
    def test_large_batch_paces_between_items(self):
        pacer = RecordingPacer()
        items = [make_item(f"P{i}", guid=f"g{i}") for i in range(6)]
        job = make_job(items, pacer=pacer)
        rec = Recorder(job)
        job.start()
        # 6 items over the threshold of 5: a pause before every item
        # except the first.
        assert pacer.calls == [BATCH_PACE_MS] * 5
        assert rec.of("completed") == [("completed", 6, 6)]

    def test_small_batch_never_paces(self):
        pacer = RecordingPacer()
        items = [make_item(f"P{i}", guid=f"g{i}") for i in range(5)]
        job = make_job(items, pacer=pacer)
        job.start()
        assert pacer.calls == []

    def test_cancel_during_pace_stops_batch(self):
        pacer = RecordingPacer(immediate=False)
        items = [make_item(f"P{i}", guid=f"g{i}") for i in range(6)]
        job = make_job(items, pacer=pacer)
        rec = Recorder(job)
        job.start()
        # First item done; the pause before item 2 is parked.
        assert len(pacer.pending) == 1
        job.cancel()
        before = list(rec.events)
        pacer.pending[0]()  # the timer fires after cancel
        assert rec.events == before
        assert rec.of("completed") == []


# --------------------------------------------------------------------------- #
# Cancellation                                                                #
# --------------------------------------------------------------------------- #

class TestCancellation:
    def test_cancel_mid_item_stops_everything(self):
        factory, created = make_factory([("pending", None)])
        job = make_job(TWO_ITEMS, factory=factory)
        rec = Recorder(job)
        job.start()
        assert len(created) == 1
        job.cancel()
        assert created[0].cancelled is True
        before = list(rec.events)
        # A late settle after cancel must not leak signals or advance.
        created[0].stashed.emit("x", "y", 1)
        created[0].completed.emit(RESULTS_OK)
        assert rec.events == before
        assert rec.of("completed") == []

    def test_cancel_before_start_suppresses_all_signals(self):
        job = make_job(TWO_ITEMS)
        rec = Recorder(job)
        job.cancel()
        job.start()
        assert rec.of("item_started") == []
        assert rec.of("completed") == []


# --------------------------------------------------------------------------- #
# Long-form (naddr) resolution                                                #
# --------------------------------------------------------------------------- #

NADDR = encode_naddr("post-1", "cd" * 32, 30023, ["wss://hint.example"])

THIN_HTML = "<p>teaser</p>"
THICK_HTML = "<p>" + ("long body text " * 30) + "</p>"
LONG_PROSE = "# Full prose from Nostr\n\n" + ("relay paragraph text " * 40)


class TestLongFormResolution:
    def test_nostr_uri_resolves_and_adopts_longer_relay_body(self):
        factory, created = make_factory()
        long_form = FakeLongFormFetcher({"content": LONG_PROSE})
        item = make_item("Nostr post", guid="ng1", link=f"nostr:{NADDR}",
                         content_html=THICK_HTML)
        job = make_job([item], factory=factory, long_form=long_form)
        rec = Recorder(job)
        job.start()
        assert len(long_form.calls) == 1
        coord, extra = long_form.calls[0]
        assert coord.d_tag == "post-1"
        assert extra == ("wss://read.example",)
        assert rec.of("item_resolving") == [("item_resolving", 0, "Nostr post")]
        assert created[0].inner_event["content"].startswith(
            "# Full prose from Nostr")

    def test_http_link_with_naddr_resolves_even_when_thick(self):
        # A coordinate is an explicit publisher pointer: resolve
        # regardless of how long the teaser reads; length decides
        # adoption, not resolution.
        factory, created = make_factory()
        long_form = FakeLongFormFetcher({"content": LONG_PROSE})
        thick = make_item("Thick", guid="t2",
                          link=f"https://njump.me/{NADDR}",
                          content_html=THICK_HTML)
        job = make_job([thick], factory=factory, long_form=long_form)
        job.start()
        assert len(long_form.calls) == 1
        assert created[0].inner_event["content"].startswith(
            "# Full prose from Nostr")

    def test_relay_body_shorter_than_feed_keeps_feed_body(self):
        factory, created = make_factory()
        long_form = FakeLongFormFetcher({"content": "# stub"})
        thick = make_item("Thick", guid="t2",
                          link=f"https://njump.me/{NADDR}",
                          content_html=THICK_HTML)
        job = make_job([thick], factory=factory, long_form=long_form)
        job.start()
        assert len(long_form.calls) == 1
        assert created[0].inner_event["content"].startswith("long body text")

    def test_bare_coordinate_in_guid_resolves(self):
        factory, created = make_factory()
        long_form = FakeLongFormFetcher({"content": LONG_PROSE})
        item = make_item("Native CMS post", guid=f"30023:{'cd' * 32}:my-post",
                         link="https://blog.example/native-post",
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, long_form=long_form)
        job.start()
        coord, _extra = long_form.calls[0]
        assert coord.d_tag == "my-post"
        assert coord.relay_hints == ()
        assert created[0].inner_event["content"].startswith(
            "# Full prose from Nostr")

    def test_not_found_falls_back_to_feed_body(self):
        factory, created = make_factory()
        item = make_item("Nostr post", guid="ng1", link=f"nostr:{NADDR}",
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory,
                       long_form=FakeLongFormFetcher(None))
        rec = Recorder(job)
        job.start()
        assert created[0].inner_event["content"].startswith("teaser")
        assert rec.of("completed") == [("completed", 1, 1)]

    def test_empty_event_content_falls_back(self):
        factory, created = make_factory()
        item = make_item("Nostr post", guid="ng1", link=f"nostr:{NADDR}",
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory,
                       long_form=FakeLongFormFetcher({"content": "   "}))
        job.start()
        assert created[0].inner_event["content"].startswith("teaser")

    def test_content_markdown_is_authoritative_and_skips_recovery(self):
        # A Markdown-emitting source (Nostr, MDX): the body IS canonical,
        # so neither the naddr resolver nor full-text may run even though
        # the link carries a coordinate and the body is thin.
        factory, created = make_factory()
        long_form = FakeLongFormFetcher({"content": LONG_PROSE})
        pages = FakeFetcher()
        item = make_item(
            "Native", guid="ng1", link=f"https://njump.me/{NADDR}",
            content_html="", content_markdown="# Verbatim body")
        job = make_job([item], factory=factory, long_form=long_form,
                       page_fetcher=pages)
        job.start()
        assert long_form.calls == []
        assert pages.calls == []
        assert created[0].inner_event["content"].startswith("# Verbatim body")


# --------------------------------------------------------------------------- #
# Full-text recovery                                                          #
# --------------------------------------------------------------------------- #

ARTICLE_URL = "https://blog.example/post"

ARTICLE_PAGE = (
    "<html><head><title>The Real Article Title</title></head><body><article>"
    + "".join(
        f"<p>Paragraph {i} with plenty of ordinary prose words to satisfy "
        f"readability scoring and beat the summary comfortably.</p>"
        for i in range(20)
    )
    + "</article><nav>menu chrome</nav></body></html>"
)


class TestFullTextRecovery:
    def test_thin_item_adopts_extracted_article(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("ok", ARTICLE_PAGE)})
        item = make_item("Teaser", guid="t1", link=ARTICLE_URL,
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages)
        rec = Recorder(job)
        job.start()
        assert pages.calls == [ARTICLE_URL]
        assert rec.of("item_extracting") == [("item_extracting", 0, "Teaser")]
        content = created[0].inner_event["content"]
        assert "Paragraph 3" in content
        assert "Originally published at" in content  # footer preserved
        assert "menu chrome" not in content          # chrome stripped

    def test_slug_title_upgraded_from_page(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("ok", ARTICLE_PAGE)})
        item = make_item("post-slug", guid="t1", link=ARTICLE_URL,
                         content_html="", title_from_url=True)
        job = make_job([item], factory=factory, page_fetcher=pages)
        job.start()
        tags = created[0].inner_event["tags"]
        assert ["title", "The Real Article Title"] in tags

    def test_real_title_never_overwritten(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("ok", ARTICLE_PAGE)})
        item = make_item("Author Chosen Title", guid="t1", link=ARTICLE_URL,
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages)
        job.start()
        tags = created[0].inner_event["tags"]
        assert ["title", "Author Chosen Title"] in tags

    def test_toggle_off_skips_recovery(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("ok", ARTICLE_PAGE)})
        item = make_item("Teaser", guid="t1", link=ARTICLE_URL,
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages,
                       fetch_full_text=False)
        job.start()
        assert pages.calls == []
        assert created[0].inner_event["content"].startswith("teaser")

    def test_thick_body_skips_recovery(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("ok", ARTICLE_PAGE)})
        item = make_item("Thick", guid="t1", link=ARTICLE_URL,
                         content_html=THICK_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages)
        job.start()
        assert pages.calls == []

    def test_page_fetch_failure_keeps_feed_body(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("err", "HTTP 503")})
        item = make_item("Teaser", guid="t1", link=ARTICLE_URL,
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages)
        rec = Recorder(job)
        job.start()
        assert created[0].inner_event["content"].startswith("teaser")
        assert rec.of("completed") == [("completed", 1, 1)]

    def test_worthless_extraction_keeps_feed_body(self):
        factory, created = make_factory()
        pages = FakeFetcher({ARTICLE_URL: ("ok", "<html><body><p>hi</p></body></html>")})
        item = make_item("Teaser", guid="t1", link=ARTICLE_URL,
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages)
        job.start()
        assert created[0].inner_event["content"].startswith("teaser")

    def test_naddr_miss_falls_through_to_full_text(self):
        # Chain: coordinate present but relays return nothing, the link
        # is an ordinary page, so full-text recovery still runs.
        factory, created = make_factory()
        pages = FakeFetcher({
            f"https://njump.me/{NADDR}": ("ok", ARTICLE_PAGE),
        })
        item = make_item("Teaser", guid="t1",
                         link=f"https://njump.me/{NADDR}",
                         content_html=THIN_HTML)
        job = make_job([item], factory=factory, page_fetcher=pages,
                       long_form=FakeLongFormFetcher(None))
        job.start()
        assert "Paragraph 3" in created[0].inner_event["content"]


# --------------------------------------------------------------------------- #
# Image rehosting                                                             #
# --------------------------------------------------------------------------- #

IMAGE_HTML = (
    "<p>A post with enough body text to comfortably clear the thin "
    "threshold so no recovery pass interferes with this test.</p>"
    '<img src="https://a.example/banner.png">'
    '<img src="https://a.example/photo.jpg">'
)


def fake_image_mirror(results=None):
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


class TestImageRehosting:
    def test_images_mirrored_and_rewritten_in_inner_event(self):
        factory, created = make_factory()
        mirror, calls = fake_image_mirror()
        item = make_item("Pictures", guid="p1", content_html=IMAGE_HTML)
        job = make_job([item], factory=factory, image_mirror=mirror)
        rec = Recorder(job)
        job.start()
        assert len(calls) == 2
        content = created[0].inner_event["content"]
        assert "https://blossom.example/1" in content
        assert "https://a.example/banner.png" not in content
        # Progress reached the terminal 2-of-2 state.
        assert ("item_mirroring", 0, 2, 0, 2) in rec.events

    def test_mirror_failure_keeps_original_and_publishes(self):
        factory, created = make_factory()
        mirror, _ = fake_image_mirror({
            "https://a.example/banner.png": ("err", "no thanks"),
        })
        item = make_item("Pictures", guid="p1", content_html=IMAGE_HTML)
        job = make_job([item], factory=factory, image_mirror=mirror)
        rec = Recorder(job)
        job.start()
        content = created[0].inner_event["content"]
        assert "https://a.example/banner.png" in content
        # The second image (mirror call #2) still succeeded.
        assert "https://blossom.example/2" in content
        assert rec.of("completed") == [("completed", 1, 1)]

    def test_rehost_disabled_skips_mirroring(self):
        factory, created = make_factory()
        mirror, calls = fake_image_mirror()
        item = make_item("Pictures", guid="p1", content_html=IMAGE_HTML)
        job = make_job([item], factory=factory, image_mirror=mirror,
                       rehost_images=False)
        job.start()
        assert calls == []
        assert "https://a.example/banner.png" in created[0].inner_event["content"]

    def test_skip_set_honoured(self):
        factory, created = make_factory()
        mirror, calls = fake_image_mirror()
        item = make_item("Pictures", guid="p1", content_html=IMAGE_HTML)
        job = make_job([item], factory=factory, image_mirror=mirror,
                       skip_image_urls={"https://a.example/banner.png"})
        job.start()
        assert calls == ["https://a.example/photo.jpg"]
        content = created[0].inner_event["content"]
        assert "https://a.example/banner.png" in content


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #

class TestRelayAcceptance:
    def test_counts_accepted_rows(self):
        assert pipeline_mod._relay_acceptance(RESULTS_OK) == (1, 2)

    def test_non_list_payload_is_zero(self):
        assert pipeline_mod._relay_acceptance(None) == (0, 0)
        assert pipeline_mod._relay_acceptance("nope") == (0, 0)

    def test_malformed_rows_count_toward_total_only(self):
        rows = [("wss://a", True, ""), None, ("short",)]
        assert pipeline_mod._relay_acceptance(rows) == (1, 3)
