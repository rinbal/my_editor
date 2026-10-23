# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Published view (nostr/ui/published_view.py): its states, its rows,
its actions and its keys.

What must hold: every state says what it is in words (no account,
loading, nothing published yet, could not be read with Try Again); a row
says its title, Article or Note, and when it first went out, also to a
screen reader; Return opens (an article for editing, a note in the
browser); Delete asks first, with Cancel as the default button and the
owner's wording; a deletion no relay took offers Try Again; the Menu key
opens the menu of the current row; every control has a name.
"""

import os
import time
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import alerts  # noqa: E402
from nostr.published import LOADING, PAGE_SIZE, PublishedList  # noqa: E402
from nostr.ui import published_view as pv  # noqa: E402
from nostr.ui.post_list import ROW_ROLE, list_date  # noqa: E402
from nostr.ui.published_view import PublishedView  # noqa: E402
from tests.accessibility import unnamed_controls  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    NOW, FakeClient, FakeRelayDirectory, FakeSessionPool, PK, Profile, signed,
)
from tests.published_fakes import Relays  # noqa: E402
from tests.widget_lifetime import delete_new_windows  # noqa: E402

R1, R2 = "wss://one.example", "wss://two.example"
REAL_ALERT = alerts.Alert          # kept before the tests stand in for it


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every view and window a test makes is deleted after it (see
    tests/widget_lifetime.py)."""
    yield from delete_new_windows()


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


class AnsweringAlert:
    """Stands in for alerts.Alert: records what was asked, answers from a
    list (a value, or a function of what was asked that returns one)."""

    asked = []
    answers = []

    def __init__(self, parent, *, title, message="", buttons=(), caution=False,
                 details="", checkbox="", is_dark=None, nostr=False):
        self.record = {"title": title, "message": message, "buttons": list(buttons),
                       "caution": caution, "details": details, "nostr": nostr}
        AnsweringAlert.asked.append(self.record)

    def run(self):
        answer = AnsweringAlert.answers.pop(0)
        return answer(self.record) if callable(answer) else answer


@pytest.fixture(autouse=True)
def answered_alerts(monkeypatch):
    AnsweringAlert.asked, AnsweringAlert.answers = [], []
    monkeypatch.setattr(alerts, "Alert", AnsweringAlert)
    return AnsweringAlert


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(pv.QDesktopServices, "openUrl", lambda url: urls.append(url.toString()))
    return urls


def note(text, *, at=NOW - 1000):
    return signed(1, [["client", "MyEditor"]], text, created_at=at)


def article(d, title, *, at=NOW - 2000):
    return signed(30023, [["d", d], ["title", title]], "Body", created_at=at)


def make(*stored, refuse_publish=(), down=()):
    relays = Relays(refuse_publish=refuse_publish)
    relays.keep(R1, *stored)
    relays.down = set(down)
    model = PublishedList(relay_pool=relays,
                          relay_directory=FakeRelayDirectory({PK: [R1, R2]}),
                          session_pool=FakeSessionPool(FakeClient()), clock=lambda: NOW)
    view = PublishedView(model, is_dark=False)
    view.resize(320, 480)
    said = []
    view.status_message.connect(said.append)
    edited = []
    view.open_requested.connect(edited.append)
    return view, model, relays, said, edited


def shown(view):
    """What the view shows now: the list's rows, or the placeholder's words."""
    if view._stack.currentWidget() is view._list:
        return [view._rows.data(view._rows.index(row)) for row in range(view._rows.rowCount())]
    return (view._empty_title.text() if view._empty_title.isVisibleTo(view) else "",
            view._empty_body.text(), view._busy.isVisibleTo(view),
            view._empty_action.isVisibleTo(view))


def select(view, row):
    view._list.setCurrentIndex(view._rows.index(row))


# -- states --------------------------------------------------------------------------------

def test_no_account():
    view, _model, _relays, _said, _edited = make()
    assert shown(view) == ("No account in use", "Choose an account in the Nostr menu to see "
                           "what you published.", False, False)
    assert unnamed_controls(view) == []


def test_loading_then_nothing_published_yet():
    view, model, relays, _said, _edited = make()
    view.set_account(Profile())
    assert model.state == LOADING
    assert shown(view) == ("", "Looking for your notes and articles…", True, False)
    relays.run()
    assert shown(view) == ("Nothing Published Yet",
                           "Notes and articles you publish appear here.", False, False)
    assert unnamed_controls(view) == []


