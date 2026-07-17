# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Blossom blob thumbnail loader.

Downloads image blobs to a disk cache and emits a ``QPixmap`` for the
caller (the media grid in the Library dialog, and the preview lightbox).

Cache layout: ~/.config/my_editor/blossom_cache/<sha256>
The filename is the content hash, so the cache is content-addressed and
never needs invalidation.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


CACHE_DIR = Path.home() / ".config" / "my_editor" / "blossom_cache"

# Hard upper bound on thumbnail downloads. The library is restricted to
# files the user uploaded themselves, so they can't accidentally pull a
# multi-gigabyte object — but we still guard against a malicious server
# returning an unbounded stream.
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB
_HTTP_TIMEOUT_MS = 30_000


class ThumbnailLoader(QObject):
    """Resolve a Blossom blob URL to a local file path + QPixmap.

    Always keyed by sha256; the URL is only used when the cache misses.
    Concurrent requests for the same hash coalesce.
    """

    ready = Signal(str, str, object)   # sha256, local_path, QPixmap
    failed = Signal(str, str)          # sha256, reason

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._nam = QNetworkAccessManager(self)
        self._inflight: Dict[str, QNetworkReply] = {}

    def cache_path(self, sha256: str) -> Path:
        return CACHE_DIR / sha256.lower()

    def load(self, sha256: str, url: str) -> None:
        """Asynchronously resolve the blob. Emits ``ready`` on success or
        ``failed`` on any error. Idempotent: a second call for the same
        hash while a request is in flight is a no-op (the in-flight reply
        will fire ``ready`` for both callers via signal broadcast)."""
        sha = sha256.lower()
        path = self.cache_path(sha)
        if path.is_file():
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.ready.emit(sha, str(path), pix)
                return
            # Corrupt cache entry — fall through and re-download.
            try:
                path.unlink()
            except OSError:
                pass
        if sha in self._inflight:
            return
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", b"my-editor-blossom-thumb/1")
        request.setTransferTimeout(_HTTP_TIMEOUT_MS)
        reply = self._nam.get(request)
        self._inflight[sha] = reply
        reply.finished.connect(
            lambda s=sha, r=reply, p=path: self._on_reply(s, r, p)
        )

    def _on_reply(self, sha: str, reply: QNetworkReply, path: Path) -> None:
        self._inflight.pop(sha, None)
        try:
            if reply.error() != QNetworkReply.NoError:
                self.failed.emit(sha, reply.errorString() or "network error")
                return
            data = bytes(reply.readAll())
            if not data:
                self.failed.emit(sha, "empty response")
                return
            if len(data) > _MAX_DOWNLOAD_BYTES:
                self.failed.emit(sha, "blob exceeds cache limit")
                return
            # Validate the bytes match the hash before trusting them.
            actual = hashlib.sha256(data).hexdigest()
            if actual != sha:
                self.failed.emit(sha, "downloaded bytes do not match sha256")
                return
            pix = QPixmap()
            if not pix.loadFromData(data):
                # Non-image blob — still cache it but tell the caller we
                # have no pixmap to show.
                tmp = path.with_suffix(path.suffix + ".tmp")
                try:
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
                except OSError:
                    pass
                self.failed.emit(sha, "not an image")
                return
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except OSError:
                pass
            self.ready.emit(sha, str(path), pix)
        finally:
            reply.deleteLater()
