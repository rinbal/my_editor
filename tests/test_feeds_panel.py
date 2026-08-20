# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the preview-first ``FeedsPanel``.

Drives the whole panel state machine (idle -> loading -> preview ->
importing -> done) with a synchronous fake fetcher and a real
``ImportItemsJob`` running on injected fakes, so the test covers the
actual wiring between panel, registry, and pipeline without network,
relays, signer, threads, or timers.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from nostr.imports.pipeline import ImportItemsJob
from nostr.ui.feeds_panel import FeedsPanel

from tests.imports_fakes import (
    PROFILE,
    TWO_ITEM_FEED,
    FakeFetcher,
    FakeLongFormFetcher,
    FakeRelayListCache,
    ManualFetcher,
    RecordingPacer,
    inline_run_blocking,
    make_factory,
    rss_feed,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def make_panel(fetcher, *, publish_outcomes=None):
    """A fully wired panel whose import jobs run on fakes.

    Returns (panel, created_publish_jobs, job_kwargs_seen): the last
    lets tests assert what the panel actually handed the pipeline.
    """
    import tempfile
    from nostr.imports.subscriptions import FeedSubscriptionStore
    from tests.test_imports_subscriptions import (
        FakePublisher,
        FakeScheduler,
        FakeSessionPool,
    )

    publish_factory, created_jobs = make_factory(publish_outcomes)
    job_kwargs_seen = []
    cache_dir = tempfile.mkdtemp(prefix="feeds-panel-test-")

    def subscription_store_factory(**kwargs):
        kwargs.update(
            session_pool=FakeSessionPool(),
            relay_pool=None,
            relay_list_cache=FakeRelayListCache(),
            cache_dir=cache_dir,
            publisher=FakePublisher(),
            scheduler=FakeScheduler(),
            clock=lambda: 1_700_000_000,
        )
        return FeedSubscriptionStore(**kwargs)

    def import_job_factory(**kwargs):
        job_kwargs_seen.append(dict(kwargs))
        kwargs.update(
            relay_list_cache=FakeRelayListCache(),
            # Page fetches (full-text recovery) also settle synchronously
            # against the same fake response table.
            fetcher=fetcher,
            long_form_fetcher=FakeLongFormFetcher(None),
            # No real Blossom in tests: mirroring settles synchronously.
            image_mirror=lambda url, ok, err: ok(
                f"https://blossom.test/{len(url)}"),
            publish_job_factory=publish_factory,
            run_blocking=inline_run_blocking,
            pacer=RecordingPacer(),
        )
        return ImportItemsJob(**kwargs)

    panel = FeedsPanel(
        is_dark=True,
        fetcher=fetcher,
        import_job_factory=import_job_factory,
        run_blocking=inline_run_blocking,
        subscription_store_factory=subscription_store_factory,
    )
    panel.bind_runtime(
        relay_pool=object(),
        relay_list_cache=FakeRelayListCache(),
        session_pool=object(),
        blossom_settings=type("S", (), {"primary": "https://blossom.test"})(),
    )
    panel.set_active_profile(PROFILE)
    return panel, created_jobs, job_kwargs_seen


class TestUrlGate:
    def test_bare_word_disables_load_with_hint(self):
        panel, _jobs, _kw = make_panel(FakeFetcher())
        panel._url_edit.setText("feed")
        assert not panel._load_btn.isEnabled()
        assert "doesn't look like a fetchable URL" in panel._status_label.text()

    def test_valid_url_enables_load(self):
        panel, _jobs, _kw = make_panel(FakeFetcher())
        panel._url_edit.setText("https://example.com/feed")
        assert panel._load_btn.isEnabled()

    def test_no_profile_disables_load(self):
        panel, _jobs, _kw = make_panel(FakeFetcher())
        panel.set_active_profile(None)
        panel._url_edit.setText("https://example.com/feed")
        assert not panel._load_btn.isEnabled()
        assert "Connect a Nostr profile" in panel._status_label.text()


class TestPreviewFlow:
    def test_load_builds_checked_preview(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()

        assert panel._state == "preview"
        assert panel._list.count() == 2
        assert all(
            panel._list.item(i).checkState() == Qt.Checked for i in range(2))
        assert panel._import_btn.text() == "Import 2 selected"
        assert panel._import_btn.isVisible() or panel._state == "preview"

    def test_unchecking_updates_import_button(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._list.item(0).setCheckState(Qt.Unchecked)
        assert panel._import_btn.text() == "Import 1 selected"
        panel._list.item(1).setCheckState(Qt.Unchecked)
        assert not panel._import_btn.isEnabled()

    def test_scope_chip_refilters_without_refetch(self):
        big_feed = rss_feed([
            {"title": f"Post {i}", "guid": f"g{i}"} for i in range(30)
        ])
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", big_feed)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._list.count() == 25  # default scope for a big feed
        fetches_before = len(fetcher.calls)
        panel._on_scope_selected("newest10")
        assert panel._list.count() == 10
        assert len(fetcher.calls) == fetches_before

    def test_small_feed_defaults_to_small_scope(self):
        small_feed = rss_feed([
            {"title": f"Post {i}", "guid": f"g{i}"} for i in range(3)
        ])
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", small_feed)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._selected_scope_key() == "newest10"
        assert panel._list.count() == 3

    def test_empty_feed_returns_to_idle_with_message(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", rss_feed([]))})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._state == "idle"
        assert "no items to import" in panel._status_label.text()

    def test_failed_load_shows_friendly_error(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("err", "Host not found.")})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._state == "idle"
        assert "Couldn't reach that URL" in panel._status_label.text()
        # Failures are visually distinguished from ordinary status.
        assert panel._status_label.property("error") == "true"

    def test_cancel_during_load_discards_late_result(self):
        fetcher = ManualFetcher()
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._state == "loading"
        panel._on_cancel_clicked()
        assert panel._state == "idle"
        assert "Load cancelled" in panel._status_label.text()
        # The fetch settles late; the stale generation must drop it.
        _url, on_success, _on_failure = fetcher.pending[0]
        on_success(TWO_ITEM_FEED)
        assert panel._state == "idle"
        assert panel._list.count() == 0


class TestImportFlow:
    def test_import_selected_items_end_to_end(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, created_jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._list.item(0).setCheckState(Qt.Unchecked)
        panel._on_import_clicked()

        assert panel._state == "done"
        assert len(created_jobs) == 1  # only the selected item published
        assert "Done. 1/1 item(s) imported as drafts." in panel._status_label.text()
        # The remaining row shows the published state with relay counts.
        assert "published: 1/2 relays" in panel._list.item(0).text()

    def test_import_failure_row_and_summary(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(
            fetcher, publish_outcomes=[("fail", "signer said no")])
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._on_import_clicked()
        assert "failed: signer said no" in panel._list.item(0).text()
        assert "Done. 1/2 item(s) imported as drafts." in panel._status_label.text()

    def test_cancel_during_import(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, created_jobs, _kw = make_panel(
            fetcher, publish_outcomes=[("pending", None)])
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._on_import_clicked()
        assert panel._state == "importing"
        panel._on_cancel_clicked()
        assert panel._state == "done"
        assert created_jobs[0].cancelled is True
        assert "Import cancelled" in panel._status_label.text()

    def test_import_wxr_file_opens_preview(self, tmp_path=None):
        import tempfile
        from tests.test_imports_files import WXR
        panel, _jobs, kwargs_seen = make_panel(FakeFetcher())
        with tempfile.NamedTemporaryFile(
                "w", suffix=".xml", delete=False) as handle:
            handle.write(WXR)
            path = handle.name
        try:
            panel._import_file(path)
            assert panel._state == "preview"
            assert panel._list.count() == 2
            panel._on_import_clicked()
            assert panel._state == "done"
            # The source tag carries the file name, not a URL.
            assert kwargs_seen[0]["feed_url"] == os.path.basename(path)
        finally:
            os.unlink(path)

    def test_import_substack_zip_opens_preview(self):
        import tempfile
        from tests.test_imports_files import substack_zip
        panel, _jobs, _kw = make_panel(FakeFetcher())
        with tempfile.NamedTemporaryFile(
                "wb", suffix=".zip", delete=False) as handle:
            handle.write(substack_zip())
            path = handle.name
        try:
            panel._import_file(path)
            assert panel._state == "preview"
            assert panel._list.count() == 1
        finally:
            os.unlink(path)

    def test_import_opml_bulk_subscribes(self):
        import tempfile
        from tests.test_imports_files import OPML
        panel, _jobs, _kw = make_panel(FakeFetcher())
        with tempfile.NamedTemporaryFile(
                "w", suffix=".opml", delete=False) as handle:
            handle.write(OPML)
            path = handle.name
        try:
            panel._import_file(path)
            assert panel._state == "idle"
            assert "Subscribed to 2 feed(s)" in panel._status_label.text()
            assert panel._subscriptions.has_feed("https://a.example/feed")
            assert panel._sources_list.count() == 2
            # Re-importing the same list adds nothing and says so.
            panel._import_file(path)
            assert "already subscribed" in panel._status_label.text()
        finally:
            os.unlink(path)

    def test_import_garbage_zip_fails_cleanly(self):
        import tempfile
        panel, _jobs, _kw = make_panel(FakeFetcher())
        with tempfile.NamedTemporaryFile(
                "wb", suffix=".zip", delete=False) as handle:
            handle.write(b"PK\x03\x04 utterly broken")
            path = handle.name
        try:
            panel._import_file(path)
            assert panel._state == "idle"
            assert "archive" in panel._status_label.text().lower()
        finally:
            os.unlink(path)

    def test_profile_switch_aborts_and_resets(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._state == "preview"
        panel.set_active_profile(None)
        assert panel._state == "idle"
        assert panel._list.count() == 0


class TestNativeBehaviours:
    """Platform-convention behaviours: progress, Escape, guidance."""

    def test_idle_empty_state_offers_guidance(self):
        panel, _jobs, _kw = make_panel(FakeFetcher())
        assert "Paste a link" in panel._status_label.text()

    def test_loading_shows_indeterminate_progress(self):
        fetcher = ManualFetcher()
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._state == "loading"
        # Range (0, 0) is Qt's indeterminate (busy) mode.
        assert panel._progress_bar.minimum() == 0
        assert panel._progress_bar.maximum() == 0

    def test_import_progress_is_determinate_and_complete(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._on_import_clicked()
        assert panel._progress_bar.maximum() == 2
        assert panel._progress_bar.value() == 2

    def test_escape_cancels_a_load(self):
        fetcher = ManualFetcher()
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._on_escape()
        assert panel._state == "idle"
        assert "Load cancelled" in panel._status_label.text()

    def test_escape_outside_busy_states_is_inert(self):
        panel, _jobs, _kw = make_panel(FakeFetcher())
        panel._on_escape()
        assert panel._state == "idle"

    def test_file_button_carries_ellipsis(self):
        # Buttons that open a dialog for further input end with "…".
        panel, _jobs, _kw = make_panel(FakeFetcher())
        assert panel._file_btn.text().endswith("…")


class TestSubscriptions:
    def test_subscribe_from_preview_populates_sources(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        assert panel._state == "preview"
        panel._on_subscribe_clicked()
        assert panel._subscriptions.has_feed("https://example.com/feed")
        assert panel._subscriptions.get(
            "https://example.com/feed").title == "My Blog"
        assert panel._sources_list.count() == 1

    def test_import_of_subscribed_source_stamps_last_fetched(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._on_subscribe_clicked()
        panel._on_import_clicked()
        assert panel._state == "done"
        feed = panel._subscriptions.get("https://example.com/feed")
        assert feed.last_fetched_at == 1_700_000_000

    def test_since_visit_chip_hidden_without_history(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        chip = panel._scope_buttons["sinceVisit"]
        assert chip.isHidden()

    def test_since_visit_scope_filters_by_last_import(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._url_edit.setText("https://example.com/feed")
        panel._on_load_clicked()
        panel._on_subscribe_clicked()
        # Cut between the two items (2024-01-01 and 2024-01-02).
        panel._subscriptions.mark_fetched(
            "https://example.com/feed", when=1704100000)
        panel._on_load_clicked()
        assert not panel._scope_buttons["sinceVisit"].isHidden()
        panel._on_scope_selected("sinceVisit")
        assert panel._list.count() == 1

    def test_source_row_activation_loads_preview(self):
        fetcher = FakeFetcher({"https://example.com/feed": ("ok", TWO_ITEM_FEED)})
        panel, _jobs, _kw = make_panel(fetcher)
        panel._subscriptions.add_feed("https://example.com/feed", "My Blog")
        row = panel._sources_list.item(0)
        panel._on_source_activated(row)
        assert panel._state == "preview"
        assert panel._url_edit.text() == "https://example.com/feed"

    def test_remove_button_unsubscribes_selected_source(self):
        panel, _jobs, _kw = make_panel(FakeFetcher())
        panel._subscriptions.add_feed("https://example.com/feed", "My Blog")
        assert not panel._remove_source_btn.isEnabled()
        panel._sources_list.setCurrentRow(0)
        assert panel._remove_source_btn.isEnabled()
        panel._on_remove_source()
        assert not panel._subscriptions.has_feed("https://example.com/feed")
        assert panel._sources_list.count() == 0
