# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Imports window: sources, their posts, and the open one.

The window is built on a real controller and a real inbox (in a
temporary folder); the network, the signer and the checks are faked.
It shows what the controller holds and remembers how it was left.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from nostr.imports.feed_list import source_key
from nostr.imports.inbox_store import INBOX, OLDER_POSTS, SKIPPED_POSTS
from nostr.imports.subscriptions import FeedSubscriptionStore
from nostr.imports_controller import ImportsController
from nostr.ui.imports_sidebar import FILE, HEADER, LIST, SOURCE
from nostr.ui.imports_window import ImportsWindow, freshness
from tests.accessibility import unnamed_controls
from tests.imports_fakes import FakeCatalogue, FakeFetcher, TWO_ITEM_FEED, inline_run_blocking
from tests.imports_fakes import make_item
from tests.outbox_fakes import FakeRelayDirectory, settle
from tests.test_imports_subscriptions import FakeRelay, FakeScheduler, FakeSessionPool
from tests.widget_lifetime import delete_new_windows


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every window and panel a test makes is deleted after it: left to
    the cycle collector, one without a parent can crash it."""
    yield from delete_new_windows()


PK = "ab" * 32
# The inbox dates posts by the real clock, so the posts here are too.
NOW = int(time.time())
JOURNAL = "https://journal.example/feed"
FIELD = "https://field.example/rss.xml"
AUTHOR = "https://example.com/feed"      # read when opened in these tests


class FakeChecker(QObject):
    checking = Signal(str, bool)
    source_checked = Signal(str, int)

    def start(self):
        pass

    def stop(self):
        pass

    def check_now(self, key):
        return False

    def is_checking(self, key):
        return False


class Images(QObject):
    ready = Signal(str)

    def image(self, url):
        return None

    def request(self, url, size=None, *, urgent=False):
        pass


def controller_for(tmp_path, *, manual=(), relay=None, scheduler=None):
    relay = relay if relay is not None else FakeRelay()
    store = FeedSubscriptionStore(
        session_pool=FakeSessionPool(), relay_pool=None,
        relay_directory=FakeRelayDirectory(), cache_dir=tmp_path / "cache",
        query=relay, publisher=relay, scheduler=scheduler or FakeScheduler(),
        clock=lambda: NOW)
    fetcher = FakeFetcher({AUTHOR: ("ok", TWO_ITEM_FEED)})
    controller = ImportsController(
        relay_pool=None, relay_directory=FakeRelayDirectory(), session_pool=None,
        config_dir=tmp_path, subscription_store=store, fetcher=fetcher, images=Images(),
        run_blocking=inline_run_blocking, checker_factory=lambda inbox: FakeChecker(),
        catalogue_factory=lambda inbox: FakeCatalogue())
    controller.account_changed(SimpleNamespace(user_pubkey=PK, bunker_relays=[],
                                               display_name="Ada"))
    settle()
    return controller


def fill(controller):
    """Two automatic sources with posts, one failing, one read when opened."""
    subscriptions, inbox = controller.subscriptions, controller.inbox
    subscriptions.add_feed(FIELD, "field notes")
    subscriptions.add_feed(JOURNAL, "A thoughtful journal")
    inbox._db.execute("UPDATE sources SET created_at = ?", (NOW - 30 * 86400,))
    inbox._db.commit()
    for url, titles in ((JOURNAL, ("Self-custody", "Small blogs")), (FIELD, ("Lisbon",))):
        inbox.ingest(source_key(url), [make_item("Archive", guid=f"{url}-old",
                                                 published_at=NOW - 60 * 86400)])
        inbox._db.execute("UPDATE sources SET last_checked = 1")
        inbox.ingest(source_key(url), [make_item(t, guid=f"{url}-{t}",
                                                 published_at=NOW - 3600 * (i + 1))
                                       for i, t in enumerate(titles)])
    inbox.record_failure(source_key(FIELD), "Couldn't reach that URL: Host not found.")
    controller._announce()


@pytest.fixture
def window(tmp_path):
    controller = controller_for(tmp_path)
    fill(controller)
    saved = {}
    win = ImportsWindow(controller, load_settings=lambda: {},
                        save_settings=lambda value: saved.update(value))
    win.saved = saved
    win.show()
    settle()
    yield win
    win.close()
    controller.account_changed(None)
    settle()


def entries(win):
    return win.sidebar.model_.entries()


def source_menu(win, entry):
    """The toolbar's menu for the source, as it is filled when it opens."""
    win.show_view(entry.kind, entry.key)
    win._fill_source_menu()
    return win.source_menu_button.menu()


