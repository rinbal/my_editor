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
TIMEOUT_MS for all of them together. A picture that does not come in
time is not waited for: a page keeps its address, paper a quiet
stand-in with its description.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from PySide6.QtCore import QEventLoop, QTimer

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


def gather(urls: Iterable[str], *, held: Optional[HeldBytes] = None, fetcher=None,
           timeout_ms: int = TIMEOUT_MS) -> Dict[str, bytes]:
    """The bytes of each picture at ``urls`` that is a picture: from
    ``held`` first, the rest from ``fetcher`` (a BlobFetcher; by default
    one capped at MAX_BYTES), waiting at most ``timeout_ms`` for all."""
    out: Dict[str, bytes] = {}
    wanted: List[str] = []
    for url in urls:
        data = held(url) if held is not None else None
        if data and sniff_image_mime(data):
            out[url] = data
        elif is_web_address(url):
            wanted.append(url)
    if not wanted:
        return out
    if fetcher is None:
        from nostr.imports.fetch import BlobFetcher
        fetcher = BlobFetcher(max_bytes=MAX_BYTES)
    loop = QEventLoop()
    pending = set(wanted)
    # A picture that comes after the wait is over is not used.
    over = [False]

    def done(url: str, data: Optional[bytes]) -> None:
        if over[0]:
            return
        if data and len(data) <= MAX_BYTES and sniff_image_mime(data):
            out[url] = bytes(data)
        pending.discard(url)
        if not pending:
            loop.quit()

    for url in wanted:
        fetcher.fetch(url, on_success=lambda data, _mime, u=url: done(u, data),
                      on_failure=lambda _reason, u=url: done(u, None))
    if pending:
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(max(1, int(timeout_ms)))
        loop.exec()
        timer.stop()
    over[0] = True
    return out
