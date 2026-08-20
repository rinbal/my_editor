#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Crash recovery: debounced backup files for every open editor.

Each editor gets an EditorBackup instance that writes a JSON snapshot
to ~/.cache/my_editor/backups/ a few seconds after the last keystroke.
On a normal close the backup is deleted; on a crash it survives and is
restored silently on the next launch.

Snapshots are HTML, not plain text. A plain-text snapshot restored over
an .html original and saved erased every image in the document, which is
the one thing a crash-recovery feature must never do. The record carries
a version so a build that predates this format leaves a newer file
alone, and a source mtime so a backup older than the file on disk can be
recognised and restored as a copy instead of overwriting newer work.
"""

import hashlib
import json
import os
import re
import time
import uuid

from PySide6.QtCore import QTimer

from doc_walk import iter_image_names
from nostr.media.assets import ASSET_SCHEME


BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".cache", "my_editor", "backups")
_DEBOUNCE_MS = 3_000
_MAX_INTERVAL_MS = 10_000

# Current record format. Readers must skip anything higher (see
# classify_backup's caller) rather than guess at its meaning.
BACKUP_VERSION = 2

# Truncated HTML is corrupt HTML, so an oversized snapshot is skipped
# whole and the previous backup is kept. The cooldown stops a huge
# document from re-serializing on every debounce tick.
MAX_BACKUP_BYTES = 32 * 1024 * 1024
_OVERSIZE_RETRY_S = 30.0

# Inline images above this size are handed to the asset layer instead of
# being written into the snapshot again and again. Smaller ones are left
# alone: the rewrite costs more than the bytes save.
MIN_EXTERNALIZE_BYTES = 64 * 1024

_DATA_URI_SRC_RE = re.compile(r'src="(data:[^"]*;base64,[^"]*)"')
_ASSET_KEY_RE = re.compile(re.escape(ASSET_SCHEME) + r":[0-9a-f]{64}")


def _ensure_backup_dir() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _backup_id_for(file_path: str | None) -> str:
    """Stable ID derived from the file path, or a fresh UUID for untitled docs."""
    if file_path:
        return hashlib.md5(file_path.encode()).hexdigest()
    return str(uuid.uuid4())


def _backup_path_for(backup_id: str) -> str:
    return os.path.join(BACKUP_DIR, f"{backup_id}.autosave")


def _source_mtime_ns(file_path: str | None) -> int | None:
    if not file_path:
        return None
    try:
        return os.stat(file_path).st_mtime_ns
    except OSError:
        return None


def classify_backup(record: dict) -> str:
    """How a backup relates to the file it came from.

    Returns "untitled" (no original path), "fresh" (the backup is at
    least as new as the file, or the file is gone) or "stale" (the file
    on disk has moved on since the snapshot).

    A record with no ``source_mtime_ns`` cannot prove it is fresh, so an
    existing file makes it stale. Being wrong in that direction costs
    one Save As; being wrong the other way overwrites newer work.
    """
    path = record.get("original_path") if isinstance(record, dict) else None
    if not path:
        return "untitled"
    mtime = _source_mtime_ns(path)
    if mtime is None:
        return "fresh"
    saved = record.get("source_mtime_ns")
    if isinstance(saved, bool) or not isinstance(saved, int):
        return "stale"
    return "stale" if mtime > saved else "fresh"


class EditorBackup:
    """Manages the backup lifecycle for a single editor instance."""

    def __init__(self, editor, file_path: str | None, *, externalize=None):
        self._editor = editor
        self._file_path = file_path
        self._backup_id = _backup_id_for(file_path)
        # Maps a data: URI to a resolvable asset key. Injected so this
        # module stays testable without the asset layer running.
        self._externalize = externalize
        self._last_hash = ""
        self._oversize_until = 0.0

        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._on_timeout)

        self._max_timer = QTimer()
        self._max_timer.setInterval(_MAX_INTERVAL_MS)
        self._max_timer.timeout.connect(self._on_timeout)
        self._max_timer.start()

        self._editor.document().contentsChanged.connect(self._schedule)

    @property
    def path(self) -> str:
        """Where this backup writes. Callers cleaning up an older file
        compare against it so they never delete the live snapshot."""
        return _backup_path_for(self._backup_id)

    def update_file_path(self, new_path: str) -> None:
        """Call when an untitled doc is saved with a new path for the first time."""
        old_backup = _backup_path_for(self._backup_id)
        self._file_path = new_path
        self._backup_id = _backup_id_for(new_path)
        # Saving over the same path keeps the same ID, and removing that
        # file would leave the document with no crash protection.
        if old_backup != self.path and os.path.exists(old_backup):
            try:
                os.remove(old_backup)
            except OSError:
                pass

    def write_now(self) -> bool:
        """Force an immediate write, bypassing the debounce timer."""
        self._timer.stop()
        return self._write()

    def delete(self) -> None:
        """Call on normal close: stop the timers and remove the backup file."""
        self._timer.stop()
        self._max_timer.stop()
        try:
            self._editor.document().contentsChanged.disconnect(self._schedule)
        except RuntimeError:
            pass
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule(self) -> None:
        self._timer.start()  # restarts the countdown on every change

    def _on_timeout(self) -> None:
        if time.monotonic() < self._oversize_until:
            return
        self._write()

    def _snapshot(self) -> str:
        """The HTML to persist, with big inline images externalized.

        Only the snapshot string is rewritten; the live document keeps
        whatever spelling it had, so nothing the user can see changes.
        """
        content = self._editor.document().toHtml()
        if self._externalize is None:
            return content

        def replace(match) -> str:
            uri = match.group(1)
            if len(uri) <= MIN_EXTERNALIZE_BYTES:
                return match.group(0)
            key = self._externalize(uri)
            return f'src="{key}"' if key else match.group(0)

        return _DATA_URI_SRC_RE.sub(replace, content)

    def _write(self) -> bool:
        """Write the snapshot. False means nothing usable is on disk yet."""
        doc = self._editor.document()
        has_image = any(True for _ in iter_image_names(doc))
        if doc.characterCount() <= 1 and not has_image:
            return False  # an empty document's HTML is still a full skeleton

        content = self._snapshot()
        fingerprint = hashlib.sha256(
            f"{self._file_path or ''}\n{content}".encode("utf-8")
        ).hexdigest()
        # The fingerprint says the payload has not changed, not that the
        # file survived: a restore or a cleanup elsewhere can have
        # removed it, and skipping the write then would leave the
        # document unprotected until the next keystroke.
        if fingerprint == self._last_hash and os.path.exists(self.path):
            return True  # already on disk, byte for byte

        record = {
            "version": BACKUP_VERSION,
            "format": "html",
            "original_path": self._file_path,
            "content": content,
            "assets": list(dict.fromkeys(_ASSET_KEY_RE.findall(content))),
            "saved_at": int(time.time()),
            "source_mtime_ns": _source_mtime_ns(self._file_path),
        }
        payload = json.dumps(record, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_BACKUP_BYTES:
            self._oversize_until = time.monotonic() + _OVERSIZE_RETRY_S
            return False

        try:
            _ensure_backup_dir()
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError:
            return False  # backup is best-effort; never raise to the user
        self._last_hash = fingerprint
        return True


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
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    return results
