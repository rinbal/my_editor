# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the record an update restart reopens the tabs from.

What must hold:

  The record is restored at most once: reading it deletes it.

  A broken record never stops MyEditor from starting, and one broken tab
  never costs the tabs written beside it.

  A record from a newer build is left alone, not guessed at.

  The record names the backups it restores, so crash recovery can leave
  them to it.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workspace  # noqa: E402
from workspace import DOCUMENT, PDF, WELCOME, TabState, Workspace  # noqa: E402


def sample():
    return Workspace(
        tabs=(
            TabState(kind=WELCOME),
            TabState(kind=DOCUMENT, path="/notes/a.md", backup_file="/b/1.autosave",
                     modified=True, cursor=12, anchor=8, scroll=300,
                     draft={"identifier": "abc", "inner_kind": 30023}),
            TabState(kind=PDF, path="/papers/x.pdf"),
            TabState(kind=DOCUMENT, backup_file="/b/2.autosave", modified=True),
        ),
        active=1,
        from_version="3.3",
        to_version="3.4",
        release_notes="# MyEditor v3.4\n\n- New",
        release_url="https://github.com/rinbal/my_editor/releases/tag/v3.4",
    )


def test_a_written_workspace_comes_back_exactly(tmp_path):
    path = str(tmp_path / "workspace.json")
    original = sample()
    assert workspace.write_workspace(original, path)
    restored = workspace.take_workspace(path)
    assert restored.tabs == original.tabs
    assert restored.active == 1
    assert (restored.from_version, restored.to_version) == ("3.3", "3.4")
    assert restored.release_notes == original.release_notes
    assert restored.release_url == original.release_url


def test_taking_the_workspace_deletes_it(tmp_path):
    path = str(tmp_path / "workspace.json")
    workspace.write_workspace(sample(), path)
    assert workspace.take_workspace(path) is not None
    assert not os.path.exists(path)
    assert workspace.take_workspace(path) is None


def test_no_record_means_nothing_to_restore(tmp_path):
    assert workspace.take_workspace(str(tmp_path / "missing.json")) is None


def test_an_unreadable_record_is_dropped_without_raising(tmp_path):
    path = tmp_path / "workspace.json"
    for junk in ("{not json", "[]", '"text"', json.dumps({"tabs": "nope"})):
        path.write_text(junk)
        assert workspace.take_workspace(str(path)) is None
        assert not path.exists()


def test_one_broken_tab_does_not_cost_the_others(tmp_path):
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps({
        "version": 1,
        "tabs": [
            {"kind": "document", "path": "/a.txt"},
            "garbage",
            {"kind": "spreadsheet", "path": "/x"},
            {"kind": "pdf"},                       # a PDF tab needs its file
            {"kind": "document", "cursor": "12", "scroll": -5, "modified": "yes"},
        ],
        "active": 7,
    }))
    restored = workspace.take_workspace(str(path))
    assert [t.path for t in restored.tabs] == ["/a.txt", None]
    last = restored.tabs[1]
    assert (last.cursor, last.scroll, last.modified) == (0, 0, False)
    assert restored.active == 0   # out of range falls back to the first tab


def test_a_record_from_a_newer_build_is_left_untouched(tmp_path):
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps({"version": workspace.WORKSPACE_VERSION + 1, "tabs": []}))
    assert workspace.take_workspace(str(path)) is None
    assert path.exists()


def test_the_record_names_the_backups_it_restores():
    claimed = sample().claimed_backups()
    assert claimed == {os.path.abspath("/b/1.autosave"), os.path.abspath("/b/2.autosave")}


def test_discarding_removes_the_record(tmp_path):
    path = str(tmp_path / "workspace.json")
    workspace.write_workspace(sample(), path)
    workspace.discard_workspace(path)
    assert not os.path.exists(path)
    workspace.discard_workspace(path)   # and is harmless when there is none


def test_a_failed_write_reports_false_and_leaves_no_partial_file(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    path = str(blocker / "workspace.json")   # a file where a folder should be
    assert workspace.write_workspace(sample(), path) is False
    assert not os.path.exists(path + ".tmp")
