# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One spelling for each file, however it arrived.

What must hold:

  A path is kept absolute, with the system's own separators: one with
  forward slashes (as Qt's dialogs and drops give them on Windows), with
  ``..`` steps, or relative to the current folder, takes the one
  spelling.

  Two spellings of one file are the same file, and so is another case
  of its name exactly where the disk ignores case; two files are not.
  The disk decides when it gives both files an identity, so two names a
  folder keeps apart stay two files whatever their case. A disk that
  gives no identity (network and cloud drives on Windows report 0)
  never makes two files one.

  A file already open under another spelling is not opened again: its
  tab becomes current and says so. Another file on a disk without
  identities opens in a tab of its own.

  Save As keeps the one spelling, whatever the dialog answered.

  The recent files list holds each file once, however it was spelled.

  Renaming a file to another case of its own name renames it; a name
  another file has is refused, and so is the name of the file a link
  points to, or of a broken link: renaming onto either replaces it.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import file_paths  # noqa: E402
import main_window as main_window_module  # noqa: E402
import recent_files  # noqa: E402
from main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def note(tmp_path):
    (tmp_path / "sub").mkdir()
    path = tmp_path / "note.md"
    path.write_text("text", encoding="utf-8")
    return path


def spellings(note, monkeypatch):
    """Other ways the same file reaches the app."""
    folder = note.parent
    monkeypatch.chdir(folder)
    return [
        note.as_posix(),                                  # Qt's spelling on Windows
        str(folder / "sub" / ".." / note.name),           # with a step back
        (folder / "sub").as_posix() + "/../" + note.name,  # both at once
        note.name,                                        # relative to the current folder
    ]


# -- the spelling ---------------------------------------------------------------------

def test_every_spelling_of_a_file_becomes_the_one_spelling(note, monkeypatch):
    for spelled in spellings(note, monkeypatch):
        assert file_paths.normalize(spelled) == str(note), spelled


def test_the_one_spelling_stays_as_it_is(note):
    assert file_paths.normalize(str(note)) == str(note)
    assert file_paths.normalize("") == ""


def test_spellings_of_one_file_are_the_same_file(note, monkeypatch):
    for spelled in spellings(note, monkeypatch):
        assert file_paths.same_file(spelled, str(note)), spelled


def test_two_files_are_not_the_same_file(note, tmp_path):
    other = tmp_path / "other.md"
    other.write_text("text", encoding="utf-8")
    assert not file_paths.same_file(str(note), str(other))
    assert not file_paths.same_file(str(note), str(tmp_path / "missing.md"))
    assert not file_paths.same_file(str(note), None)
    assert not file_paths.same_file(None, None)


def test_another_case_is_the_same_file_exactly_where_the_disk_ignores_case(note):
    other_case = str(note.parent / note.name.upper())
    disk_ignores_case = os.path.exists(other_case)
    assert file_paths.same_file(str(note), other_case) == disk_ignores_case


# The fields os.stat gives by position and by name; every other st_ field
# is carried over by name when a result is renumbered.
_NAMED_POSITIONS = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size")


def _renumbered(result, number):
    values = list(result)
    values[1], values[2] = number, (7 if number else 0)          # st_ino, st_dev
    extra = {name: getattr(result, name) for name in dir(result)
             if name.startswith("st_") and name not in _NAMED_POSITIONS}
    return os.stat_result(values, extra)


def renumber(monkeypatch, folder, numbers):
    """Let the disk give the files of ``folder`` the file numbers
    ``numbers`` names; 0 is no identity, as network and cloud drives on
    Windows report for every file. Everything else is the real disk."""
    folder = str(folder)
    for name in ("stat", "lstat"):
        def renumbering(path, *args, _real=getattr(os, name), **kwargs):
            result = _real(path, *args, **kwargs)
            if (isinstance(path, (str, os.PathLike))
                    and os.path.dirname(os.path.abspath(path)) == folder):
                return _renumbered(result, numbers(os.path.basename(path)))
            return result
        monkeypatch.setattr(os, name, renumbering)


def without_identities(monkeypatch, folder):
    renumber(monkeypatch, folder, lambda name: 0)


@pytest.fixture
def chapters(tmp_path):
    made = []
    for name in ("chapter1.md", "chapter2.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
        made.append(str(tmp_path / name))
    return made


def test_on_a_disk_without_identities_two_files_stay_two(chapters, monkeypatch):
    without_identities(monkeypatch, os.path.dirname(chapters[0]))
    assert not file_paths.same_file(*chapters)
    assert file_paths.same_file(chapters[0], os.path.join(os.path.dirname(chapters[0]),
                                                          "sub", "..", "chapter1.md"))


def test_names_the_disk_keeps_apart_are_two_files_whatever_their_case(tmp_path, monkeypatch):
    # A Windows folder that tells case apart (as WSL makes them) holds
    # README.md and readme.md as two files, though Windows' own spelling
    # rules take them for one name.
    (tmp_path / "README.md").write_text("one", encoding="utf-8")
    numbers = {"README.md": 101, "readme.md": 102}
    renumber(monkeypatch, tmp_path, lambda name: numbers[name])
    assert not file_paths.same_file(str(tmp_path / "README.md"), str(tmp_path / "readme.md"))


# -- opening ------------------------------------------------------------------------------

class _Tabs:
    def __init__(self, paths):
        self.paths = list(paths)
        self.current = None

    def count(self):
        return len(self.paths)

    def widget(self, index):
        return self.paths[index]

    def setCurrentIndex(self, index):
        self.current = index


class _OpensANewTab(Exception):
    """Raised where open_path goes on to make a new tab."""


class _OpenWindow:
    """The slice of MainWindow that finds a file already open."""

    open_path = MainWindow.open_path

    def __init__(self, paths):
        self.tabs = _Tabs(paths)

    def _tab_file_path(self, widget):
        return widget

    def _new_wired_editor(self):
        raise _OpensANewTab()

    def _opens_in_viewer(self, path, kind=None):
        return False


def test_a_file_open_under_another_spelling_is_not_opened_again(note, tmp_path, monkeypatch):
    for spelled in spellings(note, monkeypatch):
        window = _OpenWindow([str(tmp_path / "other.md"), str(note)])
        # Its tab comes forward, and that is all: nothing to warn about.
        assert window.open_path(spelled) is None, spelled
        assert window.tabs.current == 1


def test_on_a_disk_without_identities_another_file_opens_in_its_own_tab(chapters, monkeypatch):
    without_identities(monkeypatch, os.path.dirname(chapters[0]))
    window = _OpenWindow([chapters[0]])
    with pytest.raises(_OpensANewTab):
        window.open_path(chapters[1])
    assert window.tabs.current is None


# -- saving -------------------------------------------------------------------------------

class _SaveAsWindow:
    """The slice of MainWindow that Save As uses."""

    save_as = MainWindow.save_as
    _saved_as = MainWindow._saved_as

    def __init__(self):
        self.saved, self.watched, self.current = [], [], []
        self._watcher = SimpleNamespace(addPath=self.watched.append,
                                        removePath=lambda path: None)

    def current_editor(self):
        return None

    def current_sheet(self):
        return None

    def current_reader(self):
        return None

    def current_path(self):
        return None

    def _save_to(self, path, then=None):
        self.saved.append(path)
        return True

    def set_current_path(self, path):
        self.current.append(path)

    def _populate_recent_menu(self):
        pass

    def _update_knit_actions(self):
        pass


def test_save_as_keeps_the_one_spelling_whatever_the_dialog_answers(note, monkeypatch):
    answered = (note.parent / "sub").as_posix() + "/../saved.md"
    monkeypatch.setattr(main_window_module, "QFileDialog", SimpleNamespace(
        getSaveFileName=lambda *a, **k: (answered, ".md (*.md)")))
    remembered = []
    monkeypatch.setattr(main_window_module, "add_recent", remembered.append)
    window = _SaveAsWindow()

    assert window.save_as()

    expected = str(note.parent / "saved.md")
    assert window.saved == window.watched == window.current == remembered == [expected]


# -- the recent files ---------------------------------------------------------------------

@pytest.fixture
def recent(tmp_path, monkeypatch):
    path = tmp_path / "recent.json"
    monkeypatch.setattr(recent_files, "_RECENT_FILE", str(path))
    return path


def test_the_recent_files_hold_each_file_once_however_it_was_spelled(recent, note, monkeypatch):
    other = str(note.parent / "other.md")
    recent.write_text(json.dumps([note.as_posix(), other, str(note)]), encoding="utf-8")
    assert recent_files.load_recent() == [str(note), other]

    for spelled in spellings(note, monkeypatch):
        recent_files.add_recent(spelled)
        assert recent_files.load_recent() == [str(note), other], spelled

    recent_files.add_recent(other)
    assert recent_files.load_recent() == [other, str(note)]


def test_on_a_disk_without_identities_the_recent_files_keep_every_file(recent, chapters,
                                                                      monkeypatch):
    without_identities(monkeypatch, os.path.dirname(chapters[0]))
    recent_files.add_recent(chapters[0])
    recent_files.add_recent(chapters[1])
    assert recent_files.load_recent() == [chapters[1], chapters[0]]


# -- renaming -----------------------------------------------------------------------------

class _RenameWindow:
    """The slice of MainWindow that renames a file."""

    _rename_tab = MainWindow._rename_tab
    _unwatch = MainWindow._unwatch
    _document_name = staticmethod(MainWindow._document_name)

    def __init__(self):
        self.titles = []
        self.kinds = []          # what each rename has the file read as
        self.tabs = SimpleNamespace(setTabText=lambda index, text: self.titles.append(text))
        self._watcher = SimpleNamespace(addPath=lambda path: None,
                                        removePath=lambda path: None,
                                        files=lambda: [], directories=lambda: [])

    def _update_window_title(self):
        pass

    def _attach_highlighter(self, ed, path):
        self.kinds.append(path)


def _rename(monkeypatch, path, new_name):
    told = []
    monkeypatch.setattr(main_window_module, "inform",
                        lambda parent, **kw: told.append(kw.get("title", "")))
    monkeypatch.setattr(main_window_module.QInputDialog, "getText",
                        lambda *a, **k: (new_name, True))
    moved, ed_names = [], []
    ed = SimpleNamespace(_file_path=str(path),
                         _backup=SimpleNamespace(update_file_path=moved.append),
                         setAccessibleName=ed_names.append, names=ed_names)
    window = _RenameWindow()
    window._rename_tab(0, ed)
    return ed, window, told, moved


def test_another_case_of_its_own_name_renames_the_file(note, monkeypatch):
    ed, window, told, moved = _rename(monkeypatch, note, "Note.md")
    renamed = str(note.parent / "Note.md")
    assert told == []
    assert "Note.md" in os.listdir(note.parent) and "note.md" not in os.listdir(note.parent)
    assert ed._file_path == renamed and moved == [renamed]
    assert window.titles == ["Note.md"] and ed.names == ["Note.md"]
    assert window.kinds == [renamed]           # read again for what it now is


def test_a_name_another_file_has_is_refused(note, monkeypatch):
    (note.parent / "taken.md").write_text("another file", encoding="utf-8")
    ed, window, told, moved = _rename(monkeypatch, note, "taken.md")
    assert told == ["“taken.md” already exists"]
    assert ed._file_path == str(note) and moved == []
    assert (note.parent / "taken.md").read_text(encoding="utf-8") == "another file"


def _link(target, name):
    """A link called ``name`` beside ``target``; skipped where this system
    does not let the tests make one."""
    link = target.parent / name
    try:
        link.symlink_to(target.name)
    except (OSError, NotImplementedError):
        pytest.skip("making a link needs administrator rights or developer mode here")
    return link


def test_a_link_renamed_to_the_name_of_its_file_is_refused_and_both_are_kept(tmp_path,
                                                                           monkeypatch):
    real = tmp_path / "real.md"
    real.write_text("the only copy of my text", encoding="utf-8")
    link = _link(real, "link.md")
    ed, window, told, moved = _rename(monkeypatch, link, "real.md")
    assert told == ["\u201creal.md\u201d already exists"]
    assert ed._file_path == str(link) and moved == []
    assert link.is_symlink() and not real.is_symlink()
    assert real.read_text(encoding="utf-8") == "the only copy of my text"


def test_the_name_of_a_broken_link_is_refused(note, monkeypatch):
    broken = _link(note.parent / "gone.md", "broken.md")
    ed, window, told, moved = _rename(monkeypatch, note, "broken.md")
    assert told == ["\u201cbroken.md\u201d already exists"]
    assert broken.is_symlink() and note.read_text(encoding="utf-8") == "text"


def test_a_case_change_is_judged_by_the_entries_themselves(note, monkeypatch):
    other_case = str(note.parent / "Note.md")
    # Where the disk ignores case, the other case finds the note itself.
    assert file_paths.is_case_change(str(note), other_case) == os.path.exists(other_case)
    assert not file_paths.is_case_change(str(note), str(note))
    # A second name of the same file is not a change of case.
    alias = note.parent / "alias.md"
    try:
        os.link(note, alias)
    except OSError:
        alias = None
    if alias is not None:
        assert not file_paths.is_case_change(str(note), str(alias))
    # Without identities nothing shows the two names are one entry.
    without_identities(monkeypatch, note.parent)
    assert not file_paths.is_case_change(str(note), other_case)
