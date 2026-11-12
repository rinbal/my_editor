# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The list of open files kept for the next launch."""

import json

import pytest

from main_window import MainWindow


class Tabs:
    def __init__(self, paths):
        self.paths = paths
        self.current = 0

    def count(self):
        return len(self.paths)

    def widget(self, index):
        return self.paths[index]

    def currentIndex(self):
        return self.current

    def setCurrentIndex(self, index):
        self.current = index


class Status:
    def __init__(self):
        self.messages = []

    def showMessage(self, text, _timeout=0):
        self.messages.append(text)


class Window:
    """Just what the session code uses."""

    _save_session = MainWindow._save_session
    _restore_session = MainWindow._restore_session
    _session_file = MainWindow._session_file

    def __init__(self, session_file, open_paths=()):
        self._SESSION_FILE = str(session_file)
        self.tabs = Tabs(list(open_paths))
        self.status = Status()
        self.opened = []

    def _tab_file_path(self, widget):
        return widget

    def open_path(self, path):
        self.opened.append(path)
        self.tabs.paths.append(path)


@pytest.fixture
def files(tmp_path):
    made = []
    for name in ("a.md", "b.md"):
        path = tmp_path / name
        path.write_text("text", encoding="utf-8")
        made.append(str(path))
    return made


def test_the_open_files_come_back_once(tmp_path, files):
    session = tmp_path / "session.json"
    Window(session, files)._save_session()
    window = Window(session)
    assert window._restore_session()
    assert window.opened == files
    assert not session.exists()
    assert Window(session)._restore_session() is False


@pytest.mark.parametrize("content", [
    '{"paths": ["a.md"',                     # cut off
    "[1, 2]",                                # not a record
    '{"paths": "not a list"}',
    '{"paths": [5, null]}',
])
def test_a_damaged_session_is_dropped_without_an_error(tmp_path, content):
    session = tmp_path / "session.json"
    session.write_text(content, encoding="utf-8")
    window = Window(session)
    assert window._restore_session() is False
    assert window.opened == []
    assert not session.exists()


def test_a_strange_active_tab_is_ignored(tmp_path, files):
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"paths": files, "active": "2"}), encoding="utf-8")
    window = Window(session)
    assert window._restore_session()
    assert window.tabs.current == 0


def test_files_that_are_gone_are_counted(tmp_path, files):
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"paths": files + [str(tmp_path / "gone.md")]}),
                       encoding="utf-8")
    window = Window(session)
    assert window._restore_session()
    assert window.opened == files
    assert len(window.status.messages) == 1