def command(menu, text):
    return next(a for a in menu.actions() if a.text() == text)


def titles(win):
    return [win.posts.model_.post(r).title for r in range(win.posts.model_.rowCount())]


class TestSidebar:
    def test_the_lists_then_the_sources_sorted_by_title(self, window):
        rows = [(e.kind, e.title) for e in entries(window)]
        assert rows[:4] == [(LIST, "Inbox"), (LIST, "Older Posts"), (LIST, "Imported"),
                            (LIST, "Skipped")]
        assert rows[4:] == [(HEADER, "Sources"), (SOURCE, "A thoughtful journal"),
                            (SOURCE, "field notes")]

    def test_counts(self, window):
        counts = {e.key: e.count for e in entries(window) if e.kind == LIST}
        assert counts == {INBOX: 3, OLDER_POSTS: 2, "imported": 0, SKIPPED_POSTS: 0}
        inbox = entries(window)[0]
        assert inbox.emphasized

    def test_a_failed_source_says_why_and_when_it_is_tried_again(self, window):
        field = next(e for e in entries(window) if e.title == "field notes")
        assert field.failed
        assert "Host not found" in field.tooltip
        assert "Next try in" in field.tooltip

    def test_no_sources_no_header(self, tmp_path):
        controller = controller_for(tmp_path)
        win = ImportsWindow(controller)
        assert [e.kind for e in entries(win)] == [LIST] * 4
        assert win.placeholder_title.text() == "No sources yet"
        controller.account_changed(None)

    def test_header_rows_cannot_be_chosen_and_the_keys_pass_over_them(self, window):
        window.sidebar.setFocus()
        window.sidebar.select(LIST, SKIPPED_POSTS)
        QTest.keyClick(window.sidebar, Qt.Key.Key_Down)
        assert window.sidebar.chosen().kind == SOURCE


class TestLists:
    def test_the_inbox_first_and_its_first_post_open(self, window):
        assert window.list_title.text() == "Inbox"
        assert window.list_subtitle.text() == "3 new posts"
        assert titles(window) == ["Self-custody", "Lisbon", "Small blogs"]
        assert window.article.post is not None

    def test_a_source_shows_its_new_or_older_posts(self, window):
        window.sidebar.select(SOURCE, source_key(JOURNAL))
        assert window.segments.isVisibleTo(window)
        assert sorted(titles(window)) == ["Self-custody", "Small blogs"]
        window.segment_older.click()
        assert titles(window) == ["Archive"]
        assert window.source_menu_button.isVisibleTo(window)

    def test_a_source_read_when_opened(self, tmp_path):
        controller = controller_for(tmp_path)
        controller.subscriptions.add_feed("npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237"
                                          "lmu63q0uf63m", "An author")
        win = ImportsWindow(controller)
        key = source_key("npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237lmu63q0uf63m")
        assert win.sidebar.select(SOURCE, key)
        assert not win.segments.isVisibleTo(win)
        # The fake has nothing at that address: the error and Try Again.
        assert win.placeholder_title.text() == "Couldn't read this source"
        assert win.placeholder_button.text() == "Try Again"
        assert win.list_subtitle.text() == "MyEditor reads this source when you open it."
        controller.account_changed(None)

    def test_skipped_says_it_is_this_computer(self, window):
        window.sidebar.select(LIST, SKIPPED_POSTS)
        assert window.list_subtitle.text() == "Posts you skipped on this computer."
        assert window.placeholder_title.text() == "Nothing skipped"

    def test_up_to_date(self, window):
        for row in range(window.posts.model_.rowCount()):
            post = window.posts.model_.post(row)
            window._controller.inbox.skip(post.source_key, post.d_tag, post.revision)
        window._controller._announce()
        assert window.placeholder_title.text() == "You're up to date"


