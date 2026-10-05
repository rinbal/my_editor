#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The open tabs, written down so an update restart brings them back.

Quitting on purpose asks about unsaved work and remembers only saved files
(MainWindow._save_session). An update restart is different: the person did
not choose to quit, so nothing they had open may be lost or asked about.
Before MyEditor closes for an update it records every tab in order: the
file it shows, the cursor and scroll position, the Nostr draft it is linked
to, and, for untitled or unsaved tabs, the crash-recovery backup that holds
the content (recovery.py). The next launch takes this record once, reopens
the tabs as they were, and deletes it.

The record only points at backups and never copies document content, so
unsaved work lives in one place on disk with one cleanup rule. Backups a
record points at are claimed by it, so the crash-recovery sweep that runs
afterwards does not open them a second time.

The record also says which version the restart was meant to reach, so the
next launch can tell a finished update from one that did not install.

Pure stdlib on purpose, so the format is testable without a window.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

WORKSPACE_FILE = os.path.join(os.path.expanduser("~"), ".cache", "my_editor", "workspace.json")

# Readers skip a record with a higher version instead of guessing at it.
WORKSPACE_VERSION = 1

# Tab kinds.
DOCUMENT = "document"
PDF = "pdf"
WELCOME = "welcome"
_KINDS = (DOCUMENT, PDF, WELCOME)

# A record is a short list of paths and numbers. Anything bigger is not one.
_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class TabState:
    """One tab as it was when MyEditor closed for an update."""

    kind: str
    path: Optional[str] = None          # the file the tab shows, if any
    backup_file: Optional[str] = None   # recovery record holding unsaved content
    modified: bool = False              # the tab had unsaved changes
    cursor: int = 0
    anchor: int = 0                     # the other end of a selection
    scroll: int = 0                     # vertical scroll bar value
    draft: Optional[dict] = None        # the tab's DraftBinding, as a dict


@dataclass(frozen=True)
class Workspace:
    """Every tab, the active one, and what the restart was for."""

    tabs: tuple = ()
    active: int = 0
    from_version: str = ""
    to_version: str = ""
    release_notes: str = ""   # Markdown, shown as What's New after the restart
    release_url: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))

    def claimed_backups(self) -> set:
        """Absolute paths of the recovery records this workspace restores."""
        return {os.path.abspath(t.backup_file) for t in self.tabs if t.backup_file}


def write_workspace(workspace: Workspace, path: str = WORKSPACE_FILE) -> bool:
    """Write the record atomically. False when it could not be written, in
    which case the caller must not count on the next launch restoring it."""
    record = asdict(workspace)
    record["version"] = WORKSPACE_VERSION
    record["tabs"] = [asdict(t) for t in workspace.tabs]
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        _remove(tmp)
        return False
    return True


def take_workspace(path: str = WORKSPACE_FILE) -> Optional[Workspace]:
    """Read the record and delete it, so it is restored at most once.

    A record from a newer build is not read, and not deleted here: this
    build can't know what it means. (A normal quit still removes it, like
    any leftover record; see MainWindow.closeEvent.) Anything unreadable is
    deleted and treated as absent: a broken record must not stop MyEditor
    from starting, and the backups it pointed at are still found by the
    crash-recovery sweep.
    """
    if not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            raise ValueError("too large")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        _remove(path)
        return None
    if not isinstance(data, dict):
        _remove(path)
        return None
    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > WORKSPACE_VERSION:
        return None
    _remove(path)
    return _parse(data)


def discard_workspace(path: str = WORKSPACE_FILE) -> None:
    """Forget a record written for a restart that is not going to happen."""
    _remove(path)


# -- parsing -------------------------------------------------------------------

def _parse(data: dict) -> Optional[Workspace]:
    raw_tabs = data.get("tabs")
    if not isinstance(raw_tabs, list):
        return None
    # One malformed tab is skipped, never the whole record: the other tabs
    # it was written beside are still worth bringing back.
    tabs = tuple(t for t in (_parse_tab(r) for r in raw_tabs) if t is not None)
    active = _int(data.get("active"))
    if not 0 <= active < len(tabs):
        active = 0
    return Workspace(
        tabs=tabs,
        active=active,
        from_version=_str(data.get("from_version")) or "",
        to_version=_str(data.get("to_version")) or "",
        release_notes=_str(data.get("release_notes")) or "",
        release_url=_str(data.get("release_url")) or "",
        created_at=_int(data.get("created_at")),
    )


def _parse_tab(raw) -> Optional[TabState]:
    if not isinstance(raw, dict) or raw.get("kind") not in _KINDS:
        return None
    kind = raw["kind"]
    path = _str(raw.get("path"))
    backup_file = _str(raw.get("backup_file"))
    if kind == PDF and not path:
        return None
    draft = raw.get("draft")
    return TabState(
        kind=kind,
        path=path,
        backup_file=backup_file,
        modified=raw.get("modified") is True,
        cursor=max(0, _int(raw.get("cursor"))),
        anchor=max(0, _int(raw.get("anchor"))),
        scroll=max(0, _int(raw.get("scroll"))),
        draft=draft if isinstance(draft, dict) else None,
    )


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _str(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