def test_could_not_be_read_offers_try_again():
    view, model, relays, _said, _edited = make(note("hello"), down=(R1, R2))
    view.set_account(Profile())
    relays.run()
    assert shown(view) == ("Couldn’t load what you published",
                           "Check your internet connection, then try again.", False, True)
    assert unnamed_controls(view) == []
    relays.down = set()
    view._empty_action.click()
    assert model.state == LOADING
    relays.run()
    assert shown(view) == ["hello"]


def test_a_row_says_its_title_what_it_is_and_when_it_first_went_out():
    view, _model, relays, _said, _edited = make(article("essay", "Essay"), note("Hello there"))
    view.set_account(Profile())
    relays.run()
    assert shown(view) == ["Hello there", "Essay"]
    rows = view._rows
    essay, hello = rows.data(rows.index(1), ROW_ROLE), rows.data(rows.index(0), ROW_ROLE)
    # Line 2: what it is, when it first went out, the words after the title.
    assert (essay.title, essay.kind, essay.detail) == ("Essay", "article", "Body")
    assert essay.when == list_date(NOW - 2000, int(time.time()))
    assert (hello.kind, hello.detail) == ("note", "No additional text")
    # Grouped by date: the first row starts its section.
    assert hello.section
    spoken = rows.data(rows.index(1), Qt.ItemDataRole.AccessibleTextRole)
    assert spoken.startswith("Essay. Article. First published ")
    # No tooltip repeating a row shown whole (the painter's rule).
    assert rows.data(rows.index(1), Qt.ItemDataRole.ToolTipRole) is None
    assert view._list.accessibleName() == "Published"
    assert unnamed_controls(view) == []


def test_an_untitled_item_still_has_a_name():
    view, _model, relays, _said, _edited = make(note("   "))
    view.set_account(Profile())
    relays.run()
    assert shown(view) == ["Untitled"]


def test_nothing_shown_yet_while_older_notes_may_be_there_is_not_nothing_published():
    """Review F6: when the pages read so far were all deleted, the view
    offers Load More instead of "Nothing published yet"."""
    from nostr import events
    from nostr.deletion import build_deletion
    from tests.outbox_fakes import SK
    many = [note(f"note {n}", at=NOW - 10 * n) for n in range(PAGE_SIZE * 6)]
    request = events.sign_event(build_deletion(PK, event_ids=[n["id"] for n in many],
                                               kinds=[1], created_at=NOW), SK)
    view, model, relays, _said, _edited = make(*many, request)
    view.set_account(Profile())
    relays.run()
    assert model.items() == [] and model.has_more
    assert shown(view) == ["Load More Notes"]


def test_load_more_is_the_last_row_and_return_on_it_loads_more():
    notes = [note(f"note {n}", at=NOW - 10 * n) for n in range(PAGE_SIZE + 3)]
    view, model, relays, _said, _edited = make(*notes)
    view.set_account(Profile())
    relays.run()
    rows = view._rows
    assert rows.rowCount() == PAGE_SIZE + 1 and shown(view)[-1] == "Load More Notes"
    select(view, PAGE_SIZE)
    QTest.keyClick(view._list, Qt.Key.Key_Return)
    assert model.loading_more and shown(view)[-1] == "Loading…"
    relays.run()
    assert shown(view) == [f"note {n}" for n in range(PAGE_SIZE + 3)]


def test_load_more_that_failed_says_so_and_stays_the_way_to_try_again():
    notes = [note(f"note {n}", at=NOW - 10 * n) for n in range(PAGE_SIZE + 1)]
    view, model, relays, said, _edited = make(*notes)
    view.set_account(Profile())
    relays.run()
    relays.down = {R1, R2}
    model.load_more()
    relays.run()
    rows = view._rows
    last = rows.index(rows.rowCount() - 1)
    assert rows.data(last) == "Load More Notes"             # the action stays whole
    assert rows.data(last, pv.PROBLEM_ROLE) == "Couldn’t load."
    assert rows.data(last, Qt.ItemDataRole.AccessibleTextRole).endswith(
        "Press Return to try again.")
    assert said[-1] == ("Couldn’t load more notes. Check your internet connection, "
                        "then try again.")
    relays.down = set()
    view._list.setCurrentIndex(last)
    QTest.keyClick(view._list, Qt.Key.Key_Return)
    relays.run()
    assert rows.rowCount() == PAGE_SIZE + 1 and not model.has_more


# -- actions --------------------------------------------------------------------------------