class TestSearch:
    def test_search_filters_and_clears_with_another_list(self, window):
        window.search.setText("lisbon")
        window._search_timer.timeout.emit()
        assert titles(window) == ["Lisbon"]
        window.search.setText("nothing like it")
        window._search_timer.timeout.emit()
        assert window.placeholder_title.text() == "No Results"
        window.sidebar.select(LIST, OLDER_POSTS)
        assert window.search.text() == ""
        assert len(titles(window)) == 2

    def test_find_focuses_the_field_and_escape_goes_back(self, window):
        find = QKeySequence(QKeySequence.StandardKey.Find)
        action = next(a for a in window.actions() if a.shortcut() == find)
        action.trigger()
        assert window.focusWidget() is window.search
        window.search.setText("x")
        QTest.keyClick(window.search, Qt.Key.Key_Escape)
        assert window.search.text() == ""
        assert window.focusWidget() is window.posts


class TestSelection:
    def test_the_band_counts_what_is_selected(self, window):
        model = window.posts.model_
        model.set_checked(0, True)
        assert window.select_all.text() == "1 of 3 selected"
        assert window.select_all.checkState() == Qt.CheckState.PartiallyChecked
        window.select_all.click()
        assert model.checked() == []
        window.select_all.click()
        assert len(model.checked()) == 3
        assert window.select_all.text() == "3 of 3 selected"


class TestLayout:
    def test_a_narrow_window_folds_the_sidebar_away_and_back(self, window):
        window.resize(1200, 700)
        settle()
        assert window.sidebar.isVisible()
        window.resize(800, 700)
        settle()
        assert not window.sidebar.isVisible()
        assert window.act_sidebar.text() == "Show Sidebar"
        window.resize(1200, 700)
        settle()
        assert window.sidebar.isVisible()

    def test_show_sidebar_works_in_a_narrow_window(self, window):
        """Review H3: below 852 px the window grew for the sidebar and
        folded it away again at once, so Show Sidebar never worked."""
        window.resize(1200, 700)
        settle()
        window.resize(700, 700)
        settle()
        assert not window.sidebar.isVisible()
        window.act_sidebar.trigger()
        settle()
        window.resize(window.width(), 700)
        settle()
        assert window.sidebar.isVisible()
        assert window.act_sidebar.isChecked()
        assert window.act_sidebar.text() == "Hide Sidebar"
        # Making the window narrower does not take it away either.
        window.resize(860, 700)
        settle()
        assert window.sidebar.isVisible()

    def test_hiding_the_sidebar_is_remembered(self, window):
        window.act_sidebar.trigger()
        assert not window.sidebar.isVisible()
        assert window.saved["sidebar"] is False
        window.act_sidebar.trigger()
        assert window.saved["sidebar"] is True

    def test_the_list_shown_is_remembered_and_restored(self, window, tmp_path):
        window.sidebar.select(SOURCE, source_key(JOURNAL))
        assert window.saved["selection"] == [SOURCE, source_key(JOURNAL)]
        again = ImportsWindow(window._controller, load_settings=lambda: dict(window.saved))
        assert again.sidebar.chosen().key == source_key(JOURNAL)
        assert again.list_title.text() == "A thoughtful journal"
        again.close()

    def test_every_pane_keeps_a_minimum_width(self, window):
        assert not window.splitter.childrenCollapsible()
        assert window.sidebar.minimumWidth() >= 170
        assert window.article.minimumWidth() >= 360


