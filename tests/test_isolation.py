# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tests keep out of the person's own files (tests/home_guard.py)."""

import ast
import builtins
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import recovery
import user_dirs
from tests import conftest, home_guard
from tests.test_portability import python_files

REPO = Path(__file__).resolve().parent.parent

# Every module that keeps a file in the app's folders (user_dirs).
PATH_MODULES = (
    "diagnostics", "dictation.models", "export_pdf", "main_window", "pdf_viewer",
    "recent_files", "recovery", "rmd_toolchain", "settings", "workspace",
    "nostr.blossom.names", "nostr.blossom.settings", "nostr.first_publications",
    "nostr.imports.favicons", "nostr.imports.subscriptions", "nostr.imports_controller",
    "nostr.key_vault", "nostr.known_people", "nostr.media.assets", "nostr.media.visibility",
    "nostr.metadata", "nostr.outbox.directory", "nostr.profiles", "nostr.standup.link_names",
    "nostr.ui.thumbnail_loader",
)


def test_the_run_has_a_home_of_its_own():
    assert Path.home() == Path(conftest.RUN_HOME)
    assert os.path.realpath(conftest.RUN_HOME) != os.path.realpath(home_guard.persons_home())
    assert user_dirs.config_dir() == Path(conftest.RUN_HOME) / ".config" / "my_editor"
    assert os.environ["XDG_CONFIG_HOME"] == os.path.join(conftest.RUN_HOME, ".config")


def test_no_test_backs_up_into_the_persons_home():
    """A window a test builds backs up what is typed in it, as the app does.
    Into the person's own ~/.cache, MyEditor would offer every one of them
    back at its next launch (thousands, after a few hundred test runs)."""
    real = os.path.join(home_guard.persons_home(), ".cache", "my_editor", "backups")
    assert os.path.abspath(recovery.backup_dir()) != os.path.abspath(real)


def _app_modules():
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None) or ""
        if path.startswith(str(REPO)) and "/tests/" not in path and "/.venv/" not in path:
            yield name, module


def _paths_in(value):
    if isinstance(value, (str, os.PathLike)):
        yield os.fspath(value)
    elif isinstance(value, (tuple, list)):
        for item in value:
            if isinstance(item, (str, os.PathLike)):
                yield os.fspath(item)


def test_no_app_module_builds_a_path_into_the_home_at_import():
    # The home is the run's own from before the first import: any module
    # or class value naming it was built at import, and would have named
    # the person's own home in the app (and in an earlier run).
    for name in PATH_MODULES:
        importlib.import_module(name)
    found = []
    for name, module in _app_modules():
        for attr, value in vars(module).items():
            holders = [(attr, value)]
            if isinstance(value, type) and value.__module__ == module.__name__:
                holders += [(f"{attr}.{a}", v) for a, v in vars(value).items()]
            for where, held in holders:
                if any(conftest.RUN_HOME in path for path in _paths_in(held)):
                    found.append(f"{name}.{where}")
    assert found == []