def test_return_opens_an_article_for_editing_and_a_note_in_the_browser(opened):
    essay = article("essay", "Essay")
    view, _model, relays, _said, edited = make(essay, note("hello"))
    view.set_account(Profile())
    relays.run()
    select(view, 1)
    QTest.keyClick(view._list, Qt.Key.Key_Return)
    assert [item.id for item in edited] == [essay["id"]] and opened == []
    select(view, 0)
    QTest.keyClick(view._list, Qt.Key.Key_Enter)
    assert len(opened) == 1 and opened[0].startswith("https://njump.me/nevent1")


def test_an_article_without_a_nostr_address_still_opens_on_standup():
    """Review F17: an identifier over 255 bytes has no Nostr address, so
    no app that reads one can show it; Standup addresses an article by its
    author and identifier, so it can, and the menu offers it."""
    view, _model, relays, _said, _edited = make(article("ü" * 130, "Long"))
    view.set_account(Profile())
    relays.run()
    menu = view.menu_for(view._rows.index(0))
    enabled = {a.text(): a.isEnabled() for a in menu.actions() if not a.isSeparator()}
    assert enabled == {"Edit Article": True, "Open in Standup Discover": True,
                       "Open With": True, "Copy Link": True, "Delete from Nostr…": True}
    said = []
    view.status_message.connect(said.append)
    view._page_check = served()                      # not on Standup, and no njump
    next(a for a in menu.actions() if a.text() == "Open in Standup Discover").trigger()
    assert said == ["Standup doesn’t show this post yet."]


def test_a_row_and_its_confirmation_name_a_markdown_note_in_words(answered_alerts):
    """Review F7: no Markdown in a row or in the confirmation's title."""
    view, _model, relays, _said, _edited = make(
        note("![cover](https://example.com/c.png)\nHello **world**"))
    view.set_account(Profile())
    relays.run()
    assert shown(view) == ["Hello world"]
    select(view, 0)
    answered_alerts.answers = [False]
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    assert answered_alerts.asked[0]["title"] == "Delete “Hello world” from Nostr?"


def test_the_menu_of_an_article_and_of_a_note():
    view, _model, relays, _said, _edited = make(article("essay", "Essay"), note("hello"))
    view.set_account(Profile())
    relays.run()
    words = lambda menu: [a.text() for a in menu.actions() if not a.isSeparator()]  # noqa: E731
    article_menu = view.menu_for(view._rows.index(1))
    assert words(article_menu) == ["Edit Article", "Open in Standup Discover", "Open With",
                                   "Copy Link", "Delete from Nostr…"]
    assert article_menu.defaultAction().text() == "Edit Article"
    # Open With lists every app that shows an article, Standup Discover first.
    open_with = next(m for m in article_menu.findChildren(QMenu) if m.title() == "Open With")
    assert words(open_with) == ["Standup Discover", "njump", "YakiHonne", "Primal",
                                "noStrudel"]
    note_menu = view.menu_for(view._rows.index(0))
    assert words(note_menu) == ["Open in njump", "Open With", "Copy Link", "Delete from Nostr…"]
    assert note_menu.defaultAction().text() == "Open in njump"
    # Discover shows no single note, so a note's menus never offer it.
    note_apps = next(m for m in note_menu.findChildren(QMenu) if m.title() == "Open With")
    assert "Standup Discover" not in words(note_apps)
    delete = note_menu.actions()[-1]
    assert not delete.shortcut().isEmpty()        # the key that does it, shown
    assert view.menu_for(view._rows.index(5)) is None


SITES = "https://media.einundzwanzig.space"
DISCOVER = "https://discover.einundzwanzig.space"


def served(*prefixes):
    """A page check (web_readers.PageCheck.first_found) for a Standup that
    serves the addresses starting with ``prefixes``; it notes what it was
    asked, in order."""
    def first_found(urls, done):
        first_found.asked.append(list(urls))
        for url in urls:
            if any(url.startswith(prefix) for prefix in prefixes):
                return done(url, True)
        done(urls[-1], False)
    first_found.asked = []
    return first_found


def test_copy_link_puts_the_address_that_is_there_on_the_clipboard():
    view, _model, relays, said, _edited = make(article("essay", "Essay"))
    view._page_check = served(f"{DISCOVER}/a/")
    view.set_account(Profile())
    relays.run()
    menu = view.menu_for(view._rows.index(0))
    next(a for a in menu.actions() if a.text() == "Copy Link").trigger()
    # The default app's link, so a shared link points to Standup.
    assert QGuiApplication.clipboard().text() == f"{DISCOVER}/a/{PK}/essay"
    assert said[-1] == "Link copied."