def test_freshness_words():
    source = SimpleNamespace(automatic=True, paused=False, error="", last_checked=NOW - 120,
                             next_check=0)
    assert freshness(source, now=NOW) == "Checked 2 min ago"
    source.last_checked = NOW - 10
    assert freshness(source, now=NOW) == "Checked just now"
    source.last_checked = NOW - 30 * 3600
    assert freshness(source, now=NOW) == "Checked yesterday"     # inside a sentence
    source.last_checked = 0
    assert freshness(source, now=NOW) == "Not checked yet."
    failed = SimpleNamespace(automatic=True, paused=False, error="Boom.", last_checked=0,
                             next_check=NOW + 7200)
    assert freshness(failed, now=NOW) == "Boom. Next try in 2 hours."
    manual = SimpleNamespace(automatic=False, paused=False, error="", last_checked=0,
                             next_check=0)
    assert freshness(manual, now=NOW) == "MyEditor reads this source when you open it."
    # Automatic checks turned off in Source Settings: said so, an old
    # error no longer is.
    off = SimpleNamespace(automatic=True, paused=True, error="Boom.", last_checked=NOW - 120,
                          next_check=0)
    assert freshness(off, now=NOW) == "Not checked automatically \u00b7 Checked 2 min ago"
    off.last_checked = 0
    assert freshness(off, now=NOW) == "Not checked automatically"


class TestAdding:
    """Add: Follow a Website, Import a File, Import a Link."""

    def test_the_add_menu(self, window):
        actions = window.add_button.menu().actions()[:3]
        assert [a.text() for a in actions] == ["Follow a Website…", "Import a File…",
                                               "Import a Link…"]
        assert all(a.isEnabled() for a in actions)
        assert window.add_button.accessibleName() == "Add"

    def test_a_window_that_only_shows_cannot_add(self, tmp_path):
        controller = controller_for(tmp_path)
        controller.read_only = True
        win = ImportsWindow(controller)
        assert not any(a.isEnabled() for a in (win.act_follow, win.act_import_file,
                                               win.act_import_link))
        win.follow_website()
        assert win.sheet() is None
        assert win.placeholder_button is None
        controller.account_changed(None)

    def test_follow_a_website_shows_the_source_it_followed(self, window):
        window.follow_website()
        sheet = window.sheet()
        assert type(sheet).__name__ == "FollowSheet"
        assert sheet.windowModality() == Qt.WindowModality.WindowModal
        sheet.address.setText(AUTHOR)
        sheet.look_up()
        settle()
        sheet.buttons["follow"].click()
        settle()
        assert window.sidebar.chosen() == next(e for e in entries(window)
                                               if e.key == source_key(AUTHOR))
        assert window.sheet() is None
        # Review N4: on Older, where the posts the card promised are (a
        # first check is a baseline), not on an empty New.
        assert window.segment_older.isChecked()
        assert sorted(titles(window)) == ["First", "Second"]

    def test_an_empty_inbox_offers_to_follow(self, tmp_path):
        controller = controller_for(tmp_path)
        win = ImportsWindow(controller)
        win.show()
        assert win.placeholder_button.text() == "Follow a Website…"
        win.placeholder_button.click()
        assert type(win.sheet()).__name__ == "FollowSheet"
        win.sheet().reject()
        win.close()
        controller.account_changed(None)

    def test_a_file_opens_with_all_its_posts_checked(self, window, tmp_path):
        from tests.test_imports_files import WXR
        path = tmp_path / "blog.xml"
        path.write_text(WXR, encoding="utf-8")
        window.import_file(str(path))
        settle()
        rows = [(e.kind, e.title) for e in entries(window)]
        assert rows[-2:] == [(HEADER, "Files and Links"), (FILE, "blog.xml")]
        assert window.sidebar.chosen().kind == FILE
        model = window.posts.model_
        assert model.rowCount() == 2
        assert len(model.checked()) == 2
        assert window.list_subtitle.text() == "Imported once, not kept as a source."
        # Removing it from the list goes back to the Inbox.
        window._controller.close_collection(window.sidebar.chosen().key)
        settle()
        assert window.sidebar.chosen().key == INBOX
        assert all(e.kind != FILE for e in entries(window))

    def test_a_file_dropped_on_the_window(self, window, tmp_path):
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent
        from tests.test_imports_files import WXR
        path = tmp_path / "dropped.xml"
        path.write_text(WXR, encoding="utf-8")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        event = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        window.dropEvent(event)
        settle()
        assert window.sidebar.chosen().title == "dropped.xml"

    def test_a_single_post_found_while_following_opens_once(self, window):
        from tests.imports_fakes import TWO_ITEM_FEED
        from nostr.rss.parser import parse_feed
        result = SimpleNamespace(url="https://example.com/a", feed=parse_feed(TWO_ITEM_FEED))
        window._open_once(result)
        settle()
        chosen = window.sidebar.chosen()
        assert chosen.kind == FILE and chosen.glyph == "link"

    def test_following_a_list_says_how_many(self, window):
        window._followed_list(3, 1, 2)
        assert window.banner.isVisibleTo(window)
        assert window.banner.label.text() == ("Following 3 new sources. "
                                              "1 was followed already. "
                                              "2 couldn't be followed.")
        window.banner.close_button.click()
        assert not window.banner.isVisibleTo(window)


