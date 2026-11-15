# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins Write, Preview and Split (preview_pane.py and the window's views).

What must hold:

  The preview shows the document as it reads (no Nostr account in use),
  or what will be published as an article or a note, with what readers
  will not see; it draws only while shown, shortly after the text
  changed, and every control in it has a name.

  Write, Preview and Split are the View menu's commands and the switch at
  the end of the toolbar (the same actions), for documents that hold
  Markdown; plain-text and code files show only their text. The choice is
  remembered. Switching keeps the text's undo history and selection, and
  in Preview nothing edits the hidden text.

  Split needs room for two readable columns: narrower, the side used
  last shows, Split is dimmed and says why, and it splits again when
  there is room (decision Q-N).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.preview import Article  # noqa: E402
from preview_pane import (  # noqa: E402
    ARTICLE, DOCUMENT, NOTE, PreviewContent, PreviewPane, ViewSwitch,
)
from tests.accessibility import unnamed_controls  # noqa: E402

from tests.app_process import run_window_script  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def pane_for(content):
    asked = []

    def give(kind):
        asked.append(kind)
        return content(kind)

    pane = PreviewPane(give)
    pane.resize(700, 500)
    return pane, asked


def test_the_document_as_it_reads_without_a_header():
    pane, asked = pane_for(lambda kind: PreviewContent(DOCUMENT, "# Title\n\nSome **words**.\n"))
    pane.show()
    QApplication.processEvents()
    text = pane.view.toPlainText()
    assert "Title" in text and "Some words." in text
    assert pane._header.isHidden() and pane._checks.isHidden()
    assert unnamed_controls(pane) == []


def test_an_article_shows_its_header_and_what_readers_will_not_see():
    content = PreviewContent(ARTICLE, "Body words.\n",
                             Article(title="My Title", author="Alice"),
                             checks=("A picture is not uploaded yet: readers will not see it.",))
    pane, _asked = pane_for(lambda kind: content)
    pane.show()
    QApplication.processEvents()
    text = pane.view.toPlainText()
    assert text.index("My Title") < text.index("Body words.")
    assert "Alice" in text
    assert not pane._header.isHidden()
    assert not pane._checks.isHidden() and "not uploaded" in pane._checks.text()
    assert unnamed_controls(pane) == []


def test_preview_as_note_draws_plain_text():
    pane, asked = pane_for(lambda kind: PreviewContent(
        kind, "**not bold** here" if kind == NOTE else "x", Article(title="T")))
    pane.show()
    pane.kind_box.setCurrentIndex(1)
    QApplication.processEvents()
    assert asked[-1] == NOTE
    assert "**not bold** here" in pane.view.toPlainText()


def test_a_hidden_preview_does_not_draw():
    pane, asked = pane_for(lambda kind: PreviewContent(DOCUMENT, "text"))
    pane.schedule()
    QApplication.processEvents()
    assert asked == [] and not pane._timer.isActive()


def test_the_switch_is_the_view_commands_and_has_names():
    actions = [QAction(title) for title in ("Write", "Preview", "Split")]
    for action in actions:
        action.setCheckable(True)
    switch = ViewSwitch(actions)
    assert [b.defaultAction() for b in switch.buttons] == actions
    assert [b.objectName() for b in switch.buttons] == ["first", "middle", "last"]
    assert unnamed_controls(switch) == []
    assert switch.accessibleName() == "View"          # the noun, not the verb


def test_the_switch_is_the_shared_segmented_control_and_follows_its_commands():
    """Write · Preview · Split is drawn as the panel's Drafts · Published
    is (one control, a neutral thumb), and shows what the commands say."""
    from segmented_control import SegmentedControl
    actions = [QAction(title) for title in ("Write", "Preview", "Split")]
    for action in actions:
        action.setCheckable(True)
    actions[0].setChecked(True)
    switch = ViewSwitch(actions)
    assert isinstance(switch, SegmentedControl)
    assert [b.isChecked() for b in switch.buttons] == [True, False, False]
    triggered = []
    actions[1].triggered.connect(lambda: triggered.append("preview"))
    switch.buttons[1].click()
    assert triggered == ["preview"]
    actions[1].setChecked(True)
    actions[0].setChecked(False)
    assert [b.isChecked() for b in switch.buttons] == [False, True, False]
    actions[2].setEnabled(False)
    assert not switch.buttons[2].isEnabled()


# -- in the window ---------------------------------------------------------------------------

SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window, settings
from tests.accessibility import unnamed_controls

w = main_window.MainWindow()
w.resize(1500, 800)
w.show()
app.processEvents()
at_start = [a.isChecked() for a in (w.act_view_write, w.act_view_preview, w.act_view_split)]
ed = w.new_tab()
ed.setFocus()
ed.insertPlainText("Hello world, a first line")
ed.document().undo()
ed.insertPlainText("Hello world")
cursor = ed.textCursor()
cursor.setPosition(0)
cursor.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
ed.setTextCursor(cursor)
app.processEvents()


def state():
    area = ed._view_splitter.widget(0)
    pane = ed._preview_pane
    return {"text": area.isVisible(), "preview": bool(pane and pane.isVisible()),
            "checked": [a.isChecked() for a in (w.act_view_write, w.act_view_preview,
                                                 w.act_view_split)],
            "split_enabled": w.act_view_split.isEnabled()}


