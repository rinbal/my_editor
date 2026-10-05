# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the window side of the update restart (workspace_restore.py).

What must hold:

  A tab is written down with what brings it back: its file, its cursor and
  selection, its Nostr draft link, and, for unsaved or untitled content,
  the crash-recovery backup written at that moment.

  Content that is in no file and no backup is reported, never silently
  left out, and only those tabs are asked about. Not saved, such a tab
  comes back as its file on disk, or, untitled, not at all.

  A tab's scroll position comes back when the tab is first shown, not only
  for the tab that was active, and never overrides what the person did
  first.

  After a launch, the person hears which version they are on now, or that
  the update did not install. A launch on the same version says nothing.

The whole restart, with real windows, is pinned in test_update_restart.py.
"""

import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

import recovery  # noqa: E402
import workspace_restore  # noqa: E402
from editor import HtmlEditor  # noqa: E402
from workspace import DOCUMENT, WELCOME, Workspace  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    """Never touch the real ~/.cache while testing."""
    target = tmp_path / "backups"
    monkeypatch.setattr(recovery, "BACKUP_DIR", str(target))
    return target


def editor(text="", path=None, modified=False):
    ed = HtmlEditor()
    ed.setPlainText(text)
    ed._file_path = path
    ed._backup = recovery.EditorBackup(ed, path)
    ed.document().setModified(modified)
    return ed


@dataclass
class Binding:
    identifier: str
    title: str = ""


# -- writing a tab down ---------------------------------------------------------

def test_an_unsaved_untitled_tab_points_at_a_backup_written_now():
    ed = editor("draft body", modified=True)
    cursor = ed.textCursor()
    cursor.setPosition(2)
    cursor.setPosition(6, cursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cursor)

    tab = workspace_restore.capture_editor_tab(ed)

    assert tab.kind == DOCUMENT and tab.path is None
    assert tab.backup_file == os.path.abspath(ed._backup.path)
    assert os.path.exists(tab.backup_file)
    assert tab.modified is True
    assert (tab.anchor, tab.cursor) == (2, 6)


def test_a_saved_file_needs_no_backup(tmp_path):
    path = str(tmp_path / "notes.txt")
    tab = workspace_restore.capture_editor_tab(editor("saved", path=path))
    assert (tab.path, tab.backup_file, tab.modified) == (path, None, False)


def test_unsaved_content_with_nowhere_to_go_is_reported(monkeypatch):
    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 10)   # the backup can't be written
    ed = editor("a document far too large for its backup", modified=True)
    assert workspace_restore.capture_editor_tab(ed) is None


def test_an_empty_untitled_tab_is_kept_without_a_backup():
    tab = workspace_restore.capture_editor_tab(editor())
    assert tab is not None and tab.backup_file is None


def test_only_an_untouched_welcome_tab_is_written_down_as_the_welcome_tab(tmp_path):
    pristine = editor("Welcome to MyEditor")
    pristine._is_welcome = True
    assert workspace_restore.capture_editor_tab(pristine).kind == WELCOME

    saved = editor("Welcome to MyEditor", path=str(tmp_path / "welcome.html"))
    saved._is_welcome = True   # the flag stays when the tab is saved as a file
    tab = workspace_restore.capture_editor_tab(saved)
    assert (tab.kind, tab.path) == (DOCUMENT, str(tmp_path / "welcome.html"))

    edited = editor("Welcome to MyEditor, with my notes", modified=True)
    edited._is_welcome = True
    tab = workspace_restore.capture_editor_tab(edited)
    assert tab.kind == DOCUMENT and tab.backup_file


def window_of(*editors, current=0):
    tabs = QTabWidget()
    for ed in editors:
        tabs.addTab(ed, "tab")
    tabs.setCurrentIndex(current)
    return SimpleNamespace(
        tabs=tabs,
        _editor_from_widget=lambda w: w if isinstance(w, HtmlEditor) else None,
        _pdf_viewer_from_widget=lambda w: None,
    )


def unprotectable(ed):
    """An editor whose backup can't be written (a full disk, say)."""
    ed._backup.write_now = lambda: False
    return ed


def test_only_the_tabs_that_cannot_be_kept_are_asked_about(tmp_path):
    kept = editor("kept in its backup", modified=True)
    lost = unprotectable(editor("no room for this", path=str(tmp_path / "a.txt"),
                                modified=True))
    saved = editor("on disk", path=str(tmp_path / "b.txt"))
    window = window_of(kept, lost, saved)   # owns the editors while it lives
    capture = workspace_restore.capture_tabs(window)
    assert capture.unprotected == [lost]


