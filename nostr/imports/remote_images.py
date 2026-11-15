# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images of posts that are not imported yet: covers, thumbnails, icons.

The Imports window shows each post's cover in the list, its images in
the article, and each source's site icon. All of them are addresses a
feed named, so they are fetched like everything else the importer
fetches: through the network guard (netguard.py: nothing on the local
network, redirects checked) and :class:`BlobFetcher` (size cap), then
decoded only through the image allow-list (image_safety.py).

What it costs is bounded, because a long list, or a hostile feed, would
otherwise fill the memory with full-size photos:

- An image is decoded off the UI thread, and no larger than the view
  that asked needs (a 64 px cover of a 4000 px photo costs 64 px); a
  view that needs it larger later gets a sharper decode from the bytes
  kept for a while, without fetching again.
- What is kept has a byte budget (``MAX_CACHE_BYTES`` of pixels,
  ``MAX_RAW_BYTES`` of downloaded bytes); the least recently used goes.
- The article's images go first (``urgent``); covers are served newest
  request first, so the rows on screen come before rows scrolled past,
  and only the newest ``MAX_QUEUED`` wait at all.

An image is known by one form of its address (fully percent-encoded),
so "B%C3%A4ume.jpg" and "Bäume.jpg" are the same image; ``ready`` names
the address the way each view asked for it. :meth:`image` answers from
memory, never from the network; :meth:`request` fetches. A failed
address is remembered for the session and not asked again.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from typing import Callable, Deque, Dict, Optional, Set, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, QUrl, Signal
from PySide6.QtGui import QImage, QImageReader

from image_safety import decode_image_bytes

from . import workers
from .fetch import BlobFetcher

_log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024            # one download
MAX_CACHE_BYTES = 64 * 1024 * 1024     # decoded pixels kept
MAX_RAW_BYTES = 24 * 1024 * 1024       # downloaded bytes kept for a sharper decode
MAX_PARALLEL = 4
MAX_QUEUED = 160
# What a request without a size gets: plenty for a thumbnail or a column.
DEFAULT_SIZE = QSize(1200, 0)


def _key(url: str) -> str:
    """One form of an address, whichever way a view spelled it."""
    encoded = QUrl(url).toString(QUrl.ComponentFormattingOption.FullyEncoded)
    return encoded or url


def _larger(a: Optional[QSize], b: Optional[QSize]) -> QSize:
    if a is None:
        return QSize(b) if b is not None else QSize(DEFAULT_SIZE)
    if b is None:
        return QSize(a)
    return QSize(max(a.width(), b.width()), max(a.height(), b.height()))


def _decode(data: bytes, size: QSize) -> Tuple[Optional[QImage], QSize]:
    """In a worker: the image, decoded to cover ``size``, and its own size."""
    image = decode_image_bytes(data, at_least=size)
    full = QSize()
    if image is not None:
        reader = QImageReader()
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        if buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            reader.setDevice(buffer)
            full = reader.size()
            buffer.close()
    return image, full if full.isValid() else (image.size() if image is not None else QSize())