class TestScreenReaders:
    def test_every_control_has_a_name(self, window):
        assert unnamed_controls(window) == []

    def test_also_with_a_message_and_an_empty_list(self, window):
        window.banner.say("Skipped 2 posts.", "Undo", "undo")
        window.sidebar.select(LIST, SKIPPED_POSTS)
        assert unnamed_controls(window) == []

    def test_an_empty_inbox_offering_to_follow(self, tmp_path):
        controller = controller_for(tmp_path)
        win = ImportsWindow(controller)
        assert win.placeholder_button is not None
        assert unnamed_controls(win) == []
        controller.account_changed(None)


class TestUnsubscribe:
    def test_it_asks_first_and_keeps_the_drafts(self, tmp_path):
        controller = controller_for(tmp_path)
        fill(controller)
        asked = []
        answer = {"yes": False}
        win = ImportsWindow(controller, confirm=lambda **kw: asked.append(kw) or answer["yes"])
        entry = next(e for e in entries(win) if e.kind == SOURCE and e.title == "field notes")
        win.unsubscribe(entry)
        assert asked[0]["title"] == "Unsubscribe from \u201cfield notes\u201d?"
        assert asked[0]["message"] == "Its posts leave Imports. Drafts you already created stay."
        assert controller.is_followed(FIELD)
        answer["yes"] = True
        win.unsubscribe(entry)
        assert not controller.is_followed(FIELD)
        assert all(e.key != entry.key for e in entries(win))
        controller.account_changed(None)

    def test_it_is_in_the_sources_menu(self, window):
        entry = next(e for e in entries(window) if e.kind == SOURCE)
        menu = source_menu(window, entry)
        assert [a.text() for a in menu.actions() if a.text()][-1] == "Unsubscribe\u2026"
        asked = []
        window._confirm_with = lambda **kw: asked.append(kw) or False
        command(menu, "Unsubscribe\u2026").trigger()
        assert asked and window._controller.is_followed(entry.url)


