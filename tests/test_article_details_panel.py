# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins Article Details, the inspector beside the document
(nostr/ui/article_details_panel.py and the window's command).

What must hold:

  It shows the article in the tab in front, the same details the publish
  window offers (title, summary, cover, hashtags; under Advanced its
  address and when it first went out), and what is typed goes to the tab
  at once (the window's one model), never published or saved by it.
  Showing a tab's details hands nothing back; details coming back as
  typed leave the typing alone.

  An article that has its address already (a saved draft, a published
  article) shows it, not to be edited. A key in the address is said under
  the field, read with it, and opens Advanced. A tab that holds no
  article says so instead of empty fields. Every control has a name.

  The command is Nostr's (D-1): in the View menu only while an account is
  in use, always in the Nostr menu; the panel goes with the account. On a
  Mac it answers Option-Command-I, as Pages' inspector does; elsewhere no
  convention gives it a key.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from nostr import bech32  # noqa: E402
from nostr.article_details import ArticleDetails  # noqa: E402
from nostr.ui.article_details_panel import ArticleDetailsPanel  # noqa: E402
from tests.accessibility import unnamed_controls  # noqa: E402
from tests.app_process import run_window_script  # noqa: E402

DETAILS = ArticleDetails(identifier="my-post", title="My Post", summary="About it",
                         image="https://cdn.example/c.jpg", hashtags=("bitcoin",),
                         published_at=1_700_000_000)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def panel_with(details=DETAILS, fixed=False):
    panel = ArticleDetailsPanel()
    sent = []
    panel.changed.connect(sent.append)
    panel.show_article(details, fixed_identifier=fixed)
    return panel, sent


def test_it_shows_the_articles_details_and_hands_nothing_back():
    panel, sent = panel_with()
    assert panel.title_edit.text() == "My Post"
    assert panel.summary_edit.toPlainText() == "About it"
    assert panel.cover.text() == "https://cdn.example/c.jpg"
    assert panel.hashtags.tags() == ("bitcoin",)
    assert panel.identifier_edit.text() == "my-post"
    assert "2023" in panel.first_published.text()
    assert sent == []


def test_what_is_typed_goes_to_the_tab_as_the_details():
    panel, sent = panel_with()
    panel.title_edit.setText("A New Title ")
    panel.hashtags.set_tags(())
    QTest.keyClicks(panel.hashtags.entry, "Bitcoin, #Nostr")
    QTest.keyClick(panel.hashtags.entry, Qt.Key.Key_Return)
    assert sent[-1] == DETAILS.with_changes(title="A New Title",
                                            hashtags=("Bitcoin", "Nostr"))
    # Shown again as handed back: the space being typed stays.
    panel.show_article(sent[-1], fixed_identifier=False)
    assert panel.title_edit.text() == "A New Title "


def test_a_saved_articles_address_is_shown_not_edited():
    panel, _sent = panel_with(fixed=True)
    assert panel.identifier_edit.isReadOnly()
    assert not panel.identifier_note.isHidden()
    panel.show_article(ArticleDetails(title="New"), fixed_identifier=False)
    assert not panel.identifier_edit.isReadOnly()
    assert panel.identifier_note.isHidden()
    assert panel.identifier_edit.placeholderText() == "new"     # what publishing makes
    assert panel.first_published.text() == "Not published yet."


def test_a_key_in_the_address_is_said_under_it_and_opens_advanced():
    panel, sent = panel_with(ArticleDetails())
    assert not panel.advanced_toggle.isChecked()
    key = "backup-" + bech32.encode_nsec("11" * 32)
    panel.identifier_edit.setText(key)
    assert panel.advanced_toggle.isChecked()
    assert not panel.identifier_problem.isHidden()
    assert panel.identifier_edit.accessibleDescription() == panel.identifier_problem.text()


def test_a_tab_without_an_article_says_so():
    panel, _sent = panel_with()
    panel.show_no_article("This is a note.")
    assert panel._pages.currentWidget() is panel._no_article
    panel.show_article(DETAILS, fixed_identifier=False)
    assert panel._pages.currentWidget() is panel._form


def test_every_control_has_a_name():
    panel, _sent = panel_with()
    panel.advanced_toggle.setChecked(True)
    assert unnamed_controls(panel) == []


SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from main_window import DraftBinding
from nostr.drafts import INNER_KIND_SHORT_NOTE

w = main_window.MainWindow()
w.show()
ed = w.new_tab()
app.processEvents()
def in_view_menu():
    w.m_view.aboutToShow.emit()
    return w.act_article_details.isVisible()
panel = w._article_details_panel
from PySide6.QtWidgets import QMenu
nostr_menu = next(menu for menu in w.menuBar().findChildren(QMenu)
                  if menu.title() == "&Nostr")
r = {"view_without": in_view_menu(),
     "in_nostr_menu": w.act_article_details in nostr_menu.actions(),
     "enabled_without": w.act_article_details.isEnabled(),
     "keys": w.act_article_details.shortcut().toString(),
     "mac": sys.platform == "darwin"}
w.nostr_state._active = True
w._update_nostr_commands()
r["view_with"] = in_view_menu()
w.act_article_details.setChecked(True)
app.processEvents()
r["shown"] = panel.isVisible()
r["width"] = panel.width()
ed.document().setModified(False)
panel.title_edit.setText("Typed in the inspector")
r["tab_title"] = w._article_details_of(ed).title
r["modified"] = ed.document().isModified()
note = w.new_tab()
note._draft_binding = DraftBinding(identifier="n", inner_kind=INNER_KIND_SHORT_NOTE)
w._on_tab_changed(w.tabs.currentIndex())
r["note_page"] = panel._pages.currentWidget() is panel._no_article
w.tabs.setCurrentIndex(w._editor_widget_index(ed))
app.processEvents()
r["back"] = panel.title_edit.text()
w.nostr_state._active = False
w._on_nostr_in_use_changed(False)
app.processEvents()
r["hidden_without"] = not panel.isVisible() and not w.act_article_details.isChecked()
for tab in (ed, note):
    tab.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_inspector_in_the_window(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["view_without"] is False and r["view_with"] is True              # D-1
    assert r["enabled_without"] is False and r["in_nostr_menu"] is True
    # Pages' inspector key; elsewhere LibreOffice's key for its sidebar.
    assert r["keys"] == ("Ctrl+Alt+I" if r["mac"] else "Ctrl+F5")
    assert r["shown"] is True and 260 <= r["width"] <= 420
    assert r["tab_title"] == "Typed in the inspector" and r["modified"] is True
    assert r["note_page"] is True
    assert r["back"] == "Typed in the inspector"
    assert r["hidden_without"] is True


def test_a_detail_kept_as_written_in_the_file_is_shown_read_only_and_says_so():
    """Review N5: a detail the file writes in a way MyEditor can't read
    safely (a mapping, an anchor) is left as written; Article Details shows
    it read-only, with a plain line under it."""
    panel = ArticleDetailsPanel()
    panel.show_article(ArticleDetails(title="T"), fixed_identifier=False,
                       kept_in_file=frozenset({"image", "tags"}))
    assert panel.cover.url_edit.isReadOnly() and panel.hashtags.isReadOnly()
    assert not panel.title_edit.isReadOnly()
    notes = [label.text() for label in panel.findChildren(QLabel)
             if not label.isHidden() and label.text() == "Kept as written in the file."]
    assert len(notes) == 2
    panel.show_article(ArticleDetails(title="T"), fixed_identifier=False)
    assert not panel.cover.url_edit.isReadOnly() and not panel.hashtags.isReadOnly()
    assert not any(label.text() == "Kept as written in the file." and not label.isHidden()
                   for label in panel.findChildren(QLabel))


def test_a_hashtag_holding_a_comma_is_shown_and_handed_back_whole():
    panel = ArticleDetailsPanel()
    sent = []
    panel.changed.connect(sent.append)
    panel.show_article(ArticleDetails(title="T", hashtags=("a", "b, c")), fixed_identifier=False)
    assert [chip.accessibleName() for chip in panel.hashtags.chips()] == ["a", "b, c"]
    QTest.keyClicks(panel.hashtags.entry, "d")
    QTest.keyClick(panel.hashtags.entry, Qt.Key.Key_Return)
    assert sent[-1].hashtags == ("a", "b, c", "d")


@pytest.mark.parametrize("submitted, fixed, read_only, why", [
    (True, False, True, True),
    (True, True, True, True),                       # submitted says more than saved
    (False, False, False, False),
], ids=["submitted", "submitted and saved", "neither"])
def test_a_submitted_articles_link_name_is_fixed_and_says_why(submitted, fixed, read_only, why):
    # RG-M2r 10: another link name would be another submission.
    panel = ArticleDetailsPanel()
    panel.show_article(DETAILS, fixed_identifier=fixed, submitted=submitted)
    assert panel.identifier_edit.isReadOnly() is read_only
    assert bool(panel.identifier_edit.toolTip()) is why
    assert panel.identifier_edit.accessibleDescription() == panel.identifier_edit.toolTip()
    if submitted:
        assert panel.identifier_note.text() == "A submitted article keeps its link name."
        assert not panel.identifier_note.isHidden()
    panel.show_article(DETAILS, fixed_identifier=False)
    assert not panel.identifier_edit.isReadOnly() and panel.identifier_edit.toolTip() == ""
