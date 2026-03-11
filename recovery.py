#!/usr/bin/env python3
"""
Crash recovery: debounced backup files for every open editor.

Each editor gets an EditorBackup instance that writes a JSON snapshot
to ~/.cache/my_editor/backups/ a few seconds after the last keystroke.
On a normal close the backup is deleted; on a crash it survives and is
restored silently on the next launch.
"""

import hashlib
import json
import os
import uuid

from PySide6.QtCore import QTimer


BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".cache", "my_editor", "backups")
_DEBOUNCE_MS = 3_000
_MAX_INTERVAL_MS = 10_000


def _ensure_backup_dir() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _backup_id_for(file_path: str | None) -> str:
    """Stable ID derived from the file path, or a fresh UUID for untitled docs."""
    if file_path:
        return hashlib.md5(file_path.encode()).hexdigest()
    return str(uuid.uuid4())


def _backup_path_for(backup_id: str) -> str:
    return os.path.join(BACKUP_DIR, f"{backup_id}.autosave")


class EditorBackup:
    """Manages the backup lifecycle for a single editor instance."""

    def __init__(self, editor, file_path: str | None):
        self._editor = editor
        self._file_path = file_path
        self._backup_id = _backup_id_for(file_path)

        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._write)

        self._max_timer = QTimer()
        self._max_timer.setInterval(_MAX_INTERVAL_MS)
        self._max_timer.timeout.connect(self._write)
        self._max_timer.start()

        self._editor.document().contentsChanged.connect(self._schedule)

    def update_file_path(self, new_path: str) -> None:
        """Call when an untitled doc is saved with a new path for the first time."""
        old_backup = _backup_path_for(self._backup_id)
        self._file_path = new_path
        self._backup_id = _backup_id_for(new_path)
        if os.path.exists(old_backup):
            try:
                os.remove(old_backup)
            except OSError:
                pass

    def write_now(self) -> None:
        """Force an immediate write, bypassing the debounce timer."""
        self._timer.stop()
        self._write()

    def delete(self) -> None:
        """Call on normal close: stop the timers and remove the backup file."""
        self._timer.stop()
        self._max_timer.stop()
        try:
            self._editor.document().contentsChanged.disconnect(self._schedule)
        except RuntimeError:
            pass
        path = _backup_path_for(self._backup_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule(self) -> None:
        self._timer.start()  # restarts the countdown on every change

    def _write(self) -> None:
        content = self._editor.toPlainText()
        if not content.strip():
            return  # nothing worth backing up
        _ensure_backup_dir()
        data = {
            "original_path": self._file_path,
            "content": content,
        }
        try:
            with open(_backup_path_for(self._backup_id), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass  # backup is best-effort; never raise to the user


def find_all_backups() -> list[dict]:
    """Return every valid backup record found on disk."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    results = []
    for name in os.listdir(BACKUP_DIR):
        if not name.endswith(".autosave"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_backup_file"] = path
            results.append(data)
        except (OSError, json.JSONDecodeError):
            pass
    return results