class RemoteImages(QObject):
    """Guarded, bounded image cache for the Imports window.

    Signals:
      ready(str)     an image arrived, or a sharper one (the address as
                     the view asked for it)
    """

    ready = Signal(str)

    def __init__(self, *, fetcher=None,
                 run_blocking: Optional[Callable] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._fetcher = fetcher or BlobFetcher(self, max_bytes=MAX_BYTES)
        self._run = run_blocking or (
            lambda fn, ok, err: workers.run_blocking(fn, ok, err, parent=self))
        self._images: "OrderedDict[str, QImage]" = OrderedDict()
        self._used = 0
        self._raw: "OrderedDict[str, bytes]" = OrderedDict()
        self._raw_used = 0
        self._full: Dict[str, QSize] = {}
        self._wanted: Dict[str, QSize] = {}
        self._names: Dict[str, Set[str]] = {}
        self._failed: Set[str] = set()
        self._busy: Set[str] = set()
        self._urgent: Deque[str] = deque()
        self._normal: Deque[str] = deque()

    # -- asking ------------------------------------------------------------

    def image(self, url: str) -> Optional[QImage]:
        """The image at ``url`` when it is here already."""
        if not url:
            return None
        key = _key(url)
        image = self._images.get(key)
        if image is not None:
            self._images.move_to_end(key)
        return image

    def raw(self, url: str) -> Optional[bytes]:
        """The bytes of the image at ``url`` as they came, while they are
        kept here (for an export that carries its pictures)."""
        return self._raw.get(_key(url)) if url else None

    def request(self, url: str, size: Optional[QSize] = None, *,
                urgent: bool = False) -> None:
        """Fetch ``url`` (decoded to cover ``size``) unless it is here at
        that size, on its way, or failed before. ``urgent`` puts it ahead
        of everything waiting (the article being read)."""
        if not url:
            return
        key = _key(url)
        self._names.setdefault(key, set()).add(url)
        self._wanted[key] = _larger(self._wanted.get(key), size)
        if key in self._failed or key in self._busy:
            return
        if self._sharp_enough(key):
            return
        if urgent:
            if key in self._normal:
                self._normal.remove(key)
            if key not in self._urgent:
                self._urgent.append(key)
        elif key not in self._urgent:
            if key in self._normal:
                self._normal.remove(key)
            self._normal.appendleft(key)
            while len(self._normal) > MAX_QUEUED:
                self._normal.pop()      # long scrolled past: asked again if seen
        self._pump()

    def kept_bytes(self) -> int:
        """Pixels kept, in bytes (within ``MAX_CACHE_BYTES``)."""
        return self._used

    # -- working -----------------------------------------------------------

    def _sharp_enough(self, key: str) -> bool:
        image = self._images.get(key)
        if image is None:
            return False
        full = self._full.get(key, image.size())
        if image.size() == full:
            return True        # it cannot get any sharper
        wanted = self._wanted.get(key, DEFAULT_SIZE)
        return image.width() >= wanted.width() and image.height() >= wanted.height()

    def _pump(self) -> None:
        while len(self._busy) < MAX_PARALLEL and (self._urgent or self._normal):
            key = self._urgent.popleft() if self._urgent else self._normal.popleft()
            if key in self._busy or key in self._failed or self._sharp_enough(key):
                continue
            self._busy.add(key)
            raw = self._raw.get(key)
            if raw is not None:
                self._raw.move_to_end(key)
                self._start_decode(key, raw)
            else:
                self._fetcher.fetch(key, on_success=lambda data, _mime, k=key: self._got(k, data),
                                    on_failure=lambda reason, k=key: self._lost(k, reason))

    def _got(self, key: str, data: bytes) -> None:
        self._raw[key] = data
        self._raw_used += len(data)
        while self._raw_used > MAX_RAW_BYTES and len(self._raw) > 1:
            _old, dropped = self._raw.popitem(last=False)
            self._raw_used -= len(dropped)
        self._start_decode(key, data)

    def _start_decode(self, key: str, data: bytes) -> None:
        size = QSize(self._wanted.get(key, DEFAULT_SIZE))
        self._run(lambda: _decode(data, size),
                  lambda result: self._decoded(key, size, *result),
                  lambda exc: self._decoded(key, size, None, QSize()))

    def _decoded(self, key: str, size: QSize, image: Optional[QImage], full: QSize) -> None:
        self._busy.discard(key)
        if image is None or image.isNull():
            self._failed.add(key)
            self._drop_raw(key)
            self._pump()
            return
        old = self._images.pop(key, None)
        if old is not None:
            self._used -= old.sizeInBytes()
        self._images[key] = image
        self._used += image.sizeInBytes()
        self._full[key] = full
        while self._used > MAX_CACHE_BYTES and len(self._images) > 1:
            _gone, dropped = self._images.popitem(last=False)
            self._used -= dropped.sizeInBytes()
        for name in sorted(self._names.get(key, {key})):
            self.ready.emit(name)
        if self._wanted.get(key) != size and not self._sharp_enough(key):
            # A view asked for it larger while this decode ran.
            self._urgent.append(key)
        self._pump()

    def _lost(self, key: str, reason: str = "") -> None:
        self._busy.discard(key)
        self._failed.add(key)
        _log.info("image not shown (%s): %s", reason or "unreadable", key[:200])
        self._pump()

    def _drop_raw(self, key: str) -> None:
        data = self._raw.pop(key, None)
        if data is not None:
            self._raw_used -= len(data)
