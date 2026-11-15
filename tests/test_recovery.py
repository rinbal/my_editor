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
- Save As over the same path removing the live backup,
- a quote, a code block or inline code coming back from a crash as
  plain paragraphs (HTML cannot hold them; the Markdown kept beside it can).
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

    stub = types.SimpleNamespace(
        _new_wired_editor=new_editor,
        _new_backup=lambda ed, path: recovery.EditorBackup(ed, path),
        _reload_from_disk=lambda *a: None,
        _attach_close_button=lambda *a: None,
        _attach_highlighter=lambda *a: None,
        _remember_split=lambda *a: None,
        _width_changed=lambda *a: None,
        _asset_manager=types.SimpleNamespace(adopt_data_uri=lambda uri: None),
        _watcher=types.SimpleNamespace(addPath=lambda p: None),
        is_dark_theme=False,
        show_line_numbers=False,
        tabs=_Tabs(),
        editors=editors,
    )
    stub._editor_container = lambda ed: MainWindow._editor_container(stub, ed)
    return stub


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


def test_a_record_spelling_its_file_another_way_restores_under_the_one_spelling(tmp_path):
    # An older version kept paths as they arrived: with Qt's forward
    # slashes on Windows, or with steps back. The tab and its new backup
    # take the one spelling (file_paths), so the file cannot open twice.
    source = tmp_path / "note.html"
    source.write_text("<p>on disk</p>", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    spelled = (tmp_path / "sub").as_posix() + "/../note.html"
    assert recovery.EditorBackup(_editor("unsaved edit"), spelled).write_now()
    record = recovery.find_all_backups()[0]

    restored = MainWindow._restore_one_backup(_window_stub(), record)

    assert restored._file_path == str(source)
    assert [r["original_path"] for r in recovery.find_all_backups()] == [str(source)]


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

    assert MainWindow._restore_one_backup(win, record) is None

    assert path.read_text(encoding="utf-8") == payload
    assert win.tabs.titles == []


def test_an_unknown_format_is_left_on_disk_and_skipped(tmp_path):
    path = tmp_path / "odd.autosave"
    payload = json.dumps({"version": 2, "format": "rtf", "content": "{\\rtf1}",
                          "original_path": None})
    path.write_text(payload, encoding="utf-8")
    record = dict(json.loads(payload), _backup_file=str(path))
    win = _window_stub()

    assert MainWindow._restore_one_backup(win, record) is None

    assert path.read_text(encoding="utf-8") == payload


# --------------------------------------------------------------------------- #
# The helpers crash recovery and the update restart share
# --------------------------------------------------------------------------- #

def test_restoring_returns_the_editor_it_opened(tmp_path):
    path = tmp_path / "old.autosave"
    path.write_text("{}", encoding="utf-8")
    record = {"original_path": None, "content": "x", "_backup_file": str(path)}
    win = _window_stub()
    assert MainWindow._restore_one_backup(win, record) is win.editors[-1]


def test_only_known_formats_are_restorable():
    assert recovery.is_restorable({})                                   # version 1, plain text
    assert recovery.is_restorable({"version": 2, "format": "html"})
    assert not recovery.is_restorable({"version": 3, "format": "html"})  # a newer build
    assert not recovery.is_restorable({"version": 2, "format": "rtf"})
    assert not recovery.is_restorable({"version": True, "format": "html"})
    assert not recovery.is_restorable({"version": "2", "format": "html"})


def test_loading_a_backup_leaves_no_undo_history_and_the_given_modified_flag():
    ed = HtmlEditor()
    recovery.load_backup_content(ed, {"version": 2, "format": "html",
                                      "content": "<p>kept</p>"}, modified=False)
    assert ed.toPlainText() == "kept"
    assert not ed.document().isModified()
    assert not ed.document().isUndoAvailable()

    recovery.load_backup_content(ed, {"content": "a <b> tag"})   # version 1: plain text
    assert ed.toPlainText() == "a <b> tag"
    assert ed.document().isModified()


def test_taking_over_writes_the_replacement_before_removing_the_old_record(tmp_path):
    old = tmp_path / "old.autosave"
    old.write_text("{}", encoding="utf-8")
    backup = recovery.EditorBackup(_editor("restored"), None)
    assert backup.take_over(str(old))
    assert not old.exists()
    assert _record_of(backup)["content"]


def test_taking_over_keeps_the_old_record_when_nothing_could_be_written(tmp_path):
    old = tmp_path / "old.autosave"
    old.write_text("{}", encoding="utf-8")
    backup = recovery.EditorBackup(HtmlEditor(), None)   # empty: nothing to write
    assert backup.take_over(str(old)) is False
    assert old.exists()


def test_taking_over_its_own_file_keeps_it(tmp_path):
    backup = recovery.EditorBackup(_editor("restored"), str(tmp_path / "note.html"))
    assert backup.write_now()
    assert backup.take_over(backup.path)
    assert os.path.exists(backup.path)


def test_an_empty_document_is_empty_and_an_image_is_not():
    assert recovery.document_is_empty(HtmlEditor().document())
    assert not recovery.document_is_empty(_editor("x").document())
    assert not recovery.document_is_empty(_editor_with_image(text="").document())


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


def test_a_backup_that_cannot_be_finished_leaves_the_previous_one_whole(tmp_path, monkeypatch):
    # The moment a backup matters most is a crash, which is also when its
    # own write may be cut short. The previous backup must survive that.
    import atomic_file

    ed = _editor("first version")
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"))
    assert backup.write_now()
    before = _record_of(backup)["content"]

    ed.setHtml("<p>second version</p>")
    monkeypatch.setattr(atomic_file, "_replace",
                        lambda *_: (_ for _ in ()).throw(OSError(5, "I/O error")))
    assert backup.write_now() is False
    assert _record_of(backup)["content"] == before
    assert not [n for n in os.listdir(os.path.dirname(backup.path)) if n.endswith(".tmp")]


# --------------------------------------------------------------------------- #
# A crash gives back exactly what was typed, structure included
# --------------------------------------------------------------------------- #

def _markdown_editor(markdown: str) -> HtmlEditor:
    from markdown_writer import read_markdown
    ed = HtmlEditor()
    read_markdown(ed.document(), markdown)
    return ed


def _restored(backup) -> HtmlEditor:
    restored = HtmlEditor()
    recovery.load_backup_content(restored, _record_of(backup))
    return restored


STRUCTURED = "Intro with `code`.\n\n> A quote\n\n```python\nprint(1)\n```\n"


def test_quotes_and_code_survive_backup_and_restore(tmp_path):
    from markdown_writer import document_to_markdown
    backup = recovery.EditorBackup(_markdown_editor(STRUCTURED), None)
    assert backup.write_now()
    record = _record_of(backup)
    assert record["structure"]["blocks"] and record["structure"]["code"]
    assert record["format"] == "html" and record["content"]   # older builds read this
    assert document_to_markdown(_restored(backup).document()) == STRUCTURED


def test_a_document_html_keeps_whole_has_no_structure_list(tmp_path):
    backup = recovery.EditorBackup(_markdown_editor("# Title\n\n- one\n- two\n"), None)
    assert backup.write_now()
    assert "structure" not in _record_of(backup)


def test_colors_and_code_survive_together(tmp_path):
    from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
    from markdown_writer import document_to_markdown
    ed = _markdown_editor(STRUCTURED)
    cursor = QTextCursor(ed.document())
    cursor.movePosition(QTextCursor.MoveOperation.NextWord, QTextCursor.MoveMode.KeepAnchor)
    red = QTextCharFormat()
    red.setForeground(QColor("red"))
    cursor.mergeCharFormat(red)
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    restored = _restored(backup)
    assert document_to_markdown(restored.document()) == STRUCTURED
    first = QTextCursor(restored.document())
    first.setPosition(1)
    assert first.charFormat().foreground().color() == QColor("red")


@pytest.mark.parametrize("typed", [
    "In HTML, <br> breaks a line.",
    "Write &amp; for an ampersand.",
    "A star stays \\* here, and *this* too.",
    "Hidden <!-- note --> words.",
    "    four spaces first",
    "Backticks `around` a word, and <b>tags</b>.",
])
def test_typed_markdown_characters_come_back_as_typed(typed):
    # Review F1: a restore that read the backup as Markdown lost the rest
    # of the document after a typed <br>, and changed entities and escapes.
    from PySide6.QtGui import QTextCursor
    ed = HtmlEditor()
    cursor = ed.textCursor()
    cursor.insertText("Code: ")
    code = rich_text_style_code()
    cursor.insertText("x = 1", code)
    cursor.insertText(" end", code.__class__())
    cursor.insertBlock()
    cursor.insertText(typed)
    cursor.insertBlock()
    cursor.insertText("Second paragraph.")
    cursor.insertBlock()
    cursor.insertText("Third paragraph.")
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    restored = _restored(backup)
    assert restored.toPlainText() == ed.toPlainText()
    probe = QTextCursor(restored.document())
    probe.setPosition(len("Code: ") + 2)
    assert probe.charFormat().fontFixedPitch()          # the inline code is back too


def rich_text_style_code():
    import rich_text
    return rich_text.style_format(rich_text.CODE, True)


def test_a_note_comes_back_without_new_blank_lines():
    from markdown_writer import document_to_note_text
    ed = HtmlEditor()
    ed.setPlainText("first line\nsecond line\nthird line")
    cursor = ed.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(5, cursor.MoveMode.KeepAnchor)
    cursor.mergeCharFormat(rich_text_style_code())
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    assert document_to_note_text(_restored(backup).document()) == document_to_note_text(
        ed.document())


def test_an_html_document_keeps_its_html_formatting():
    # Alignment, sub- and superscript and a merged cell, next to inline
    # code: the HTML keeps them, the structure list adds the code. A size
    # the page set is not kept: the text size is the person's (export_html,
    # _drop_page_fonts), as it is when the file is saved and opened.
    from PySide6.QtGui import QTextCursor
    ed = HtmlEditor()
    ed.setHtml('<p align="center"><span style="font-size:20pt">Big</span> H<sub>2</sub>O '
               'x<sup>2</sup> <code>code</code></p><table border="1"><tr>'
               '<td colspan="2">merged</td></tr><tr><td>a</td><td>b</td></tr></table>')
    probe = QTextCursor(ed.document())
    probe.setPosition(ed.toPlainText().index("code") + 1)
    whole = QTextCursor(ed.document())
    whole.setPosition(ed.toPlainText().index("code"))
    whole.setPosition(ed.toPlainText().index("code") + 4, QTextCursor.MoveMode.KeepAnchor)
    whole.mergeCharFormat(rich_text_style_code())
    backup = recovery.EditorBackup(ed, "/tmp/page.html")
    assert backup.write_now()
    kept = _restored(backup)                     # the editor owns the document
    restored = kept.document()
    first = restored.begin()
    assert first.blockFormat().alignment() & __import__("PySide6.QtCore").QtCore.Qt.AlignHCenter
    sizes = [f.charFormat().fontPointSize() for f in _fragments(first)]
    assert set(sizes) == {0}
    aligns = [f.charFormat().verticalAlignment() for f in _fragments(first)]
    from PySide6.QtGui import QTextCharFormat
    assert QTextCharFormat.VerticalAlignment.AlignSubScript in aligns
    assert QTextCharFormat.VerticalAlignment.AlignSuperScript in aligns
    assert any(f.charFormat().fontFixedPitch() for f in _fragments(first))
    cell = QTextCursor(restored.findBlock(restored.toPlainText().index("merged")))
    assert cell.currentTable().cellAt(cell).columnSpan() == 2


def _fragments(block):
    it = block.begin()
    while not it.atEnd():
        if it.fragment().isValid():
            yield it.fragment()
        it += 1


def test_a_structure_list_for_other_text_is_not_applied():
    from markdown_writer import document_to_markdown
    backup = recovery.EditorBackup(_markdown_editor(STRUCTURED), None)
    assert backup.write_now()
    record = _record_of(backup)
    record["content"] = record["content"].replace("Intro", "Other")
    restored = HtmlEditor()
    recovery.load_backup_content(restored, record)
    assert restored.toPlainText().startswith("Other with code.")
    assert ">" not in document_to_markdown(restored.document())   # no quote put on wrong text


def test_an_older_markdown_record_is_used_only_when_it_gives_back_the_text():
    from markdown_writer import document_to_markdown
    ed = _markdown_editor(STRUCTURED)
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    record = _record_of(backup)
    del record["structure"]
    record["markdown"] = STRUCTURED                    # as an earlier build wrote it
    restored = HtmlEditor()
    recovery.load_backup_content(restored, record)
    assert document_to_markdown(restored.document()) == STRUCTURED
    # Typed text the Markdown would change: the HTML wins.
    typed = HtmlEditor()
    typed.setPlainText("In HTML, <br> breaks a line.\nMore text.")
    backup = recovery.EditorBackup(typed, None)
    assert backup.write_now()
    record = _record_of(backup)
    record["markdown"] = "In HTML, <br> breaks a line.\n\nMore text.\n"
    restored = HtmlEditor()
    recovery.load_backup_content(restored, record)
    assert restored.toPlainText() == "In HTML, <br> breaks a line.\nMore text."


def test_an_idle_tab_is_not_written_again(monkeypatch):
    # Review L1 (early range): every tick rebuilt the snapshot of every
    # open tab, typing or not.
    backup = recovery.EditorBackup(_markdown_editor(STRUCTURED), None)
    assert backup.write_now()
    built = []
    original = backup._snapshot
    monkeypatch.setattr(backup, "_snapshot", lambda doc: built.append(1) or original(doc))
    assert backup.write_now()
    assert built == []
    backup._editor.textCursor().insertText("more")
    assert backup.write_now()
    assert built == [1]


def test_typed_footnotes_survive_a_crash():
    # Review H3: restored from Markdown, "[^2]: Ibid." was read as a link
    # definition and lost.
    from markdown_writer import document_to_markdown
    ed = _markdown_editor("> A quote\n")
    cursor = ed.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    for line in ("A claim[^1] and another[^2].", "[^1]: https://example.com/source",
                 "[^2]: Ibid."):
        cursor.insertBlock(__import__("PySide6.QtGui").QtGui.QTextBlockFormat())
        cursor.insertText(line)
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    restored = _restored(backup)
    assert restored.toPlainText() == ed.toPlainText()
    assert document_to_markdown(restored.document()) == document_to_markdown(ed.document())


def test_a_document_opened_as_markdown_text_stays_that_way(tmp_path):
    ed = HtmlEditor()
    ed.setPlainText("A claim[^1].\n\n[^1]: The source.")
    ed._markdown_source = True
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()

    restored = HtmlEditor()
    recovery.load_backup_content(restored, _record_of(backup))
    assert restored._markdown_source is True
    assert restored.toPlainText() == "A claim[^1].\n\n[^1]: The source."


def test_backups_follow_the_typing_closely():
    assert recovery._DEBOUNCE_MS == 2_000
    assert recovery._MAX_INTERVAL_MS == 15_000



# --------------------------------------------------------------------------- #
# A backup that cannot be kept says so (decision Q-J)
# --------------------------------------------------------------------------- #

def test_a_failed_backup_is_reported_and_so_is_its_recovery(tmp_path, monkeypatch):
    import atomic_file
    ed = _editor("words worth keeping")
    backup = recovery.EditorBackup(ed, str(tmp_path / "note.html"))
    heard = []
    backup.set_status_listener(heard.append)
    assert backup.write_now() and heard == []            # silent while it works
    real = atomic_file._replace
    monkeypatch.setattr(atomic_file, "_replace",
                        lambda *_: (_ for _ in ()).throw(OSError(28, "No space left")))
    ed.setHtml("<p>more words</p>")
    assert backup.write_now() is False
    assert backup.problem == recovery.WRITE_FAILED and heard == [recovery.WRITE_FAILED]
    ed.setHtml("<p>even more</p>")
    backup.write_now()
    assert heard == [recovery.WRITE_FAILED]              # said once, not on every tick
    monkeypatch.setattr(atomic_file, "_replace", real)
    assert backup.write_now()
    assert backup.problem == recovery.KEPT and heard == [recovery.WRITE_FAILED, recovery.KEPT]


def test_a_document_too_large_to_back_up_says_so(monkeypatch):
    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 10)
    ed = _editor("a document longer than ten bytes")
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now() is False
    assert backup.problem == recovery.TOO_LARGE


