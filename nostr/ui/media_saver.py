# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save As for the Media Library.

One file at a time: from this computer's copy when there is one, else
streamed from its media servers, one address after another, bounded by
its own size and checked against its hash before it takes its name. A
private file is saved as the file, never as its envelope.

The saver belongs to what outlives the library windows (the main
window), so a save goes on when the window that asked for it closes, as
a copy in the Finder does. Whoever is looking says how it went: the
library window while one is open, else the main window's status bar.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from atomic_file import free_path, save_document
from i18n import _

from ..blossom.store import MediaFile
from .thumbnail_loader import write_problem

# Reasons another server may still have the right file.
_NEXT_SERVER = ("network", "missing", "server", "wrong_file")


@dataclass
class _Batch:
    """The files of one Save to Folder, and the ones that weren't saved."""
    total: int
    left: int
    failures: List[Tuple[str, str]] = field(default_factory=list)   # (path, why)


@dataclass
class _Save:
    sha: str
    size: int
    target: str
    urls: List[str]
    # A private file's envelope to the file; None for a public one.
    opener: Optional[Callable[[bytes], Optional[bytes]]]
    # A file found at the target stays, and this one takes a free name.
    keep_existing: bool
    batch: Optional[_Batch] = None


class MediaSaver(QObject):
    """Saves Media Library files where the person chose, in turn."""

    started = Signal(str)            # the path being saved
    progress = Signal(int)           # percent of it
    # The path, and "" when it is saved, "cancelled", or why it wasn't
    # (a FileDownload reason, or "unreadable": this computer's copy could
    # not be read): save_failure says it. Only a reason another server
    # may not have ("network", "missing", "server", "wrong_file") tries
    # the next one; a private file's wrong key ("key") is the same
    # wherever the file comes from.
    finished = Signal(str, str)
    # A Save to Folder ended (not stopped): how many files it had, and
    # the (path, why) of each that wasn't saved; saves_failure says it.
    batch_finished = Signal(int, list)

    def __init__(self, loader, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._loader = loader
        self._queue: List[_Save] = []
        self._download = None
        self._percent = 0

    @property
    def current(self) -> str:
        """The path being downloaded to, "" when nothing is."""
        return self._queue[0].target if self._download is not None else ""

    @property
    def percent(self) -> int:
        return self._percent

    def pending(self) -> List[str]:
        """The paths still to be saved: the one downloading and the ones
        waiting, which quitting would stop."""
        return [save.target for save in self._queue]

    def save(self, media: MediaFile, target: str, *,
             opener: Optional[Callable[[bytes], Optional[bytes]]] = None,
             keep_existing: bool = False) -> None:
        """Save ``media`` at ``target``, after the saves before it.

        ``keep_existing`` (Save to Folder, where the names are the
        library's): a file of that name stays, now or when this one is
        written, and this one is numbered as the Finder numbers a copy.
        A Save As replaces only the file the system's save dialog asked
        about, one there now: a file that appears under that name before
        this one is written (another save's, under another spelling the
        folder takes for the same name, such as "a.mp4" for "A.mp4")
        stays too. Either way a name an earlier save is still going to
        is never taken.
        """
        self._enqueue(media, target, opener, keep_existing)
        self._next()

    def save_all(self, files) -> None:
        """Save to Folder: each ``(media, target, opener)`` of ``files``,
        replacing nothing, and say at the end (``batch_finished``) what
        wasn't saved."""
        batch = _Batch(total=len(files), left=len(files))
        for media, target, opener in files:
            self._enqueue(media, target, opener, True, batch)
        self._next()

    def _enqueue(self, media: MediaFile, target: str, opener, keep_existing: bool,
                 batch: Optional[_Batch] = None) -> None:
        taken = {save.target for save in self._queue}
        if keep_existing or target in taken:
            target, keep_existing = free_path(target, taken), True
        elif not os.path.lexists(target):
            keep_existing = True
        urls = [u.get("url") for u in media.urls if u.get("url")] or [media.url]
        self._queue.append(_Save(media.hash, media.size, target, urls, opener, keep_existing,
                                 batch))

    def cancel(self) -> None:
        """Stop the save running and drop the ones waiting."""
        if self._download is None:
            self._queue = []
            return
        self._queue = self._queue[:1]
        self._download.cancel()

    def _next(self) -> None:
        while self._queue and self._download is None:
            save = self._queue[0]
            cached = self._loader.cache_path(save.sha)
            if cached.is_file():
                self._queue.pop(0)
                self.started.emit(save.target)
                self._done(save, self._save_cached(save, cached))
                continue
            self._percent = 0
            self._download = self._loader.download_to(
                save.urls[0], save.sha, save.target, size=save.size, transform=save.opener,
                keep_existing=save.keep_existing)
            self._download.progress.connect(self._on_progress)
            self._download.finished.connect(self._on_finished)
            self.started.emit(save.target)

    @staticmethod
    def _save_cached(save: _Save, cached) -> str:
        try:
            data = cached.read_bytes()
        except OSError:
            data = b""
        if hashlib.sha256(data).hexdigest() != save.sha:
            return "unreadable"
        if save.opener is not None:
            data = save.opener(data)
            if data is None:
                return "key"
        if save.keep_existing:
            save.target = free_path(save.target)
        try:
            save_document(save.target, data)
        except OSError as exc:
            return write_problem(exc)
        return ""

    def _on_progress(self, received: int, total: int) -> None:
        if total > 0:
            self._percent = min(100, received * 100 // total)
            self.progress.emit(self._percent)

    def _on_finished(self, why: str) -> None:
        save = self._queue[0]
        save.target = os.fspath(self._download.target)
        self._download.deleteLater()
        self._download = None
        if why in _NEXT_SERVER and len(save.urls) > 1:
            save.urls = save.urls[1:]
            self._next()
            return
        self._queue.pop(0)
        self._done(save, why)
        self._next()

    def _done(self, save: _Save, why: str) -> None:
        self.finished.emit(save.target, why)
        batch = save.batch
        if batch is None:
            return
        batch.left -= 1
        if why and why != "cancelled":
            batch.failures.append((save.target, why))
        if batch.left == 0 and why != "cancelled":
            self.batch_finished.emit(batch.total, list(batch.failures))


def save_failure(why: str, path: str) -> str:
    """Why the file saved at ``path`` wasn't, in a sentence."""
    said = {
        "network": _("“{name}” couldn’t be downloaded: check your internet connection "
                     "and try again."),
        "missing": _("“{name}” isn’t on your media servers anymore, so it couldn’t be "
                     "saved."),
        "server": _("“{name}” couldn’t be downloaded, as your media server didn’t send it: "
                    "try again later."),
        "too_large": _("“{name}” wasn’t saved, as a media server sent more than the file: "
                       "try again later."),
        "wrong_file": _("“{name}” wasn’t saved, as a media server returned the wrong file: "
                        "try again later."),
        "unsafe": _("“{name}” wasn’t saved, as a media server sent it from an address "
                    "MyEditor doesn’t trust: try again later."),
        "key": _("“{name}” wasn’t saved: it’s kept privately, and its key doesn’t open it."),
        "full": _("“{name}” couldn’t be saved, as there isn’t enough space for it there: "
                  "free up some space or choose another folder."),
        "unreadable": _("“{name}” couldn’t be saved: try again."),
        "name": _("“{name}” couldn’t be saved, as its name is too long: choose a shorter "
                  "one."),
    }.get(why) or _("“{name}” couldn’t be saved there: choose another folder.")
    return said.format(name=os.path.basename(path))


def saves_failure(total: int, failures: list) -> Tuple[str, str]:
    """What a Save to Folder that didn't save every file says at its end,
    and every reason, one per line, for its tooltip."""
    reasons = [save_failure(why, path) for path, why in failures]
    # What did not happen, never how many of how many (lessons 36).
    head = (_("Your file wasn’t saved") if total == 1
            else _("Your files weren’t saved") if len(failures) >= total
            else _("Some files weren’t saved"))
    return f"{head}. {reasons[0]}", "\n".join(reasons)