def test_a_tab_that_was_not_saved_comes_back_as_its_file_on_disk(tmp_path):
    path = str(tmp_path / "a.txt")
    lost = unprotectable(editor("unsaved changes", path=path, modified=True))
    window = window_of(lost)   # owns the editors while it lives
    capture = workspace_restore.capture_tabs(window)
    capture.settle(lost, saved=False)
    assert capture.unprotected == []
    (tab,), active = capture.tabs()
    assert (tab.kind, tab.path, tab.backup_file, tab.modified) == (DOCUMENT, path, None, False)


def test_an_untitled_tab_that_was_not_saved_is_left_out_and_the_one_before_is_active():
    first = editor("kept", modified=True)
    lost = unprotectable(editor("nowhere to keep this", modified=True))
    last = editor("also kept", modified=True)
    window = window_of(first, lost, last, current=1)   # owns the editors while it lives
    capture = workspace_restore.capture_tabs(window)
    capture.settle(lost, saved=False)
    tabs, active = capture.tabs()
    assert [t.backup_file is not None for t in tabs] == [True, True]
    assert active == 0


def test_a_tab_that_was_saved_comes_back_as_the_file_it_was_saved_to(tmp_path):
    lost = unprotectable(editor("saved after all", modified=True))
    window = window_of(lost)   # owns the editors while it lives
    capture = workspace_restore.capture_tabs(window)
    lost._file_path = str(tmp_path / "saved.txt")   # what Save As does
    lost.document().setModified(False)
    capture.settle(lost, saved=True)
    (tab,), _ = capture.tabs()
    assert (tab.path, tab.modified) == (str(tmp_path / "saved.txt"), False)


def test_the_draft_link_is_written_down():
    ed = editor("body", modified=True)
    ed._draft_binding = Binding(identifier="abc", title="My Draft")
    tab = workspace_restore.capture_editor_tab(ed)
    assert tab.draft == {"identifier": "abc", "title": "My Draft"}


def test_a_draft_link_is_rebuilt_from_the_fields_it_knows():
    rebuild = workspace_restore._dataclass_from
    assert rebuild(Binding, {"identifier": "abc", "unknown": 1}) == Binding("abc")
    assert rebuild(Binding, {"title": "no identifier"}) is None


# -- the scroll position ---------------------------------------------------------

def long_editor():
    ed = HtmlEditor()
    ed.setPlainText("\n".join(f"line {i}" for i in range(400)))
    ed.resize(400, 300)
    return ed


def test_a_scroll_position_waits_until_the_tab_is_shown():
    # A tab that isn't current has no scroll range yet: setting the value at
    # once would leave it at the top.
    ed = long_editor()
    pending = workspace_restore.PendingScroll(ed, 1500)
    QTest.qWait(20)
    assert ed.verticalScrollBar().value() == 0 and not pending.done
    ed.show()
    try:
        QTest.qWait(50)
        assert ed.verticalScrollBar().value() == 1500
    finally:
        ed.hide()


def test_an_edit_before_the_tab_is_shown_lets_the_old_position_go():
    ed = long_editor()
    pending = workspace_restore.PendingScroll(ed, 1500)
    ed.textCursor().insertText("typed first ")
    assert pending.done
    ed.show()
    try:
        QTest.qWait(50)
        assert ed.verticalScrollBar().value() != 1500
    finally:
        ed.hide()


# -- what a launch says ------------------------------------------------------------

PAGE = "https://github.com/rinbal/my_editor/releases/tag/v3.4"


def restart(to_version, notes="- New", url="https://example.org/notes"):
    return Workspace(tabs=(), from_version="3.3", to_version=to_version,
                     release_notes=notes, release_url=url)


def test_a_restart_on_the_version_it_meant_to_reach_is_an_update():
    news = workspace_restore.version_news(restart("3.4"), "3.3", "3.4", release_page=PAGE)
    assert news == workspace_restore.VersionNews(
        updated=True, version="3.4", notes="- New", release_url="https://example.org/notes")


def test_a_restart_on_an_older_version_did_not_install():
    news = workspace_restore.version_news(restart("3.5"), "3.4", "3.4", release_page=PAGE)
    assert news == workspace_restore.VersionNews(updated=False, version="3.5")


def test_a_launch_on_a_newer_version_than_last_time_was_updated_by_hand():
    news = workspace_restore.version_news(None, "3.3", "3.4", release_page=PAGE)
    assert news == workspace_restore.VersionNews(updated=True, version="3.4", release_url=PAGE)


@pytest.mark.parametrize("last_run", ["3.4", "3.5", None, 34, "not a version"])
def test_any_other_launch_says_nothing(last_run):
    assert workspace_restore.version_news(None, last_run, "3.4", release_page=PAGE) is None
