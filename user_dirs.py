# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where MyEditor keeps its files in the person's home: what it is told
and remembers under ``~/.config/my_editor``, what it can make again under
``~/.cache/my_editor``.

Both are read from the home folder each time they are asked for, never
once at import: a process that points HOME elsewhere first (the test
run, tests/conftest.py) then never reads or writes the person's own
files. Every path into these folders is built from here.
"""

from __future__ import annotations

from pathlib import Path

APP_FOLDER = "my_editor"


def config_dir() -> Path:
    """``~/.config/my_editor``: settings, accounts, lists, positions."""
    return Path.home() / ".config" / APP_FOLDER


def cache_dir() -> Path:
    """``~/.cache/my_editor``: logs, backups, the session, thumbnails."""
    return Path.home() / ".cache" / APP_FOLDER


__all__ = ["APP_FOLDER", "cache_dir", "config_dir"]
