# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Site icons for the sources in the Imports window (C12).

A source shows its site's icon once it is known, and its letter until
then (nostr/ui/imports_glyphs.py). :class:`FaviconCache` finds the icon
the way a browser does: the icons the home page names in its head
(``<link rel="icon">``, ``apple-touch-icon``), the best four of those on
the site's own host or its subdomains, else ``/favicon.ico``.

Every request goes through the importer's network guard, pinned to the
address it checked (nostr/imports/netguard.py), and an icon may be 64 KiB
at most. It is decoded by the app's image rules (image_safety.py; SVG is
never rendered), with ICO, the format of ``/favicon.ico``, decoded here
under the same limits. What is found is kept as a small PNG in
``~/.cache/my_editor/favicons/<sha256 of the site>.png`` and looked for
again after 30 days; a site without a usable icon is asked again after
a week. A lookup that nothing answered (offline) leaves no mark: it is
tried again after a few minutes, or at the next start. Within a session
a site is looked up once a day at most, whether or not the cache can be
written. Two sites are looked up at a time.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import weakref
from html import unescape
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import shiboken6
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    Qt,
    Signal,
)
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtNetwork import QNetworkAccessManager

import atomic_file
import image_safety
import user_dirs

from .netguard import Fetched, NetGuard

_log = logging.getLogger(__name__)

MAX_ICON_BYTES = 64 * 1024
MAX_PAGE_BYTES = 1024 * 1024
ICON_PIXELS = 64                       # kept at this size, drawn at 16 to 32
REFRESH_DAYS = 30
RETRY_DAYS = 7
AT_ONCE = 2
NAMED_AT_MOST = 4                      # icons a page names that are tried
ONCE_A_DAY = 24 * 3600                 # a site looked up, within a session
OFFLINE_PAUSE = 10 * 60                # after a lookup nothing answered
_USER_AGENT = b"MyEditor (site icon)"

_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_ICO = b"\x00\x00\x01\x00"
_FEEDISH = re.compile(r"(\.(xml|rss|atom|json|rdf)$)|(/(feed|rss|atom)/?$)", re.IGNORECASE)

Get = Callable[[str, int, Callable[[Fetched], None]], None]


def site_of(url: str) -> str:
    """The site an address belongs to: ``scheme://host[:port]``, or "" for
    one that is not on the web (a Nostr author)."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def icons_named(page: str, page_url: str) -> List[str]:
    """The icons a page's head names, best first: a PNG or a large one
    before an ICO, an apple-touch-icon before what is left. SVG is left
    out (never rendered here)."""
    head = page[: page.lower().find("</head>")] if "</head>" in page.lower() else page[:65536]
    found = []
    for tag in _LINK.findall(head):
        attributes = {m.group(1).lower(): unescape(m.group(3) or m.group(4) or m.group(5) or "")
                      for m in _ATTRIBUTE.finditer(tag)}
        rel = attributes.get("rel", "").lower().split()
        href = attributes.get("href", "").strip()
        if not href or not ({"icon", "apple-touch-icon", "apple-touch-icon-precomposed"}
                            & set(rel)):
            continue
        kind = attributes.get("type", "").lower()
        if "svg" in kind or href.lower().split("?")[0].endswith(".svg"):
            continue
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", attributes.get("sizes", ""))]
        largest = max(sizes) if sizes else 0
        touch = "icon" not in rel
        ico = kind.endswith("icon") or href.lower().split("?")[0].endswith(".ico")
        rank = (0 if 32 <= largest <= 256 else 1, 1 if ico else 0, 1 if touch else 0)
        found.append((rank, len(found), urljoin(page_url, href)))
    return [url for _rank, _n, url in sorted(found)]


def own_icons(candidates: List[str], site: str) -> List[str]:
    """The best few of the icons a page names that are the site's own: on
    its host or a subdomain of it ("www." aside), or inline. A page naming
    hundreds of icons on other hosts kept a lookup busy for hours and sent
    the person's address to each of them (CP3 review L11)."""
    host = (urlsplit(site).hostname or "").removeprefix("www.")

    def own(url: str) -> bool:
        if url.startswith("data:"):
            return True
        other = urlsplit(url).hostname or ""
        return other == host or other.endswith("." + host)

    return [url for url in candidates if own(url)][:NAMED_AT_MOST]


