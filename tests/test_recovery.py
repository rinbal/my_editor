"""Pins the crash-backup lifecycle.

The defect classes this file guards against:
- a snapshot taken as plain text, so restoring it and pressing Ctrl+S
  over an .html original erases every image in the document,
- a restore deleting the replacement backup it just wrote, so recovered
  work has no crash protection until the next keystroke,
- the content fingerprint short-circuiting a write when the file it
  refers to is no longer on disk,
- a record written by a newer build being guessed at or overwritten,
- a stale backup restoring with its original path, so one Ctrl+S
  overwrites hours of newer work,
- Save As over the same path removing the live backup.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextImageFormat
from PySide6.QtWidgets import QApplication

import recovery
from doc_walk import iter_image_names
from editor import HtmlEditor
from main_window import MainWindow


ASSET_KEY = "myeditor-asset:" + "a" * 64


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


def _editor(text="hello world"):
    ed = HtmlEditor()
    ed.setHtml(f"<p>{text}</p>")
    return ed


def _editor_with_image(text="notes", key=ASSET_KEY, alt="a kitten"):
    ed = HtmlEditor()
    cursor = ed.textCursor()
    cursor.insertText(text)
    fmt = QTextImageFormat()
    fmt.setName(key)
    fmt.setProperty(QTextImageFormat.ImageAltText, alt)
    cursor.insertImage(fmt)
    return ed


def _record_of(backup) -> dict:
    with open(backup.path, "r", encoding="utf-8") as f:
        return json.load(f)


class _Tabs:
    def __init__(self):
        self.titles = []
        # The container owns the editor, so a test that drops it here
        # would have the widget collected out from under the assertion.
        self.widgets = []

    def addTab(self, widget, title):
        self.titles.append(title)
        self.widgets.append(widget)
        return len(self.titles) - 1


def _window_stub():
    """The slice of MainWindow that _restore_one_backup actually uses."""
    editors = []

    def new_editor():
        ed = _editor("")
        editors.append(ed)
        return ed

    return types.SimpleNamespace(
        _new_wired_editor=new_editor,
        _reload_from_disk=lambda *a: None,
        _attach_close_button=lambda *a: None,
        _asset_manager=types.SimpleNamespace(adopt_data_uri=lambda uri: None),
        _watcher=types.SimpleNamespace(addPath=lambda p: None),
        is_dark_theme=False,
        show_line_numbers=False,
        tabs=_Tabs(),
        editors=editors,
    )


# --------------------------------------------------------------------------- #
# Restore leaves the recovered document protected
# --------------------------------------------------------------------------- #

def test_restore_keeps_a_backup_for_the_recovered_document(tmp_path):
    # A restored document that kept its path derives the same backup ID,
    # so the replacement IS the record's own file. Deleting it left the
    # recovered work with nothing on disk until the user typed.
    source = tmp_path / "note.html"
    source.write_text("<p>on disk</p>", encoding="utf-8")
    ed = _editor("unsaved edit")
    backup = recovery.EditorBackup(ed, str(source))
    assert backup.write_now()
    records = recovery.find_all_backups()
    assert len(records) == 1

    assert MainWindow._restore_one_backup(_window_stub(), records[0])

    surviving = recovery.find_all_backups()
    assert len(surviving) == 1, "a second crash would lose the recovered work"
    assert surviving[0]["original_path"] == str(source)


def test_restore_of_a_stale_record_removes_the_old_file(tmp_path):
    # A stale record restores as an untitled copy with a backup of its
    # own, so the record it came from is genuinely obsolete.
    source = tmp_path / "note.html"
    source.write_text("<p>on disk</p>", encoding="utf-8")
    ed = _editor("older edit")
    backup = recovery.EditorBackup(ed, str(source))
    assert backup.write_now()
    record = recovery.find_all_backups()[0]
    record["source_mtime_ns"] = 0  # the file on disk has moved on

    assert MainWindow._restore_one_backup(_window_stub(), record)

    assert not os.path.exists(record["_backup_file"])
    surviving = recovery.find_all_backups()
    assert [r.get("original_path") for r in surviving] == [None]


# --------------------------------------------------------------------------- #
# The fingerprint shortcut
# --------------------------------------------------------------------------- #

def test_write_rewrites_a_backup_that_vanished(tmp_path):
    ed = _editor()
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"))
    assert backup.write_now()
    os.remove(backup.path)

    assert backup.write_now()
    assert os.path.exists(backup.path), "identical content is not proof of a file"


def test_write_skips_an_identical_payload_that_is_still_on_disk(tmp_path):
    ed = _editor()
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"))
    assert backup.write_now()
    first = os.stat(backup.path).st_mtime_ns

    assert backup.write_now()
    assert os.stat(backup.path).st_mtime_ns == first


def test_update_file_path_keeps_the_backup_when_the_path_is_unchanged(tmp_path):
    # Save As offers the current name; confirming it must not disarm
    # crash recovery for the rest of the session.
    path = str(tmp_path / "note.html")
    ed = _editor()
    backup = recovery.EditorBackup(ed, path)
    assert backup.write_now()

    backup.update_file_path(path)

    assert os.path.exists(backup.path)


def test_update_file_path_removes_the_untitled_backup(tmp_path):
    ed = _editor()
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    untitled = backup.path

    backup.update_file_path(str(tmp_path / "note.html"))

    assert not os.path.exists(untitled)
    assert backup.write_now()
    assert os.path.exists(backup.path)


# --------------------------------------------------------------------------- #
# The snapshot is HTML, so images survive a crash
# --------------------------------------------------------------------------- #

def test_v2_record_has_the_documented_shape(tmp_path):
    source = tmp_path / "note.html"
    source.write_text("<p>on disk</p>", encoding="utf-8")
    backup = recovery.EditorBackup(_editor_with_image(), str(source))

    assert backup.write_now()
    record = _record_of(backup)

    assert record["version"] == 2
    assert record["format"] == "html"
    assert record["original_path"] == str(source)
    assert record["assets"] == [ASSET_KEY]
    assert isinstance(record["saved_at"], int)
    assert record["source_mtime_ns"] == os.stat(source).st_mtime_ns


def test_the_snapshot_keeps_the_image_a_plain_text_one_would_lose(tmp_path):
    # The P0 this format exists for: a plain-text snapshot restored over
    # an .html original and saved erased every picture in it.
    backup = recovery.EditorBackup(_editor_with_image(), str(tmp_path / "note.html"))
    assert backup.write_now()

    reopened = HtmlEditor()
    reopened.setHtml(_record_of(backup)["content"])

    assert list(iter_image_names(reopened.document())) == [ASSET_KEY]


def test_a_restored_document_still_holds_its_image(tmp_path):
    source = tmp_path / "note.html"
    source.write_text("<p>on disk</p>", encoding="utf-8")
    backup = recovery.EditorBackup(_editor_with_image(), str(source))
    assert backup.write_now()
    win = _window_stub()

    assert MainWindow._restore_one_backup(win, recovery.find_all_backups()[0])

    restored = win.editors[-1]
    assert list(iter_image_names(restored.document())) == [ASSET_KEY]
    assert restored._file_path == str(source)


def test_an_untouched_tab_is_not_backed_up(tmp_path):
    backup = recovery.EditorBackup(HtmlEditor(), None)

    assert backup.write_now() is False
    assert not os.path.exists(backup.path), "an empty document's HTML is a skeleton"


def test_an_image_only_document_is_backed_up(tmp_path):
    ed = HtmlEditor()
    cursor = ed.textCursor()
    fmt = QTextImageFormat()
    fmt.setName(ASSET_KEY)
    cursor.insertImage(fmt)
    backup = recovery.EditorBackup(ed, None)

    assert backup.write_now() is True
    assert _record_of(backup)["assets"] == [ASSET_KEY]


# --------------------------------------------------------------------------- #
# Inline images are handed to the asset layer instead of re-serialized
# --------------------------------------------------------------------------- #

def test_a_large_inline_image_is_externalized_and_listed(tmp_path):
    big = "data:image/png;base64," + "A" * (recovery.MIN_EXTERNALIZE_BYTES + 64)
    small = "data:image/png;base64,AAAA"
    seen = []

    def externalize(uri):
        seen.append(uri)
        return ASSET_KEY

    ed = HtmlEditor()
    ed.setHtml(f'<p>x<img src="{big}"><img src="{small}"></p>')
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"),
                                   externalize=externalize)

    assert backup.write_now()
    record = _record_of(backup)

    assert seen == [big], "only the payload worth moving is handed over"
    assert big not in record["content"]
    assert small in record["content"], "rewriting a tiny URI costs more than it saves"
    assert f'src="{ASSET_KEY}"' in record["content"]
    assert record["assets"] == [ASSET_KEY]


def test_a_refused_externalization_leaves_the_uri_in_place(tmp_path):
    # The asset layer can decline (unsupported format, full disk). The
    # snapshot must still carry the image rather than lose it.
    big = "data:image/png;base64," + "A" * (recovery.MIN_EXTERNALIZE_BYTES + 64)
    ed = HtmlEditor()
    ed.setHtml(f'<p><img src="{big}"></p>')
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"),
                                   externalize=lambda uri: None)

    assert backup.write_now()

    assert big in _record_of(backup)["content"]


# --------------------------------------------------------------------------- #
# An oversized snapshot never replaces a good one
# --------------------------------------------------------------------------- #

def test_an_oversize_snapshot_keeps_the_previous_backup(tmp_path, monkeypatch):
    # Truncated HTML is corrupt HTML, so the whole write is skipped.
    ed = _editor("first version")
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"))
    assert backup.write_now()
    with open(backup.path, "rb") as f:
        original = f.read()

    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 10)
    ed.setPlainText("a considerably longer second version")

    assert backup.write_now() is False
    with open(backup.path, "rb") as f:
        assert f.read() == original


def test_an_oversize_snapshot_waits_before_trying_again(tmp_path, monkeypatch):
    ed = _editor("first version")
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"))
    assert backup.write_now()
    with open(backup.path, "rb") as f:
        original = f.read()

    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 10)
    ed.setPlainText("a considerably longer second version")
    assert backup.write_now() is False

    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 32 * 1024 * 1024)
    backup._on_timeout()

    with open(backup.path, "rb") as f:
        assert f.read() == original, "the cooldown keeps a huge document off the tick"


# --------------------------------------------------------------------------- #
# Older and newer record formats
# --------------------------------------------------------------------------- #

def test_a_version_less_record_restores_as_plain_text(tmp_path):
    # Version 1 stored plain text. Restoring it as HTML would render the
    # user's own angle brackets as markup.
    path = tmp_path / "old.autosave"
    path.write_text("{}", encoding="utf-8")
    record = {"original_path": None, "content": "a <b> tag typed by hand",
              "_backup_file": str(path)}
    win = _window_stub()

    assert MainWindow._restore_one_backup(win, record)

    assert win.editors[-1].toPlainText() == "a <b> tag typed by hand"


def test_a_forward_version_record_is_left_on_disk_and_skipped(tmp_path):
    path = tmp_path / "future.autosave"
    payload = json.dumps({"version": 99, "format": "html",
                          "content": "<p>from a newer build</p>",
                          "original_path": None})
    path.write_text(payload, encoding="utf-8")
    record = dict(json.loads(payload), _backup_file=str(path))
    win = _window_stub()

    assert MainWindow._restore_one_backup(win, record) is False

    assert path.read_text(encoding="utf-8") == payload
    assert win.tabs.titles == []


def test_an_unknown_format_is_left_on_disk_and_skipped(tmp_path):
    path = tmp_path / "odd.autosave"
    payload = json.dumps({"version": 2, "format": "rtf", "content": "{\\rtf1}",
                          "original_path": None})
    path.write_text(payload, encoding="utf-8")
    record = dict(json.loads(payload), _backup_file=str(path))
    win = _window_stub()

    assert MainWindow._restore_one_backup(win, record) is False

    assert path.read_text(encoding="utf-8") == payload


# --------------------------------------------------------------------------- #
# classify_backup decides whether a restore may keep its path
# --------------------------------------------------------------------------- #

def test_classify_backup_covers_every_branch(tmp_path):
    existing = tmp_path / "note.html"
    existing.write_text("on disk", encoding="utf-8")
    mtime = os.stat(existing).st_mtime_ns
    missing = str(tmp_path / "gone.html")

    assert recovery.classify_backup({}) == "untitled"
    assert recovery.classify_backup({"original_path": None}) == "untitled"
    assert recovery.classify_backup(
        {"original_path": missing, "source_mtime_ns": 1}) == "fresh"
    assert recovery.classify_backup(
        {"original_path": str(existing), "source_mtime_ns": mtime}) == "fresh"
    assert recovery.classify_backup(
        {"original_path": str(existing), "source_mtime_ns": mtime - 1}) == "stale"
    # A version 1 record carries no mtime, so it cannot prove it is
    # fresh. Being wrong this way costs one Save As; the other way
    # overwrites newer work.
    assert recovery.classify_backup({"original_path": str(existing)}) == "stale"
    assert recovery.classify_backup(
        {"original_path": str(existing), "source_mtime_ns": True}) == "stale"


def test_a_stale_restore_cannot_overwrite_the_newer_file(tmp_path):
    source = tmp_path / "note.html"
    source.write_text("<p>newer work</p>", encoding="utf-8")
    backup = recovery.EditorBackup(_editor("older edit"), str(source))
    assert backup.write_now()
    record = recovery.find_all_backups()[0]
    record["source_mtime_ns"] = 0
    win = _window_stub()

    assert MainWindow._restore_one_backup(win, record)

    assert win.editors[-1]._file_path is None, "Ctrl+S has to go through Save As"
    assert win.tabs.titles == ["note.html (recovered copy)*"]
