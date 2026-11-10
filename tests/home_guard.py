# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The test run keeps out of the person's own files.

tests/conftest.py points HOME (and the XDG folders) at a folder of the
run's own before any app module is imported (:func:`isolate`), so every
path the app builds (user_dirs) lands there. As a second line, it notes
what the person's own ``~/.config/my_editor`` holds when the run starts
(:func:`snapshot`) and fails the run when anything there changed by its
end (:func:`changed`). Only names, sizes and modification times are
compared: no file there is ever opened, and nothing from one is printed.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from typing import Dict, List, Optional, Tuple

Entry = Tuple[Optional[int], int]          # size (None for a folder), mtime in ns
_PREFIX = "myeditor-tests-home-"


def persons_home() -> str:
    """The person's own home folder, whatever HOME says now: from the
    account database where there is one, so a run started with HOME
    pointed elsewhere still watches the right folder."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError):
        return os.environ.get("USERPROFILE") or os.path.expanduser("~")


def persons_config() -> str:
    """The person's own MyEditor settings folder (user_dirs.config_dir)."""
    return os.path.join(persons_home(), ".config", "my_editor")


def isolate() -> str:
    """Point HOME, USERPROFILE and the XDG folders at a new empty folder
    of the run's own, and return it. Call before any app module is
    imported; child processes the tests start inherit it."""
    home = tempfile.mkdtemp(prefix=_PREFIX)
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    for variable, folder in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache"),
                             ("XDG_DATA_HOME", ".local/share"),
                             ("XDG_STATE_HOME", ".local/state")):
        os.environ[variable] = os.path.join(home, folder)
    return home


def remove_run_home(home: str) -> bool:
    """Remove the run's own home, and only that: a folder :func:`isolate`
    made (its name, directly in the system's temporary folder), never the
    person's home or anything else. Whether it was removed."""
    path = os.path.realpath(home)
    made_here = (os.path.basename(path).startswith(_PREFIX)
                 and os.path.dirname(path) == os.path.realpath(tempfile.gettempdir())
                 and path != os.path.realpath(persons_home()))
    if made_here:
        shutil.rmtree(path, ignore_errors=True)
    return made_here


def snapshot(folder: str) -> Dict[str, Entry]:
    """Every file and folder under ``folder``, the folder itself too, by
    its path inside it: its size and modification time, never what it
    holds. Empty when there is no such folder."""
    top = _entry(folder) if os.path.isdir(folder) else None
    if top is None:
        return {}
    seen: Dict[str, Entry] = {"": top}
    for root, folders, files in os.walk(folder):
        for name in folders + files:
            path = os.path.join(root, name)
            entry = _entry(path)
            if entry is not None:
                seen[os.path.relpath(path, folder)] = entry
    return seen


def _entry(path: str) -> Optional[Entry]:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    size = None if stat.S_ISDIR(info.st_mode) else info.st_size
    return size, info.st_mtime_ns


def changed(before: Dict[str, Entry], after: Dict[str, Entry]) -> List[str]:
    """The names added, removed or changed in size or time, sorted; the
    folder itself is named ".". Never their contents."""
    names = set(before) | set(after)
    return sorted(name or "." for name in names if before.get(name) != after.get(name))


__all__ = ["changed", "isolate", "persons_config", "persons_home", "remove_run_home",
           "snapshot"]