def test_only_user_dirs_spells_out_the_apps_folders():
    # One helper says where the folders are: a module spelling them out
    # itself would go on using its own copy when that helper changes.
    found = []
    for path in python_files():
        where = path.relative_to(REPO)
        if where.parts[0] == "tests" or where == Path("user_dirs.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                           ast.AsyncFunctionDef))
                      for body in [node.body]
                      if body and isinstance(body[0], ast.Expr)
                      and isinstance(body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and _spells_a_folder(node.value)):
                found.append(f"{where}:{node.lineno}")
    assert found == []


def _spells_a_folder(text):
    # A part of the path ("my_editor") or a whole one ("~/.config/my_editor");
    # the project's address ("rinbal/my_editor") names no folder.
    folders = (".config", ".cache")
    return text in (*folders, user_dirs.APP_FOLDER) \
        or any(f"{folder}/{user_dirs.APP_FOLDER}" in text for folder in folders)


def _resolvers():
    import diagnostics
    import export_pdf
    import pdf_viewer
    import recent_files
    import rmd_toolchain
    import settings
    import workspace
    from dictation import models
    from nostr import first_publications, key_vault, known_people, profiles
    from nostr.blossom import names, settings as blossom_settings
    from nostr.media import assets, visibility
    from nostr.outbox import directory
    from nostr.standup import link_names
    return {
        "settings": settings._settings_file, "recent files": recent_files._recent_file,
        "pdf positions": pdf_viewer._positions_path, "page setup": export_pdf._config_path,
        "backups": recovery.backup_dir, "workspace": workspace.workspace_file,
        "logs": diagnostics.log_folder, "dictation": models.folder,
        "rmd toolchain": rmd_toolchain._toolchain_dir,
        "blossom servers": blossom_settings.settings_file, "media names": names.names_file,
        "keys": key_vault.keys_file, "profiles": profiles.profiles_file,
        "media index": assets.index_file, "media ledger": visibility.ledger_file,
        "known people": known_people.known_people_file,
        "relay lists": directory.relay_lists_file,
        "first publications": first_publications.default_path,
        "submitted marks": link_names.default_path,
    }


@pytest.mark.parametrize("name", sorted(_resolvers()))
def test_every_app_path_is_found_in_the_home_when_it_is_used(name, tmp_path, monkeypatch):
    # The app's own place, not the folder every test backs up into (conftest).
    monkeypatch.setattr(recovery, "BACKUP_DIR", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    found = Path(_resolvers()[name]())
    assert found.is_relative_to(tmp_path / ".config" / "my_editor") \
        or found.is_relative_to(tmp_path / ".cache" / "my_editor"), found


def test_a_store_made_without_a_path_uses_the_home_of_that_moment(tmp_path, monkeypatch):
    from nostr.blossom.names import MediaNames
    from nostr.key_vault import KeyVault
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert KeyVault()._path == tmp_path / ".config" / "my_editor" / "nostr_keys.json"
    assert MediaNames()._path == tmp_path / ".config" / "my_editor" / "media_names.json"


def test_the_guard_names_what_changed_and_never_opens_a_file(tmp_path, monkeypatch):
    folder = tmp_path / "my_editor"
    (folder / "cache").mkdir(parents=True)
    (folder / "settings.json").write_text("{}", encoding="utf-8")
    (folder / "gone.json").write_text("{}", encoding="utf-8")
    before = home_guard.snapshot(str(folder))
    (folder / "settings.json").write_text('{"window_geometry": 1}', encoding="utf-8")
    (folder / "gone.json").unlink()
    (folder / "cache" / "new").write_text("x", encoding="utf-8")

    def no_open(*_args, **_kwargs):
        raise AssertionError("the guard opened a file")

    monkeypatch.setattr(builtins, "open", no_open)
    after = home_guard.snapshot(str(folder))
    monkeypatch.undo()
    assert home_guard.changed(before, after) == [".", "cache", "cache/new".replace("/", os.sep),
                                                 "gone.json", "settings.json"]
    assert home_guard.changed(after, after) == []
    assert home_guard.snapshot(str(tmp_path / "none")) == {}


def test_the_run_fails_when_the_persons_settings_folder_changed(monkeypatch, capsys):
    session = SimpleNamespace(exitstatus=0, config=SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda _name: None)))
    monkeypatch.setattr(conftest, "PERSONS_CONFIG_AT_START", {"": (None, 1)})
    monkeypatch.setattr(home_guard, "snapshot", lambda _folder: {"": (None, 1)})
    conftest._check_persons_config(session)
    assert session.exitstatus == 0
    monkeypatch.setattr(home_guard, "snapshot",
                        lambda _folder: {"": (None, 2), "pdf_positions.json": (10, 2)})
    conftest._check_persons_config(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    said = capsys.readouterr().err
    assert "pdf_positions.json" in said and conftest.PERSONS_CONFIG in said


def test_the_guard_watches_the_persons_own_settings_folder():
    assert conftest.PERSONS_CONFIG == os.path.join(home_guard.persons_home(), ".config",
                                                   "my_editor")
    assert isinstance(conftest.PERSONS_CONFIG_AT_START, dict)


def test_the_end_of_the_run_checks_the_persons_settings_folder(monkeypatch):
    from PySide6.QtWidgets import QApplication
    checked = []
    monkeypatch.setattr(QApplication, "topLevelWidgets", staticmethod(lambda: []))
    monkeypatch.setattr(conftest, "_check_persons_config", checked.append)
    session = SimpleNamespace(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert checked == [session]


def test_only_a_home_the_run_made_is_ever_removed(tmp_path):
    import tempfile
    made = tempfile.mkdtemp(prefix="myeditor-tests-home-")
    assert home_guard.remove_run_home(made) and not os.path.exists(made)
    other = tempfile.mkdtemp(prefix="myeditor-other-")
    try:
        assert not home_guard.remove_run_home(other) and os.path.isdir(other)
    finally:
        os.rmdir(other)
    nested = tmp_path / "myeditor-tests-home-x"
    nested.mkdir()
    assert not home_guard.remove_run_home(str(nested)) and nested.is_dir()