# -- a document locked with a password (locked_files.py) -----------------------

def test_a_locked_documents_backup_holds_nothing_in_the_clear():
    from locked_files import Lock
    ed = _editor("private words")
    ed._lock = Lock("correct horse")
    backup = recovery.EditorBackup(ed, "/tmp/diary.md")
    assert backup.write_now()
    with open(backup.path, "rb") as f:
        written = f.read()
    assert b"private words" not in written
    record = _record_of(backup)
    assert record["format"] == recovery.SEALED and recovery.is_restorable(record)
    assert record["original_path"] == "/tmp/diary.md"        # where it goes back to
    lock, opened = recovery.unseal_backup(record, "correct horse")
    assert "private words" in opened["content"] and opened["format"] == "html"
    assert lock.matches("correct horse")


def test_a_sealed_backup_needs_the_password_and_older_builds_leave_it():
    from locked_files import Lock, WrongPassword
    ed = _editor("private words")
    ed._lock = Lock("correct horse")
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    record = _record_of(backup)
    with pytest.raises(WrongPassword):
        recovery.unseal_backup(record, "wrong")
    # A build before locking knew the "html" format only: it leaves this be.
    assert record["format"] != "html"


def test_a_locked_documents_pictures_stay_inside_its_sealed_backup():
    from locked_files import Lock
    ed = _editor("words")
    ed._lock = Lock("correct horse")
    handed_out = []
    backup = recovery.EditorBackup(ed, None, externalize=lambda uri: handed_out.append(uri))
    big = "data:image/png;base64," + "A" * (recovery.MIN_EXTERNALIZE_BYTES + 10)
    ed.textCursor().insertHtml(f'<img src="{big}">')
    assert backup.write_now()
    assert handed_out == []


