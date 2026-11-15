# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The names files had when they were added to the Media Library.

A media server keeps a file by its hash and knows no name for it, so a
library of hashes would show a grid of dates. The name a file had on
this computer when it was added is kept here, by hash, beside the
server settings: one small JSON file, written atomically. It is a
convenience only: losing it costs the names, never a file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

import user_dirs
from atomic_file import write_text


def names_file() -> Path:
    """media_names.json in the person's config folder, found each time
    (user_dirs)."""
    return user_dirs.config_dir() / "media_names.json"

# Long enough for any real file name, short enough that a hand-edited
# file cannot fill a tile with a novel.
_MAX_NAME = 200


class MediaNames:
    """Hash to file name, read once and written on every change."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else names_file()
        self._names: Dict[str, str] = {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        names = data.get("names") if isinstance(data, dict) else None
        if isinstance(names, dict):
            self._names = {str(k).lower(): str(v)[:_MAX_NAME]
                           for k, v in names.items() if isinstance(v, str) and v.strip()}

    def get(self, sha256: str) -> str:
        return self._names.get((sha256 or "").lower(), "")

    def remember(self, sha256: str, name: str) -> None:
        sha = (sha256 or "").lower()
        name = " ".join((name or "").split())[:_MAX_NAME]
        if not sha or not name or self._names.get(sha) == name:
            return
        self._names[sha] = name
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass
        try:
            write_text(self._path, json.dumps({"version": 1, "names": self._names},
                                              indent=2, ensure_ascii=False))
        except OSError:
            pass