def _answered(fetched: Fetched) -> bool:
    """Whether the site said something: an answer of any status, or an
    address the guard refuses (it stays refused). Not a name that did not
    resolve or a connection that failed."""
    return fetched.status > 0 or bool(fetched.refused)


def decode_icon(data: bytes) -> Optional[QImage]:
    """An icon's pixels, by the app's image rules, ICO included."""
    if not data or len(data) > MAX_ICON_BYTES:
        return None
    if not data.startswith(_ICO):
        return image_safety.decode_image_bytes(data)
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        reader = QImageReader(buffer)
        reader.setAutoDetectImageFormat(False)
        reader.setDecideFormatFromContent(False)
        reader.setFormat(QByteArray(b"ico"))
        best: Optional[QImage] = None
        # An ICO holds several sizes; the largest up to 256 is the sharpest.
        for _ in range(max(1, min(reader.imageCount(), 16))):
            image = reader.read()
            if image is not None and not image.isNull() and image.width() <= 256 and (
                    best is None or image.width() > best.width()):
                best = image
            if not reader.jumpToNextImage():
                break
    finally:
        buffer.close()
    return best


def _later(cache: "FaviconCache", method: str, *args) -> Callable[[Fetched], None]:
    """What a request calls when it ends, holding the cache weakly: the
    request is a child of the cache's network manager, and a callback
    holding the cache would close a reference cycle through them, which
    only the cycle collector frees, in an order Qt does not survive."""
    ref = weakref.ref(cache)

    def answered(fetched: Fetched) -> None:
        alive = ref()
        if alive is not None and shiboken6.isValid(alive):
            getattr(alive, method)(*args, fetched)
    return answered