def test_locking_a_document_seals_its_backup_at_once():
    from locked_files import Lock
    ed = _editor("private words")
    backup = recovery.EditorBackup(ed, None)
    assert backup.write_now()
    assert _record_of(backup)["format"] == "html"
    ed._lock = Lock("correct horse")
    assert backup.write_now()                       # no word changed
    assert _record_of(backup)["format"] == recovery.SEALED


# -- photo groups --------------------------------------------------------------------------

GROUPED = ("Before\n\n![a](a.png)\n\n*Under a*\n\n![b](b.png) ![c](c.png)\n\n"
           "![d](plan.png#wide)\n\nAfter, with `code`.\n")


def test_photo_groups_survive_backup_and_restore(tmp_path):
    """Kept as their paragraphs (HTML has no place for a group), back as
    the groups they were, and the Markdown as it was read."""
    import photo_group
    from markdown_writer import _image_as_read, document_to_markdown
    ed = _markdown_editor(GROUPED)
    assert photo_group.has_groups(ed.document())
    backup = recovery.EditorBackup(ed, str(tmp_path / "trip.md"))
    ed.document().setModified(True)
    assert backup.write_now()
    record = _record_of(backup)
    assert record["photo_groups"] is True and "<table" not in record["content"]
    restored = _restored(backup)
    assert photo_group.has_groups(restored.document())
    assert document_to_markdown(restored.document(), _image_as_read) == GROUPED


def test_a_backup_without_groups_says_nothing_of_them(tmp_path):
    ed = _markdown_editor("![a](a.png)\n\nWords\n")
    ed.document().setModified(True)
    backup = recovery.EditorBackup(ed, str(tmp_path / "one.md"))
    assert backup.write_now()
    assert "photo_groups" not in _record_of(backup)
