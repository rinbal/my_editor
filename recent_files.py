#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import Optional

import user_dirs
from atomic_file import read_json, write_json
from file_paths import normalize, same_file

# A file of its own (a test's); None: recent_files.json in the person's
# cache folder, found each time (user_dirs).
_RECENT_FILE: Optional[str] = None
_MAX_ENTRIES = 10


def load_recent() -> list[str]:
    """Return the stored recent file paths, newest first, missing files
    included, each in the app's one spelling (an older version may have
    written another) and each once."""
    entries: list[str] = []
    for path in read_json(_recent_file(), list):
        if isinstance(path, str) and path:
            path = normalize(path)
            if path not in entries:
                entries.append(path)
    return entries


def add_recent(path: str) -> None:
    """Put path at the top of the recent list, once, however it was spelled
    before, capped at _MAX_ENTRIES."""
    path = normalize(path)
    entries = [p for p in load_recent() if not same_file(p, path)]
    entries.insert(0, path)
    _save(entries[:_MAX_ENTRIES])


def clear_recent() -> None:
    _save([])


def _recent_file() -> str:
    return _RECENT_FILE or str(user_dirs.cache_dir() / "recent_files.json")


def _save(entries: list[str]) -> None:
    write_json(_recent_file(), entries, indent=2)