def test_an_article_in_no_site_opens_in_discover_and_njump_where_standup_has_none(opened):
    """Seen in the app: every article opened on njump, because Discover
    serves no /a/<naddr> page; it serves /a/<author>/<d> for any article it
    has collected."""
    view, _model, relays, said, _edited = make(article("essay", "Essay"))
    view._page_check = served()                       # Standup has no page for it
    view.set_account(Profile())
    relays.run()
    view.preview_act(view._rows.item_at(0).id, "open:discover")
    assert view._page_check.asked == [[f"{DISCOVER}/a/{PK}/essay"]]
    assert opened and opened[0].startswith("https://njump.me/naddr1")
    assert said[-1] == "Standup doesn’t show this article yet, so it opened on njump."
    opened.clear()
    view._page_check = served(f"{DISCOVER}/a/")
    view.preview_act(view._rows.item_at(0).id, "open:discover")
    assert opened == [f"{DISCOVER}/a/{PK}/essay"]
    # Another app is not checked first.
    opened.clear()
    view._page_check = lambda urls, done: (_ for _ in ()).throw(AssertionError("asked"))
    view.preview_act(view._rows.item_at(0).id, "open:primal")
    assert opened == [opened[0]] and opened[0].startswith("https://primal.net/a/naddr1")


def test_an_article_in_a_site_opens_on_the_sites_page(opened):
    """An article a Standup site lists is read on the site's page, as
    Discover's own cards link it: the owner-qualified address where
    Standup serves it (from 1.5), else the site's short one, else
    Discover's page for the article."""
    site = signed(30004, [["d", "buho-ecosystem"], ["title", "Buho-Ecosystem"],
                          ["a", f"30023:{PK}:essay"]], "", created_at=NOW - 9000)
    view, _model, relays, _said, _edited = make(article("essay", "Essay"), site)
    view.set_account(Profile())
    relays.run()
    essay = next(index for index in range(view._rows.rowCount())
                 if view._rows.item_at(index).is_article)
    key = "YnVoby1lY29zeXN0ZW0"                       # buho-ecosystem, URL-safe base64
    for served_now, expected in (
            ((f"{SITES}/s/buho-ecosystem/", f"{DISCOVER}/a/"),        # Standup today
             f"{SITES}/s/buho-ecosystem/essay"),
            ((f"{SITES}/s/by/", f"{SITES}/s/buho-ecosystem/"),       # Standup 1.5
             f"{SITES}/s/by/{PK}/{key}/essay")):
        opened.clear()
        view._page_check = served(*served_now)
        view.preview_act(view._rows.item_at(essay).id, "open:discover")
        assert view._page_check.asked == [[f"{SITES}/s/by/{PK}/{key}/essay",
                                           f"{SITES}/s/buho-ecosystem/essay",
                                           f"{DISCOVER}/a/{PK}/essay"]]
        assert opened == [expected]


def test_delete_asks_first_with_the_owners_words_and_cancel_as_the_default(answered_alerts):
    view, model, relays, _said, _edited = make(article("essay", "Essay"))
    view.set_account(Profile())
    relays.run()
    select(view, 0)
    answered_alerts.answers = [False]
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    (asked,) = answered_alerts.asked
    assert asked["title"] == "Delete “Essay” from Nostr?"
    assert asked["message"] == ("Relays that keep it are asked to remove it. People and apps "
                                "that already saved a copy may still have it. Your draft stays.")
    assert relays.published == [] and len(model.items()) == 1
    # The real alert, built from what the view asked for.
    dialog = REAL_ALERT(None, title=asked["title"], message=asked["message"],
                        buttons=asked["buttons"], caution=asked["caution"])
    assert dialog.buttons["Cancel"].isDefault()
    assert not dialog.buttons["Delete"].isDefault()
    assert dialog.buttons["Delete"].objectName() == "destructive"
    dialog.deleteLater()


def test_confirming_deletes_and_the_row_goes(answered_alerts):
    view, model, relays, said, _edited = make(article("essay", "Essay"), note("kept"))
    view.set_account(Profile())
    relays.run()
    select(view, 1)
    answered_alerts.answers = [True]
    QTest.keyClick(view._list, Qt.Key.Key_Backspace)
    assert view._rows.data(view._rows.index(1), ROW_ROLE).detail == "Deleting…"
    relays.run()
    assert shown(view) == ["kept"]
    assert said[-1] == "2 of 2 relays accepted the request to delete “Essay”."
    assert view._list.currentIndex().row() == 0          # the keyboard stays in the list