class TestWhatThePersonIsDoingStays:
    """Review M1: any change to the list of sources (a source followed on
    another device, a first check renaming one) used to clear the search,
    the checks and the open post."""

    def test_a_new_source_elsewhere_changes_nothing_here(self, window):
        window.search.setText("s")
        window._show_list()
        model = window.posts.model_
        assert model.rowCount() >= 2
        model.set_checked(0, True)
        window.posts.setCurrentIndex(model.index(1))
        opened = window.posts.current_post().key
        checked = [p.key for p in model.checked()]
        window._controller.subscriptions.add_feed("https://elsewhere.example/feed", "Zzz")
        settle()
        assert window.search.text() == "s"
        assert [p.key for p in model.checked()] == checked
        assert window.posts.current_post().key == opened
        assert any(e.title == "Zzz" for e in entries(window))

    def test_a_source_shown_keeps_its_segment(self, window):
        window.sidebar.select(SOURCE, source_key(JOURNAL))
        window.segment_older.click()
        window._controller.subscriptions.add_feed("https://elsewhere.example/feed", "Aaa")
        settle()
        assert window.segment_older.isChecked()
        assert titles(window) == ["Archive"]


def test_a_long_source_title_elides_and_keeps_the_segments(tmp_path):
    """Review M2: a long title was cut without an ellipsis and the New
    and Older control showed no labels at all."""
    controller = controller_for(tmp_path)
    long = "Verbraucherzentrale Nordrhein-Westfalen Pressemitteilungen Aktuell"
    controller.subscriptions.add_feed(JOURNAL, long)
    win = ImportsWindow(controller)
    win.resize(1180, 760)
    win.show()
    settle()
    win.sidebar.select(SOURCE, source_key(JOURNAL))
    settle()
    assert win.list_title.text() == long
    assert win.list_title.painted_text().endswith("\u2026")
    for segment in (win.segment_new, win.segment_older):
        assert segment.width() >= segment.sizeHint().width()
    win.close()
    controller.account_changed(None)


class TestReviewLows:
    """The CP1 review's small findings (L1, L3, L4, L6, L9, L11)."""

    def test_the_posts_have_the_keyboard_from_the_start(self, window):
        assert window.focusWidget() is window.posts

    def test_open_website_opens_the_site_not_the_feed(self, window):
        opened = []
        window._open_url = lambda url: opened.append(url.toString())
        entry = next(e for e in entries(window) if e.kind == SOURCE and e.url == JOURNAL)
        command(source_menu(window, entry), "Open Website").trigger()
        assert opened == ["https://journal.example"]
        window._controller.inbox.set_site(source_key(JOURNAL),
                                          site_url="https://journal.example/blog/")
        command(source_menu(window, entry), "Open Website").trigger()
        assert opened[-1] == "https://journal.example/blog/"
        command(source_menu(window, entry), "Copy Address").trigger()
        assert QApplication.clipboard().text() == JOURNAL

    def test_a_check_under_way_says_so(self, window):
        entry = next(e for e in entries(window) if e.kind == SOURCE and e.automatic)
        window._controller.is_checking = lambda key: True
        window._controller.check_now = lambda key: False
        window.check_now(entry)
        assert window.list_subtitle.text() == "Checking for new posts…"

    def test_a_link_that_cannot_be_opened_says_so(self, window):
        window.article.link_refused.emit("mailto:someone@example.com")
        assert window.banner.label.text() == "That link can't be opened from here."


def test_a_closed_window_is_freed_without_the_collector(tmp_path):
    """Left to the cycle collector, a window without a parent can crash it
    (Shiboken hands its children back to Python one by one). Nothing its
    children keep may hold the window: no closure over it on their
    signals, no callback of it in their attributes."""
    import gc
    import weakref
    controller = controller_for(tmp_path)
    fill(controller)
    win = ImportsWindow(controller, load_settings=lambda: {},
                        save_settings=lambda value: None)
    win.show()
    settle()
    entry = next(e for e in entries(win) if e.kind == SOURCE and e.automatic)
    source_menu(win, entry)
    for button in win._segment_group.buttons():
        button.click()
    win.search.setText("s")
    for name in ("follow_website", "import_file", "import_link"):
        getattr(win, name)()
        win.sheet().reject()
        settle()
    win._show_job_card()
    win.show_view(LIST, INBOX)
    win.posts.setCurrentIndex(win.posts.model_.index(0))
    win.skip_or_restore()           # the Undo banner holds no callback of the window
    assert win.banner.action is not None
    win.close()
    settle()
    ref = weakref.ref(win)
    gc.collect()
    # As it was afterwards: the test run collects on its own (conftest).
    collecting = gc.isenabled()
    gc.disable()
    try:
        del win
        assert ref() is None
    finally:
        if collecting:
            gc.enable()
        controller.account_changed(None)
        settle()


