# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pictures from the web, for an export that carries its pictures.

An exported page holds the pictures of its photo groups as their bytes (a
data: address), so it shows the same on a train without a network, as
it does on the desk. A picture the document names by a web address comes
from what the window already holds (the editor showed it); any other is
fetched here, through the importer's guarded fetcher: no address on this
computer or on its network (nostr/imports/netguard.py), at most
MAX_BYTES a picture, and TIMEOUT_MS for all of them together. A picture
that does not come in time stays its address, as it was.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from PySide6.QtCore import QEventLoop, QTimer

import photo_group
from image_safety import sniff_image_mime

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
    """The web addresses of the pictures in the photo groups of ``doc``,
    without #wide, each once, in order."""
    found: List[str] = []
    for group in photo_group.groups_in(doc):
        for unit in group.units:
            url = photo_group.without_wide(unit.name)
            if is_web_address(url) and url not in found:
                found.append(url)
    return found


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