class FaviconCache(QObject):
    """Site icons, found once and kept on this computer.

    Signals:
      ready(str)   the icon of a site (its :func:`site_of`) is known now
    """

    ready = Signal(str)

    def __init__(self, cache_dir: Optional[Path] = None, *, get: Optional[Get] = None,
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._dir = Path(cache_dir) if cache_dir else user_dirs.cache_dir() / "favicons"
        self._clock = clock
        if get is None:
            guard = NetGuard()
            nam = QNetworkAccessManager(self)

            def get(url: str, max_bytes: int, on_done: Callable[[Fetched], None]) -> None:
                guard.get(nam, url, headers=[(b"User-Agent", _USER_AGENT)],
                          max_bytes=max_bytes, on_done=on_done)
        self._get = get
        self._images: Dict[str, Optional[QImage]] = {}
        self._queue: List[tuple] = []        # (site, address) waiting
        self._waiting: set = set()
        self._running: set = set()
        # When each site may be looked up again in this session, whatever
        # the cache holds (CP3 review L12: a cache that could not be written
        # started a lookup on every repaint).
        self._not_before: Dict[str, float] = {}

    # -- asking ---------------------------------------------------------------

    def icon(self, url: str) -> Optional[QImage]:
        """The icon of the site ``url`` is on, when it is known; looked up
        (once, in the background) when it is not, or is old."""
        site = site_of(url)
        if not site:
            return None
        if site not in self._images:
            self._images[site] = self._read(site)
        if self._due(site):
            self._enqueue(site, url)
        return self._images.get(site)

    # -- the cache --------------------------------------------------------------

    def _path(self, site: str, suffix: str = ".png") -> Path:
        return self._dir / (hashlib.sha256(site.encode("utf-8")).hexdigest() + suffix)

    def _read(self, site: str) -> Optional[QImage]:
        path = self._path(site)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return image_safety.decode_image_bytes(data)

    def _due(self, site: str) -> bool:
        if site in self._waiting or site in self._running:
            return False
        now = self._clock()
        if now < self._not_before.get(site, 0):
            return False
        for suffix, days in ((".png", REFRESH_DAYS), (".none", RETRY_DAYS)):
            try:
                age = now - os.path.getmtime(self._path(site, suffix))
            except OSError:
                continue
            return age > days * 24 * 3600
        return True

    def _keep(self, site: str, image: Optional[QImage]) -> None:
        """Keep what was found: the icon as a PNG, or a mark that there is
        none; either dated now, which says when to ask again."""
        now = self._clock()
        try:
            if image is None:
                path = self._path(site, ".none")
                atomic_file.write_bytes(path, b"")
            else:
                scaled = image.scaled(ICON_PIXELS, ICON_PIXELS,
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                scaled.save(buffer, "PNG")
                path = self._path(site)
                atomic_file.write_bytes(path, bytes(buffer.data()))
                try:
                    os.remove(self._path(site, ".none"))
                except OSError:
                    pass
            os.utime(path, (now, now))
        except OSError:
            _log.warning("could not keep the site icon of %s", site, exc_info=True)

    # -- looking it up -------------------------------------------------------------

    def _enqueue(self, site: str, url: str) -> None:
        self._waiting.add(site)
        self._queue.append((site, url))
        self._next()

    def _next(self) -> None:
        while self._queue and len(self._running) < AT_ONCE:
            site, url = self._queue.pop(0)
            self._waiting.discard(site)
            self._running.add(site)
            self._look_up(site, url)

    def _look_up(self, site: str, url: str) -> None:
        # The page the address names, unless it is a feed: then the site's
        # home page.
        home = site + "/" if _FEEDISH.search(urlsplit(url).path or "/") else url
        self._get(home, MAX_PAGE_BYTES, _later(self, "_page_read", site, home))

    def _page_read(self, site: str, home: str, fetched: Fetched) -> None:
        candidates = []
        if fetched.ok and 200 <= fetched.status < 300 and fetched.data:
            text = fetched.data[:MAX_PAGE_BYTES].decode("utf-8", errors="replace")
            candidates = own_icons(icons_named(text, fetched.url or home), site)
        candidates.append(site + "/favicon.ico")
        self._try(site, list(dict.fromkeys(candidates)), _answered(fetched))

    def _try(self, site: str, candidates: List[str], answered: bool) -> None:
        """Try the icons in turn; ``answered`` says whether the site has
        said anything so far in this lookup."""
        if not candidates:
            self._found(site, None, answered=answered)
            return
        first, rest = candidates[0], candidates[1:]
        if first.startswith("data:"):
            image = decode_icon(image_safety.data_uri_bytes(first) or b"")
            if image is not None:
                self._found(site, image)
            else:
                self._try(site, rest, answered)
            return
        self._get(first, MAX_ICON_BYTES, _later(self, "_icon_read", site, rest, answered))

    def _icon_read(self, site: str, rest: List[str], answered: bool,
                   fetched: Fetched) -> None:
        image = decode_icon(fetched.data) if fetched.ok and 200 <= fetched.status < 300 else None
        if image is not None:
            self._found(site, image)
        else:
            self._try(site, rest, answered or _answered(fetched))

    def _found(self, site: str, image: Optional[QImage], *, answered: bool = True) -> None:
        """The lookup of ``site`` ended. Only an icon, or a site that said
        it has none, is kept: after a lookup nothing answered (offline)
        the site is asked again after a pause, never taken for one without
        an icon for a week (CP3 review L12)."""
        self._running.discard(site)
        now = self._clock()
        if image is None and not answered:
            self._not_before[site] = now + OFFLINE_PAUSE
            self._next()
            return
        self._not_before[site] = now + ONCE_A_DAY
        self._keep(site, image)
        if image is not None:
            self._images[site] = self._read(site) or image
            self.ready.emit(site)
        self._next()