def test_a_deletion_no_relay_took_offers_try_again(answered_alerts):
    view, model, relays, _said, _edited = make(note("hello"), refuse_publish={R1, R2})
    view.set_account(Profile())
    relays.run()
    select(view, 0)

    def try_again_once_the_relays_are_back(_record):
        relays.publisher.refuse = set()
        return True

    answered_alerts.answers = [True, try_again_once_the_relays_are_back]
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    relays.run()
    failure = answered_alerts.asked[1]
    assert failure["title"] == "Couldn’t delete “hello”"
    assert failure["message"].startswith("No relay accepted the request.")
    assert [(b.label, b.role) for b in failure["buttons"]] == [
        ("Cancel", alerts.CANCEL), ("Try Again", alerts.DEFAULT)]
    assert R1 in failure["details"]
    relays.run()
    assert shown(view) == ("Nothing Published Yet",
                           "Notes and articles you publish appear here.", False, False)


def test_relays_that_refused_are_not_blamed_on_the_connection(answered_alerts):
    """Review F9: a relay that answered with a refusal was reached; the
    advice to check the connection is for relays that never answered."""
    view, model, relays, _said, _edited = make(note("hello"), refuse_publish={R1, R2})
    relays.publisher.reason = "rate-limited: slow down"
    view.set_account(Profile())
    relays.run()
    select(view, 0)
    answered_alerts.answers = [True, False]         # Delete, then Cancel
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    relays.run()
    failure = answered_alerts.asked[1]
    assert failure["message"].startswith("The relays turned the request down.")
    assert "rate-limited: slow down" in failure["details"]


def test_keys_act_on_the_selected_row_only(answered_alerts, opened):
    """Review F8: when the list appears its first row is where the
    keyboard rests, not a selection; Delete and Return do nothing until
    a row is selected."""
    view, _model, relays, _said, _edited = make(note("only note"))
    view.set_account(Profile())
    relays.run()
    assert view._list.currentIndex().row() == 0
    assert view._list.selectionModel().selectedIndexes() == []
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    QTest.keyClick(view._list, Qt.Key.Key_Return)
    assert answered_alerts.asked == [] and opened == []
    select(view, 0)
    answered_alerts.answers = [False]
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    assert answered_alerts.asked[0]["title"] == "Delete “only note” from Nostr?"


def test_a_note_says_the_words_after_its_first_line():
    view, _model, relays, _said, _edited = make(note("Coffee first\nthen the long article"))
    view.set_account(Profile())
    relays.run()
    row = view._rows.data(view._rows.index(0), ROW_ROLE)
    assert (row.title, row.detail) == ("Coffee first", "then the long article")


def test_a_deletion_left_as_it_is_keeps_the_row(answered_alerts):
    view, model, relays, _said, _edited = make(note("hello"), refuse_publish={R1, R2})
    view.set_account(Profile())
    relays.run()
    select(view, 0)
    answered_alerts.answers = [True, False]         # Delete, then Cancel
    QTest.keyClick(view._list, Qt.Key.Key_Delete)
    relays.run()
    assert shown(view) == ["hello"] and model.deletion_state(model.items()[0].id) == ""


def test_the_menu_key_opens_the_menu_of_the_current_row():
    view, _model, relays, _said, _edited = make(note("one", at=NOW - 1), note("two"))
    view.set_account(Profile())
    relays.run()
    asked = []
    view._list.menu_requested.disconnect()
    view._list.menu_requested.connect(lambda index, _point: asked.append(index.row()))
    select(view, 1)
    QApplication.sendEvent(view._list, QContextMenuEvent(
        QContextMenuEvent.Reason.Keyboard, QPoint(5, 5), view._list.mapToGlobal(QPoint(5, 5))))
    assert asked == [1]


def test_both_themes_build():
    view, _model, relays, _said, _edited = make(note("hello"))
    view.set_account(Profile())
    relays.run()
    view.apply_theme(True)
    from nostr.ui.appearance import roles
    assert view.is_dark and roles(True).panel.lower() in view.styleSheet().lower()
    view.apply_theme(False)
    assert not view.is_dark


# -- search --------------------------------------------------------------------------------

