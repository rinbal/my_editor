#!/usr/bin/env python3

import json
import os

_RECENT_FILE = os.path.expanduser("~/.cache/my_editor/recent_files.json")
_MAX_ENTRIES = 10


def load_recent() -> list[str]:
    """Return the stored list of recent file paths (all entries, including missing files)."""
    try:
        with open(_RECENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, str)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def add_recent(path: str) -> None:
    """Add path to the top of the recent list, removing duplicates, capped at _MAX_ENTRIES."""
    entries = load_recent()
    if path in entries:
        entries.remove(path)
    entries.insert(0, path)
    _save(entries[:_MAX_ENTRIES])


def clear_recent() -> None:
    _save([])


def _save(entries: list[str]) -> None:
    os.makedirs(os.path.dirname(_RECENT_FILE), exist_ok=True)
    with open(_RECENT_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
