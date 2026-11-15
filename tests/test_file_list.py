# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the file list: grouped by the day files changed, notes named
without their extension and showing their first line, search and the
scope filter, what clicks and keys say, the menu by kind, renaming
that keeps an extension unless the person means it, drags out as real
files and drops in as copies, and that the list follows the disk.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication, QDateTime, QEventLoop, QMimeData, QPointF, Qt, QUrl  # noqa: E402,E501
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QMenu  # noqa: E402

import file_list  # noqa: E402
import library_actions  # noqa: E402
from file_list import FileListPane, date_group, note_snippet, plain_line  # noqa: E402
from library_set import LibrarySet  # noqa: E402
from tests.accessibility import unnamed_controls  # noqa: E402
from tests.waiting import wait_until  # noqa: E402

DAY = 24 * 3600


def write(path, text="", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def noon_today():
    now = QDateTime.currentDateTime()
    return float(QDateTime(now.date(), now.time().fromString("12:00", "HH:mm")).toSecsSinceEpoch())


@pytest.fixture
def libraries():
    made = LibrarySet()
    yield made
    for root in made.roots():
        made.remove_root(root)
    made.deleteLater()
    QCoreApplication.processEvents()


@pytest.fixture
def pane(libraries):
    widget = FileListPane(libraries, is_dark=False)
    widget.resize(320, 700)
    widget.show()
    yield widget
    widget.shutdown()
    widget.close()
    widget.deleteLater()
    QCoreApplication.processEvents()


def titles(pane):
    return [r.title if r.is_header else pane.model_.data(pane.model_.index(i))
            for i, r in enumerate(pane.model_.rows())]


def index_of(pane, path):
    return pane.model_.index(pane.model_.row_of(str(path)))


def send_drop(pane, mime):
    point = QPointF(pane.view.viewport().rect().center())
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    for kind in (QDragEnterEvent, QDragMoveEvent):
        QCoreApplication.sendEvent(pane.view.viewport(), kind(
            point.toPoint(), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    highlighted = pane._drop_highlight
    QCoreApplication.sendEvent(pane.view.viewport(), QDropEvent(
        point, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    return highlighted


# -- reading -----------------------------------------------------------------------

def test_a_notes_first_line_leaves_out_front_matter_markdown_and_its_title():
    assert plain_line("## A **bold** [link](x.md) and `code`") == "A bold link and code"
    assert plain_line("- [x] done ![pic](a.png)") == "done pic"


def test_a_note_snippet_reads_past_front_matter(tmp_path):
    note = write(tmp_path / "Plan.md", "---\ntitle: Plan\ntags: [a]\n---\n\n# Plan\n\n"
                                       "```\ncode\n```\nThe *first* real line.\nSecond.\n")
    assert note_snippet(str(note), "Plan") == "The first real line."
    assert note_snippet(str(tmp_path / "missing.md")) == ""


def test_files_are_grouped_by_day_as_apple_notes_groups_them():
    now = noon_today()
    assert date_group(now - 60, now)[1] == "Today"
    assert date_group(now - DAY, now)[1] == "Yesterday"
    assert date_group(now - 5 * DAY, now)[1] == "Previous 7 Days"
    assert date_group(now - 20 * DAY, now)[1] == "Previous 30 Days"
    old = QDateTime.fromSecsSinceEpoch(int(now)).addYears(-2)
    assert date_group(old.toSecsSinceEpoch(), now)[1] == str(old.date().year())


# -- the list ------------------------------------------------------------------------

def test_a_folder_lists_its_files_newest_first_under_day_headers(tmp_path, libraries, pane):
    now = noon_today()
    root = tmp_path / "root"
    write(root / "Today.md", "hello", now - 60)
    write(root / "yesterday.png", "", now - DAY)
    write(root / "week.pdf", "", now - 4 * DAY)
    write(root / "month.txt", "", now - 25 * DAY)
    (root / "Sub").mkdir()
    libraries.add_root(str(root))
    pane._now = now
    pane.show_folder(str(root))
    # A sidebar folder lists its subfolders too, as rows to open (the
    # sidebar may show it closed), and counts its files as the sidebar does.
    assert titles(pane) == ["Folders", "Sub", "Today", "Today", "Yesterday", "yesterday.png",
                            "Previous 7 Days", "week.pdf", "Previous 30 Days", "month.txt"]
    assert pane.title_label.text() == "root"
    assert pane.subtitle_label.text() == "4 files"
    assert pane.current_folder() == str(root)


def test_a_folder_below_a_sidebar_folder_lists_its_subfolders_first(tmp_path, libraries, pane):
    root = tmp_path / "root"
    write(root / "Sub" / "note.md")
    (root / "Sub" / "Deeper").mkdir()
    libraries.add_root(str(root))
    pane.show_folder(str(root / "Sub"))
    assert titles(pane)[:2] == ["Folders", "Deeper"]
    opened = []
    pane.folder_opened.connect(opened.append)
    pane.activate(str(root / "Sub" / "Deeper"))
    assert opened == [str(root / "Sub" / "Deeper")]
    assert pane.current_folder() == str(root / "Sub" / "Deeper")


def test_a_library_section_spans_folders_and_names_each_rows_folder(tmp_path, libraries, pane):
    write(tmp_path / "a" / "One.md", "first line")
    write(tmp_path / "b" / "Inner" / "Two.markdown")
    write(tmp_path / "b" / "pic.png")
    libraries.add_root(str(tmp_path / "a"))
    libraries.add_root(str(tmp_path / "b"))
    assert wait_until(lambda: not libraries.is_indexing())
    pane.show_section("notes")
    QCoreApplication.processEvents()
    assert sorted(t for t in titles(pane) if t not in ("Today",)) == ["One", "Two"]
    assert pane.spans_folders()
    assert pane.current_folder() is None
    assert pane.title_label.text() == "Notes"
    one = pane.model_.row(pane.model_.row_of(str(tmp_path / "a" / "One.md"))).entry
    pane.fact(one)
    assert wait_until(lambda: pane.fact(one) == "first line")
    spoken = pane.model_.data(index_of(pane, tmp_path / "a" / "One.md"),
                              Qt.ItemDataRole.AccessibleTextRole)
    assert spoken.startswith("One, ") and spoken.endswith("first line, a")


def test_search_finds_names_and_words_inside_notes(tmp_path, libraries, pane):
    root = tmp_path / "root"
    write(root / "Harbour.md", "boats and gulls")
    write(root / "Garden.md", "roses")
    write(root / "harbour.jpg")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    pane.search.setText("harb")
    assert sorted(e.name for e in pane.model_.entries()) == ["Harbour.md", "harbour.jpg"]
    pane.search.setText("gulls")
    assert wait_until(lambda: [e.name for e in pane.model_.entries()] == ["Harbour.md"])
    pane.search.setText("nothing like this")
    assert pane.model_.entries() == []
    assert pane.empty_label.isVisible()
    assert "nothing like this" in pane.empty_label.text()


def test_the_scope_filter_keeps_one_kind_of_file(tmp_path, libraries, pane):
    root = tmp_path / "root"
    for name in ("a.md", "b.txt", "c.png", "d.mp3", "e.pdf", "f.xlsx"):
        write(root / name)
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    pane.set_scope("notes")
    assert sorted(e.name for e in pane.model_.entries()) == ["a.md", "b.txt"]
    pane.set_scope("media")
    assert sorted(e.name for e in pane.model_.entries()) == ["c.png", "d.mp3"]
    pane.set_scope("files")
    assert sorted(e.name for e in pane.model_.entries()) == ["e.pdf", "f.xlsx"]
    assert pane.subtitle_label.text() == "2 files"
    pane.scope.segments[0].click()
    assert len(pane.model_.entries()) == 6


def test_a_picture_shows_its_pixel_size_and_a_thumbnail(tmp_path, libraries, pane):
    root = tmp_path / "root"
    root.mkdir()
    image = QImage(640, 480, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkGreen)
    image.save(str(root / "photo.png"))
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    entry = pane.model_.entries()[0]
    pane.fact(entry)
    assert wait_until(lambda: pane.fact(entry) == "640 × 480")
    pane.thumbnail(entry)
    assert wait_until(lambda: pane.thumbnail(entry) is not None)
    assert min(pane.thumbnail(entry).width(), pane.thumbnail(entry).height()) == 200


def test_a_package_in_a_folder_shows_its_size_measured_off_the_gui_thread(tmp_path, libraries,
                                                                          pane):
    root = tmp_path / "root"
    (root / "Talk.key" / "Data").mkdir(parents=True)
    (root / "Talk.key" / "Data" / "slide.bin").write_bytes(b"x" * 2048)
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    entry = pane.model_.entries()[0]
    assert entry.name == "Talk.key"
    pane.fact(entry)
    assert wait_until(lambda: pane.fact(entry) == "2 KB")


def test_the_gallery_shows_the_same_files_without_headers(tmp_path, libraries, pane):
    root = tmp_path / "root"
    for name in ("a.md", "b.pdf", "c.xlsx", "d.zip", "e.mov"):
        write(root / name)
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    pane.gallery_button.click()
    assert pane.view_mode() == "gallery"
    assert all(not r.is_header for r in pane.model_.rows())
    assert len(pane.model_.rows()) == 5
    assert not pane.grab().isNull()
    pane.list_button.click()
    assert pane.view_mode() == "list"
    assert pane.model_.rows()[0].is_header


# -- signals -------------------------------------------------------------------------

def test_clicks_and_keys_say_what_the_person_wants(tmp_path, libraries, pane):
    root = tmp_path / "root"
    note = write(root / "Note.md")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    said = []
    pane.file_selected.connect(lambda p: said.append(("selected", p)))
    pane.file_activated.connect(lambda p: said.append(("activated", p)))
    pane.quick_look_requested.connect(lambda p: said.append(("quick look", p)))
    rect = pane.view.visualRect(index_of(pane, note))
    QTest.mouseClick(pane.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    QTest.mouseDClick(pane.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    pane.view.setFocus()
    QTest.keyClick(pane.view, Qt.Key.Key_Space)
    QTest.keyClick(pane.view, Qt.Key.Key_Return)
    assert said == [("selected", str(note)), ("activated", str(note)),
                    ("quick look", str(note)), ("activated", str(note))]


def test_new_note_asks_the_host_for_a_note_in_the_folder_shown(tmp_path, libraries, pane):
    (tmp_path / "root").mkdir()
    libraries.add_root(str(tmp_path / "root"))
    pane.show_folder(str(tmp_path / "root"))
    asked = []
    pane.new_note_requested.connect(asked.append)
    pane.new_note_button.click()
    assert asked == [str(tmp_path / "root")]


def test_select_path_chooses_quietly_and_goes_to_the_folder(tmp_path, libraries, pane):
    root = tmp_path / "root"
    note = write(root / "Sub" / "Note.md")
    libraries.add_root(str(root))
    said = []
    pane.file_selected.connect(said.append)
    assert pane.select_path(str(note))
    assert pane.current_folder() == str(root / "Sub")
    assert pane.selected_paths() == [str(note)]
    assert said == []
    assert not pane.select_path(str(tmp_path / "elsewhere" / "x.md"))


# -- the menu ------------------------------------------------------------------------

def menu_names(pane, path):
    return [a.objectName() for a in pane.menu_for([str(path)]).actions() if a.objectName()]


def test_the_menu_offers_what_each_kind_can_do(tmp_path, libraries, pane, monkeypatch):
    import default_apps
    monkeypatch.setattr(default_apps, "default_app_name", lambda path: "Preview")
    root = tmp_path / "root"
    note, pic, doc = write(root / "a.md"), write(root / "b.png"), write(root / "c.docx")
    libraries.add_root(str(root))
    assert "insert_into_note" not in menu_names(pane, note)
    assert "open_in_app" not in menu_names(pane, note)
    assert {"open", "quick_look", "open_in_app", "insert_into_note", "duplicate", "move_to",
            "reveal", "copy_path", "rename", "trash"} <= set(menu_names(pane, pic))
    assert "import_as_note" in menu_names(pane, doc)
    menu = pane.menu_for([str(pic)])
    assert any(a.text() == "Open in Preview" for a in menu.actions())
    asked = []
    pane.insert_into_note_requested.connect(asked.append)
    next(a for a in menu.actions() if a.objectName() == "insert_into_note").trigger()
    assert asked == [str(pic)]


def test_open_with_marks_the_default_app_as_a_viewer_does(tmp_path, libraries, pane,
                                                          monkeypatch):
    import default_apps
    choices = [default_apps.AppChoice("Preview", "/Applications/Preview.app"),
               default_apps.AppChoice("Pixelmator", "/Applications/Pixelmator.app")]
    monkeypatch.setattr(default_apps, "default_app_name", lambda path: "Preview")
    monkeypatch.setattr(default_apps, "other_apps", lambda path: choices)
    root = tmp_path / "root"
    pic = write(root / "b.png")
    libraries.add_root(str(root))
    menu = pane.menu_for([str(pic)])
    submenu = menu.findChild(QMenu, "open_with")
    submenu.aboutToShow.emit()
    words = [a.text() for a in submenu.actions() if not a.isSeparator()]
    assert words == ["Preview (default)", "Pixelmator", "Other…"]


def test_renaming_a_note_keeps_its_extension_and_links_follow(tmp_path, libraries, pane,
                                                              monkeypatch):
    root = tmp_path / "root"
    note = write(root / "Plan.md")
    index = write(root / "Index.md", "[plan](Plan.md)\n")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    monkeypatch.setattr(library_actions, "ask_name", lambda parent, title, label, text, select: (
        "Plan v2" if text == "Plan" else None))
    said = []
    pane.status_message.connect(said.append)
    assert pane.rename_path(str(note)) == str(root / "Plan v2.md")
    assert index.read_text(encoding="utf-8") == "[plan](<Plan v2.md>)\n"
    assert said == ["Updated links in 1 note."]
    assert pane.selected_paths() == [str(root / "Plan v2.md")]


def test_changing_an_extension_is_asked_about_and_kept_by_default(tmp_path, libraries, pane,
                                                                  monkeypatch):
    root = tmp_path / "root"
    pic = write(root / "photo.png")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    asked = []
    monkeypatch.setattr(library_actions, "ask_name", lambda *a: "photo.jpg")
    monkeypatch.setattr(library_actions, "ask_extension_change",
                        lambda parent, old, new: asked.append((old, new)) or False)
    assert pane.rename_path(str(pic)) == str(root / "photo.jpg.png")
    assert asked == [(".png", ".jpg")]
    monkeypatch.setattr(library_actions, "ask_name", lambda *a: "beach.png")
    assert pane.rename_path(str(root / "photo.jpg.png")) == str(root / "beach.png")
    assert len(asked) == 1


def test_trash_asks_first_and_answers_the_trash_keys(tmp_path, libraries, pane, monkeypatch):
    import send2trash
    root = tmp_path / "root"
    note = write(root / "Note.md")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    trashed, asked = [], []
    monkeypatch.setattr(send2trash, "send2trash", trashed.append)
    monkeypatch.setattr(library_actions, "confirm_trash",
                        lambda parent, paths: asked.append(paths) or True)
    pane.select_path(str(note))
    pane.view.setFocus()
    QTest.keyClick(pane.view, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
    assert asked == [[str(note)]]
    assert trashed == [str(note)]


def test_duplicate_makes_a_finder_named_copy_and_chooses_it(tmp_path, libraries, pane):
    root = tmp_path / "root"
    pic = write(root / "pic.png", "x")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    assert pane.duplicate_paths([str(pic)]) == [str(root / "pic 2.png")]
    assert pane.selected_paths() == [str(root / "pic 2.png")]


# -- drags and drops -------------------------------------------------------------------

def test_a_drag_carries_the_real_files_and_the_apps_own_paths(tmp_path):
    paths = [str(tmp_path / "a.png"), str(tmp_path / "b c.md")]
    mime = library_actions.mime_for(paths)
    # The files themselves, as URLs (Qt spells a Windows path in one with
    # "/"; the app's own spelling comes back from dropped).
    assert mime.urls() == [QUrl.fromLocalFile(p) for p in paths]
    assert library_actions.dropped(mime) == (paths, [])
    finder = QMimeData()
    finder.setUrls([QUrl.fromLocalFile(paths[0]), QUrl("https://example.com")])
    assert library_actions.dropped(finder) == ([], [paths[0]])


def test_files_dropped_from_elsewhere_are_copied_into_the_folder(tmp_path, libraries, pane):
    outside = write(tmp_path / "outside" / "photo.jpg", "x")
    (tmp_path / "root").mkdir()
    libraries.add_root(str(tmp_path / "root"))
    pane.show_folder(str(tmp_path / "root"))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(outside))])
    assert send_drop(pane, mime)
    assert not pane._drop_highlight
    assert (tmp_path / "root" / "photo.jpg").exists()
    assert outside.exists()
    assert pane.selected_paths() == [str(tmp_path / "root" / "photo.jpg")]


def test_files_dragged_from_another_folder_move_and_links_follow(tmp_path, libraries, pane):
    root = tmp_path / "root"
    pic = write(root / "Old" / "pic.png")
    note = write(root / "Note.md", "![](Old/pic.png)\n")
    (root / "New").mkdir()
    libraries.add_root(str(root))
    pane.show_folder(str(root / "New"))
    send_drop(pane, library_actions.mime_for([str(pic)]))
    assert (root / "New" / "pic.png").exists()
    assert not pic.exists()
    assert note.read_text(encoding="utf-8") == "![](New/pic.png)\n"


def test_files_already_in_the_folder_are_not_dropped_again(tmp_path, libraries, pane):
    root = tmp_path / "root"
    pic = write(root / "pic.png")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    assert not send_drop(pane, library_actions.mime_for([str(pic)]))
    assert os.listdir(root) == ["pic.png"]


# -- following the disk ------------------------------------------------------------------

def test_the_list_follows_changes_and_keeps_the_choice(tmp_path, libraries, pane):
    root = tmp_path / "root"
    note = write(root / "Note.md")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    pane.select_path(str(note))
    libraries.new_note(str(root))
    assert wait_until(lambda: len(pane.model_.entries()) == 2)
    assert pane.selected_paths() == [str(note)]
    write(root / "from elsewhere.txt")
    libraries.refresh()
    assert wait_until(lambda: len(pane.model_.entries()) == 3)
    assert pane.selected_paths() == [str(note)]


def test_the_folder_on_screen_is_watched(tmp_path, libraries, pane):
    root = tmp_path / "root"
    (root / "Sub").mkdir(parents=True)
    libraries.add_root(str(root))
    pane.show_folder(str(root / "Sub"))
    assert str(root / "Sub") in libraries.libraries()[0]._watcher.directories()


# -- size and names -----------------------------------------------------------------------

def test_widths_theme_and_names(tmp_path, libraries, pane):
    assert (FileListPane.MIN_WIDTH, FileListPane.DEFAULT_WIDTH,
            FileListPane.MAX_WIDTH) == (240, 300, 480)
    # Never narrower than MIN_WIDTH, nor than the scope's labels side by side.
    labels = sum(s.label_width() for s in pane.scope.segments) + 4
    assert pane.minimumWidth() == max(240, labels + 24) and pane.maximumWidth() == 480
    write(tmp_path / "root" / "a.md")
    libraries.add_root(str(tmp_path / "root"))
    pane.show_folder(str(tmp_path / "root"))
    pane.update_theme(True)
    assert not pane.grab().isNull()
    assert unnamed_controls(pane) == []


def test_many_files_list_without_reading_them(tmp_path, libraries, pane, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    for number in range(2000):
        (root / f"note {number}.md").write_text("x", encoding="utf-8")
    libraries.add_root(str(root))
    read = []
    monkeypatch.setattr(file_list, "note_snippet", lambda path, title="": read.append(path) or "")
    pane.show_folder(str(root))
    wait_until(lambda: False, 0.3)            # the list lays its rows out
    pane.grab()
    assert wait_until(lambda: not pane.reader.busy())
    assert len(pane.model_.entries()) == 2000
    assert wait_until(lambda: len(read) > 0)  # the visible rows, when the list has drawn them
    assert len(read) < 50


# -- the red team's second round -----------------------------------------------------

def test_the_scope_labels_are_whole_at_the_lists_narrowest_also_in_german(libraries):
    import i18n
    i18n.install("de")
    try:
        widget = FileListPane(libraries, is_dark=False)
        widget.resize(widget.minimumWidth(), 600)
        widget.show()
        QCoreApplication.processEvents()
        texts = [s.text() for s in widget.scope.segments]
        assert texts[-1] == "Dokumente"                 # the sidebar's word
        assert [s.painted_text() for s in widget.scope.segments] == texts, widget.width()
        widget.shutdown()
        widget.deleteLater()
    finally:
        i18n.install(i18n.ENGLISH)


def test_the_scope_names_documents_as_the_sidebar_does(pane):
    assert [s.text() for s in pane.scope.segments] == ["All", "Notes", "Media", "Documents"]


def test_tab_goes_from_the_heads_buttons_to_search_then_the_scope(pane):
    order, widget = [], pane.list_button
    for _step in range(12):
        order.append(widget)
        widget = widget.nextInFocusChain()
        while not (widget.focusPolicy() & Qt.FocusPolicy.TabFocus) or not widget.isVisible():
            widget = widget.nextInFocusChain()
    chosen = next(s for s in pane.scope.segments if s.isChecked())
    position = {id(w): i for i, w in reversed(list(enumerate(order)))}
    assert (position[id(pane.new_note_button)] < position[id(pane.search)]
            < position[id(chosen)] < position[id(pane.view)])


def test_an_empty_folder_says_so_and_offers_a_new_note(tmp_path, libraries, pane):
    root = tmp_path / "root"
    (root / "Empty").mkdir(parents=True)
    write(root / "a.md")
    libraries.add_root(str(root))
    pane.show_folder(str(root / "Empty"))
    QCoreApplication.processEvents()
    assert pane.empty_label.text() == "This folder is empty."
    assert pane.empty_new_note.isVisible()
    assert pane.empty_new_note.accessibleName() == "New Note"
    asked = []
    pane.new_note_requested.connect(asked.append)
    pane.empty_new_note.click()
    assert asked == [str(root / "Empty")]


def test_a_scope_that_hides_everything_and_a_search_say_what_happened(tmp_path, libraries,
                                                                      pane):
    root = tmp_path / "root"
    write(root / "a.md")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    pane.set_scope("media")
    assert pane.empty_label.text() == "Nothing of this kind is here."
    assert not pane.empty_new_note.isVisible()
    pane.set_scope("all")
    pane.search.setText("zebra")
    pane._apply()
    assert pane.empty_label.text() == "No results for “zebra”."
    assert not pane.empty_new_note.isVisible()


def test_the_gallery_keeps_its_columns_whether_a_scroll_bar_shows_or_not(tmp_path, libraries,
                                                                         pane):
    """Qt lays an icon grid out leaving room for the scroll bar that may
    come; the columns are measured the same way, or the second one
    folds under the first."""
    root = tmp_path / "root"
    for number in range(3):
        write(root / f"n{number}.md")
    libraries.add_root(str(root))
    pane.resize(300, 700)
    pane.show_folder(str(root))
    pane.set_view_mode("gallery")
    for _ in range(5):
        QCoreApplication.processEvents()
    assert not pane.view.verticalScrollBar().isVisible()
    first = pane.view.visualRect(pane.model_.index(0))
    second = pane.view.visualRect(pane.model_.index(1))
    assert second.top() == first.top() and second.left() > first.left()


def test_a_sidebar_folder_shows_its_subfolders_even_when_the_sidebar_shows_it_closed(
        tmp_path, libraries, pane):
    root = tmp_path / "root"
    (root / "Drafts").mkdir(parents=True)
    write(root / "a.md")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    assert [e.name for e in pane.model_.entries() if e.is_dir] == ["Drafts"]


def test_a_search_over_more_notes_than_the_cache_holds_finds_them_all_reading_each_once(
        tmp_path, libraries, pane, monkeypatch):
    monkeypatch.setattr(file_list, "CACHE_LIMIT", 15)       # more than the rows on screen
    monkeypatch.setattr(file_list, "SEARCH_READ_LIMIT", 4)
    jobs = []
    workers = pane.reader._workers
    real_run = workers.run

    def counting(work, done=None, failed=None, **options):
        jobs.append(work.args[0])          # the key, as the read is handed over
        real_run(work, done, failed, **options)

    monkeypatch.setattr(workers, "run", counting)
    root = tmp_path / "root"
    for number in range(40):
        write(root / f"Note {number:02}.md", f"About the harbour, part {number}\n")
    libraries.add_root(str(root))
    pane.show_folder(str(root))
    jobs.clear()
    pane.search.setText("harbour")
    assert wait_until(lambda: len(pane.model_.entries()) == 40, 10)
    for _ in range(20):
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    # Every note read, and then quiet: nothing is read again and again.
    snippets = [k for k in jobs if k[0] == "snippet"]
    assert len(set(snippets)) == 40
    settled = len(jobs)
    for _ in range(30):
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    assert len(jobs) == settled
