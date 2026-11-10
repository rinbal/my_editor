# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pictures of an export that carries them: where their bytes come from.

An exported page, a PDF and a printed page hold their pictures, so each
shows the same on a train without a network as it does on the desk.
``picture_bytes`` is the one place an export reads a picture: the app's
own pictures, a web picture gathered here, a file in the document's
trusted folders, or a data: address. A picture the document names by a
web address comes from what the window already holds (the editor showed
it); any other is fetched here, through the importer's guarded fetcher:
no address on this computer or on its network, a redirect to one
included (nostr/imports/netguard.py), at most MAX_BYTES a picture, and
TIMEOUT_MS for all of them together, while the app goes on (Gathering,
never a nested event loop: a tab closed or an edit made meanwhile meets
no export half done). A picture that does not come in time is not
waited for: a page keeps its address, paper a quiet stand-in with its
description.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

import doc_walk
import photo_group
from image_safety import MAX_PICTURE_BYTES, data_uri_bytes, sniff_image_mime

# One picture: what the window's own picture cache takes
# (nostr/imports/remote_images.py, MAX_BYTES).
MAX_BYTES = 8 * 1024 * 1024
# All pictures of one export together.
TIMEOUT_MS = 20 * 1000

# url -> bytes the window holds already, or None.
HeldBytes = Callable[[str], Optional[bytes]]


def is_web_address(name: str) -> bool:
    return name.lower().startswith(("http://", "https://"))


def web_picture_names(doc) -> List[str]:
    """The web addresses of the pictures of ``doc`` (in its photo groups
    and on their own), without #wide, each once, in order."""
    found: Dict[str, None] = {}
    for name in doc_walk.iter_image_names(doc):
        url = photo_group.without_wide(name)
        if is_web_address(url):
            found.setdefault(url, None)
    return list(found)


def picture_bytes(name: str, policy, *, asset_resolver=None,
                  remote_pictures: Optional[Dict[str, bytes]] = None):
    """``(bytes, web address)`` of the picture ``name`` names, read
    without its #wide, for an export; ``(None, None)`` when there is
    nothing the export may carry. In order: the app's own pictures
    (``asset_resolver``, with the address they were uploaded to), a web
    picture the caller gathered, a file ``policy`` (an ImageRootPolicy)
    may read, never larger than MAX_PICTURE_BYTES, and a data: address."""
    plain = photo_group.without_wide(name)
    resolved = asset_resolver(plain) if asset_resolver else None
    if resolved is not None and getattr(resolved, "data", b""):
        return bytes(resolved.data), str(getattr(resolved, "remote_url", "") or "") or None
    if remote_pictures and plain in remote_pictures:
        return remote_pictures[plain], plain
    data = policy.read(plain, limit=MAX_PICTURE_BYTES) or data_uri_bytes(plain)
    if data and len(data) <= MAX_PICTURE_BYTES:
        return data, None
    return None, None


def new_fetcher(parent: QObject):
    """The importer's guarded fetcher, capped at MAX_BYTES a picture; a
    child of ``parent``, so what is still under way goes with it."""
    from nostr.imports.fetch import BlobFetcher
    return BlobFetcher(parent, max_bytes=MAX_BYTES)


class Gathering(QObject):
    """The web pictures at ``urls`` for one export: ``pictures`` holds
    what came, held ones (``held``) at once, the rest from ``fetcher`` (a
    BlobFetcher; by default one capped at MAX_BYTES). ``finished`` once
    every one came or failed, or ``timeout_ms`` after start(), whichever
    is first; never after cancel(). ``complete``: nothing to fetch."""

    finished = Signal()

    def __init__(self, urls: Iterable[str], *, held: Optional[HeldBytes] = None,
                 fetcher=None, timeout_ms: int = TIMEOUT_MS, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.pictures: Dict[str, bytes] = {}
        self._wanted: List[str] = []
        for url in urls:
            data = held(url) if held is not None else None
            if data and sniff_image_mime(data):
                self.pictures[url] = data
            elif is_web_address(url):
                self._wanted.append(url)
        self._fetcher = fetcher
        self._pending: set = set()
        self._running = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(1, int(timeout_ms)))
        self._timer.timeout.connect(self._finish)

    @property
    def complete(self) -> bool:
        return not self._wanted

    def start(self) -> None:
        if self._running or not self._wanted:
            return
        if self._fetcher is None:
            self._fetcher = new_fetcher(self)
        self._running = True
        self._pending = set(self._wanted)
        self._timer.start()
        for url in self._wanted:
            if not self._running:
                break
            self._fetcher.fetch(url, on_success=lambda data, _mime, u=url: self._came(u, data),
                                on_failure=lambda _reason, u=url: self._came(u, None))

    def cancel(self) -> None:
        self._running = False
        self._timer.stop()

    def _came(self, url: str, data: Optional[bytes]) -> None:
        if not self._running:
            return
        if data and len(data) <= MAX_BYTES and sniff_image_mime(data):
            self.pictures[url] = bytes(data)
        self._pending.discard(url)
        if not self._pending:
            self._finish()

    def _finish(self) -> None:
        if not self._running:
            return
        self.cancel()
        self.finished.emit()