r = {"start": state(), "switch_shown": w.view_switch.isVisible(), "at_start": at_start}
w.act_view_preview.trigger()
app.processEvents()
r["preview"] = state()
r["preview_words"] = "Hello world" in ed._preview_pane.view.toPlainText()
r["bold_in_preview"] = w.act_bold.isEnabled()
r["undo_in_preview"] = [w.act_undo.isEnabled(), w.act_redo.isEnabled(),
                        w.format_toolbar.style_button.isEnabled()]
w._undo()                                     # the key, by habit: nothing happens
r["text_after_undo"] = ed.toPlainText()
r["select_all_in_preview"] = w.act_select_all.isEnabled()
r["names"] = unnamed_controls(ed._preview_pane) + unnamed_controls(w.view_switch)
w.act_view_write.trigger()
app.processEvents()
r["write"] = state()
r["selection_kept"] = ed.textCursor().selectedText()
r["undo_kept"] = ed.document().isUndoAvailable() and w.act_undo.isEnabled()
w.act_view_split.trigger()
app.processEvents()
r["split"] = state()
ed.moveCursor(QTextCursor.MoveOperation.End)
ed.insertPlainText(" and more")
deadline = time.monotonic() + 5
while "and more" not in ed._preview_pane.view.toPlainText() and time.monotonic() < deadline:
    QTest.qWait(20)
r["redrawn"] = "and more" in ed._preview_pane.view.toPlainText()
ed._preview_pane.view.setFocus()
app.processEvents()
w.resize(900, 800)
app.processEvents()
r["narrow"] = state()
r["narrow_tip"] = w.act_view_split.toolTip()
w.act_view_write.trigger()                  # flips the side; Split stays the choice
app.processEvents()
r["narrow_flipped"] = state()
r["mode_kept"] = settings.load_settings().get("view_mode")
w.resize(1500, 800)
app.processEvents()
r["wide_again"] = state()
txt = os.path.join(os.environ["HOME"], "plain.txt")
with open(txt, "w", encoding="utf-8") as f:
    f.write("plain\n")
plain = w.open_path(txt)
app.processEvents()
r["plain"] = {"switch_shown": w.view_switch.isVisible(),
              "text": plain._view_splitter.widget(0).isVisible()}
md = os.path.join(os.environ["HOME"], "notes.md")
with open(md, "w", encoding="utf-8") as f:
    f.write("A claim with <b>raw</b> HTML.\n\n- one\n- two\n")
source = w.open_path(md)
app.processEvents()
w.act_view_preview.trigger()
app.processEvents()
pane = source._preview_pane
r["md_source"] = {"as_text": bool(getattr(source, "_markdown_source", False)),
                  "switch_shown": w.view_switch.isVisible(),
                  "preview": bool(pane and pane.isVisible()),
                  "drawn": pane.view.toPlainText() if pane else ""}
source.document().setModified(False)
for e in (ed, plain):
    e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


WIDENED = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.resize(900, 800)
w.show()
ed = w.new_tab()
w.act_view_write.trigger()
QTest.qWait(50)


def split():
    return [w.act_view_split.isEnabled(), w.act_view_split.toolTip()]


r = {"narrow": split()}
w.resize(1500, 800)
QTest.qWait(50)
r["wide"] = split()
w.resize(900, 800)
QTest.qWait(50)
r["narrow_again"] = split()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_split_is_offered_once_the_window_is_wide_enough_in_write(tmp_path):
    """Only Split itself looked at the width: in Write, a window made wide
    enough kept Split dimmed, asking to be made wider."""
    r = run_window_script(WIDENED, tmp_path)
    wider = "Make the window wider to show both"
    assert r["narrow"] == [False, wider]
    assert r["wide"] == [True, "Split"]        # a cleared tip falls back to the name
    assert r["narrow_again"] == [False, wider]


def test_write_preview_and_split_in_the_window(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["start"] == {"text": True, "preview": False, "checked": [True, False, False],
                          "split_enabled": True}
    assert r["switch_shown"]
    assert r["at_start"] == [True, False, False]          # before any tab was switched
    assert r["preview"]["text"] is False and r["preview"]["preview"] is True
    assert r["preview"]["checked"] == [False, True, False]
    assert r["preview_words"]
    assert r["bold_in_preview"] is False and r["select_all_in_preview"] is True
    assert r["undo_in_preview"] == [False, False, False]      # nothing edits the hidden text
    assert r["text_after_undo"] == "Hello world"
    assert r["names"] == []
    assert r["write"]["text"] and not r["write"]["preview"]
    assert r["selection_kept"] == "Hello" and r["undo_kept"]
    assert r["split"]["text"] and r["split"]["preview"]
    assert r["redrawn"]
    # Narrow: the side used last (the preview had the focus), Split dimmed.
    assert r["narrow"] == {"text": False, "preview": True, "checked": [False, True, False],
                           "split_enabled": False}
    assert r["narrow_tip"] == "Make the window wider to show both"
    assert r["narrow_flipped"]["text"] and not r["narrow_flipped"]["preview"]
    assert r["mode_kept"] == "split"
    assert r["wide_again"]["text"] and r["wide_again"]["preview"]
    assert r["wide_again"]["checked"] == [False, False, True]
    assert r["plain"] == {"switch_shown": False, "text": True}
    # Markdown opened as its text (raw HTML) has its preview too: drawn
    # from its text, as it is published.
    md = r["md_source"]
    assert md["as_text"] and md["switch_shown"] and md["preview"]
    assert "A claim with <b>raw</b> HTML." in md["drawn"]
    assert "one" in md["drawn"] and "- one" not in md["drawn"]