def test_search_filters_by_title_and_text_and_says_when_nothing_matches():
    view, _model, relays, _said, _edited = make(
        note("Coffee first, then the long article"), article("lotus", "Lotus v0.4.5"))
    view.set_account(Profile())
    relays.run()
    view.set_search_text("lotus")
    assert [row for row in shown(view) if row] == ["Lotus v0.4.5"]
    view.set_search_text("COFFEE")
    assert shown(view)[0].startswith("Coffee first")
    view.set_search_text("nothing like it")
    assert shown(view) == ("No Results for “nothing like it”",
                           "Check the spelling or try a new search.", False, False)
    view.set_search_text("")
    assert len([row for row in shown(view) if row]) == 2


def test_the_panel_search_field_follows_the_view_shown():
    """One search field over both views (level with the document tabs):
    named for the view shown, filtering it, and keeping what was typed in
    each view."""
    from nostr.ui.drafts_panel import DraftsPanel
    view, _model, relays, _said, _edited = make(article("lotus", "Lotus v0.4.5"),
                                                note("gm"))
    view.set_account(Profile())
    relays.run()
    panel = DraftsPanel(is_dark=False)
    pv.add_to_panel(panel, view)
    field = panel._search_edit
    assert field.placeholderText() == "Search Drafts"
    field.setText("plain")
    panel.show_view(pv.PANEL_VIEW)
    assert field.placeholderText() == field.accessibleName() == "Search Published"
    assert field.text() == ""
    field.setText("lotus")
    assert [row for row in shown(view) if row] == ["Lotus v0.4.5"]
    panel.show_view(panel.DRAFTS_VIEW)
    assert field.text() == "plain"
    panel.show_view(pv.PANEL_VIEW)
    assert field.text() == "lotus"


# -- the preview ---------------------------------------------------------------------------

def test_a_click_on_a_published_post_opens_its_full_preview(monkeypatch):
    """As in the Drafts view: a click shows the post styled as Nostr shows
    it, with its next step; an article's is Edit Article, a note's Open in
    Browser, and Delete from Nostr… is last in the ⋯ menu."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    view, _model, relays, _said, edited = make(article("essay", "Essay"), note("Hello there"))
    view.set_account(Profile())
    relays.run()
    view.show()
    view.activateWindow()
    QApplication.processEvents()
    controller = view._preview

    def click(row):
        rect = view._list.visualRect(view._rows.index(row))
        at = QPointF(rect.left() + 20, rect.bottom() - 10)
        QApplication.sendEvent(view._list.viewport(), QMouseEvent(
            QEvent.MouseButtonPress, at, at, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

    click(1)                                          # the article
    assert controller.is_full() is True
    bar = controller.popover().action_bar()
    assert bar.primary.text() == "Edit Article"
    more = [a.text() for a in bar.more.menu().actions() if not a.isSeparator()]
    assert more[-1] == "Delete from Nostr…"
    assert controller.popover().reader().toPlainText().strip() == "Body"
    bar.primary.click()
    assert [item.title for item in edited] == ["Essay"]
    click(0)                                          # the note
    assert controller.popover().action_bar().primary.text() == "Open in njump"
    view.hide()
    assert controller.is_open() is False


def test_a_curation_is_listed_and_opens_in_standup_discover_first(opened):
    """Curations (Standup sites and magazines) are among what was
    published: a row with what they collect, opening in Standup Discover's
    curated reader first, never offered for editing."""
    shelf = signed(30004, [["d", "my-blog"], ["title", "Weekend reading"],
                           ["a", "30023:" + "b" * 64 + ":one"],
                           ["a", "30023:" + "b" * 64 + ":two"]], "", created_at=NOW - 500)
    view, _model, relays, _said, edited = make(shelf)
    view.set_account(Profile())
    relays.run()
    row = view._rows.data(view._rows.index(0), ROW_ROLE)
    assert (row.title, row.kind, row.detail) == ("Weekend reading", "curation",
                                                 "Collects 2 articles")
    menu = view.menu_for(view._rows.index(0))
    words = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert words[0] == "Open in Standup Discover" and "Edit Article" not in words
    view._page_check = served(f"{SITES}/s/my-blog")   # Standup today: the short address
    menu.defaultAction().trigger()
    assert view._page_check.asked == [[f"{SITES}/s/by/{PK}/bXktYmxvZw", f"{SITES}/s/my-blog"]]
    assert opened == [f"{SITES}/s/my-blog"] and edited == []