def test_the_list_of_sources_says_when_a_change_is_not_shared(tmp_path):
    """CP2 review N3: the list's messages reached no one since the Feeds
    page went; a change that could not be shared waited without a word."""
    from nostr.imports.subscriptions import NOT_SHARED
    from tests.test_imports_subscriptions import Offline
    relay, scheduler = Offline(), FakeScheduler()
    controller = controller_for(tmp_path, relay=relay, scheduler=scheduler)
    win = ImportsWindow(controller, load_settings=lambda: {},
                        save_settings=lambda value: None)
    win.show()
    settle()
    controller.subscriptions.add_feed(JOURNAL, "A journal")
    scheduler.fire()
    settle()
    assert win.notice.text() == NOT_SHARED and not win.notice.isHidden()
    relay.online = True
    scheduler.fire()            # the next try, now answered
    settle()
    assert win.notice.text() == "" and win.notice.isHidden()
    win.close()
    controller.account_changed(None)
    settle()


def test_a_list_of_sources_not_read_offers_try_again_which_reads_it(tmp_path):
    # Lessons 36: one quiet line in place, read again quietly meanwhile,
    # and Try Again to read it now.
    from nostr.imports.subscriptions import NOT_READ
    from tests.test_imports_subscriptions import Offline
    relay, scheduler = Offline(), FakeScheduler()
    controller = controller_for(tmp_path, relay=relay, scheduler=scheduler)
    win = ImportsWindow(controller, load_settings=lambda: {},
                        save_settings=lambda value: None)
    win.show()
    settle()
    assert win.notice.text() == NOT_READ
    assert win.notice_retry.isVisible() and win.notice_retry.text() == "Try Again"
    relay.online = True
    win.notice_retry.click()
    settle()
    assert win.notice.isHidden() and not win.notice_retry.isVisible()
    assert not win.notice_row.isVisible()
    win.close()
    controller.account_changed(None)
    settle()


def test_a_change_not_shared_offers_no_try_again(tmp_path):
    # Tried again on its own; nothing for the person to do.
    from tests.test_imports_subscriptions import Offline
    relay, scheduler = Offline(), FakeScheduler()
    relay.online = True
    controller = controller_for(tmp_path, relay=relay, scheduler=scheduler)
    win = ImportsWindow(controller, load_settings=lambda: {},
                        save_settings=lambda value: None)
    win.show()
    settle()
    relay.online = False
    controller.subscriptions.add_feed(JOURNAL, "A journal")
    scheduler.fire()
    settle()
    assert win.notice.isVisible() and not win.notice_retry.isVisible()
    win.close()
    controller.account_changed(None)
    settle()


def test_show_sidebar_is_reachable_with_the_keyboard(window):
    """CP2 review N9: on Windows and Linux this window has no menu bar of
    its own, and the toolbar's sidebar button took no focus, so a folded
    sidebar (a narrow window) could not be brought back from the keyboard."""
    button = window.sidebar_button
    assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
    window.act_sidebar.setChecked(False)
    assert not window.act_sidebar.isChecked()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert window.act_sidebar.isChecked()
    # In the Tab chain: Shift+Tab from the Add button reaches it.
    window.add_button.setFocus(Qt.FocusReason.TabFocusReason)
    assert window.add_button.previousInFocusChain() is button or \
        button.nextInFocusChain() is window.add_button
