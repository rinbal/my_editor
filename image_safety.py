#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image sniffing, allowlisted decoding, and trusted-root file reads.

Two boundaries live here.

Decoding: Qt's own sniffing hands SVG data to the QtSvg renderer, which
resolves ``xlink:href`` against the local filesystem. There is no option
on this Qt to disable that, so the only closing move is an explicit
format allowlist: bytes are sniffed by magic number, refused unless the
type is in :data:`DECODABLE_MIMES`, and then read by a QImageReader with
auto-detection switched off and the format forced. A pixel budget stops
a header that claims 30000x30000 from allocating gigabytes.

Reading: an exporter embeds files named by the document, and a document
can name any path on the machine. :class:`ImageRootPolicy` restricts
those reads to a caller-supplied set of directories, canonicalised with
``realpath`` so a symlink planted beside a hostile document cannot walk
out of the root.

The sniffers live here rather than in ``export_html`` because both
boundaries need them; ``export_html`` re-exports them for its callers.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, unquote_to_bytes, urlsplit
from urllib.request import url2pathname

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader


_MAGIC_MIMES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_image_mime(data: bytes) -> str | None:
    """Detect an image MIME type from magic bytes.

    Needed because Blossom cache files are named by bare sha256 with no
    extension. Returns None for unrecognized data.
    """
    for magic, mime in _MAGIC_MIMES:
        if data.startswith(magic):
            return mime
    # WebP: RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # SVG is text; look for an <svg root near the start.
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        if b"<svg" in data[:2048]:
            return "image/svg+xml"
    return None


_EXT_FOR_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


def sniff_image_ext(data: bytes) -> str | None:
    """File extension for image data, or None when unrecognized."""
    mime = sniff_image_mime(data)
    return _EXT_FOR_MIME.get(mime) if mime else None


# Formats this process will decode. SVG is deliberately absent: the Qt
# SVG renderer reads local files named by the document it is rendering,
# and blob bytes and stranger's avatars both reach this decoder. SVG is
# still sniffed, still cached, and still passes through exports as
# bytes; it is only never handed to a renderer.
DECODABLE_MIMES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
})

_QT_FORMAT_FOR_MIME = {
    "image/png": b"png",
    "image/jpeg": b"jpeg",
    "image/gif": b"gif",
    "image/webp": b"webp",
    "image/bmp": b"bmp",
}

# Decompression budget: a 50 megapixel image is already far past
# anything a document needs, and at 4 bytes per pixel it is 200 MB of
# RAM. A header claiming more is refused before a single pixel is read.
MAX_DECODE_PIXELS = 50_000_000


# Names that are already an address anyone can resolve. A document
# reopened from HTML names every embedded image by its whole data: URI,
# and media this app did not create keeps whatever URL it arrived with.
_PORTABLE_PREFIXES = ("http://", "https://", "data:")


def is_portable_image_source(name: str) -> bool:
    """Whether an image name is an address rather than a local path.

    Such a name travels with the document: it is never rewritten on
    save and never read from the filesystem.
    """
    return bool(name) and name.lower().startswith(_PORTABLE_PREFIXES)


def data_uri_bytes(name: str) -> Optional[bytes]:
    """Bytes carried by a ``data:`` URI, or None when it is not one.

    The payload is in the name itself, so nothing is read from disk and
    nothing is fetched. Callers still hand the result to
    :func:`decode_image_bytes`; a data: URI is as untrusted as any
    other document content.
    """
    if not name or not name.lower().startswith("data:"):
        return None
    head, sep, payload = name.partition(",")
    if not sep:
        return None
    if head[5:].lower().endswith(";base64"):
        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            return None
    try:
        return unquote_to_bytes(payload)
    except (UnicodeEncodeError, ValueError):
        return None


def decode_image_bytes(data: bytes) -> Optional[QImage]:
    """Decode ``data`` to a QImage, or None when the policy refuses it.

    Refusal reasons: unrecognised magic, a format outside
    :data:`DECODABLE_MIMES`, a declared size past
    :data:`MAX_DECODE_PIXELS`, or a decoder failure.
    """
    if not data:
        return None
    mime = sniff_image_mime(data)
    if mime is None or mime not in DECODABLE_MIMES:
        return None
    fmt = _QT_FORMAT_FOR_MIME.get(mime)
    if fmt is None:
        return None

    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.ReadOnly):
        return None
    try:
        reader = QImageReader(buffer)
        # Sniffing is what hands SVG (and any future format Qt learns)
        # to a renderer this policy never approved; force the format the
        # magic bytes actually claimed instead.
        reader.setAutoDetectImageFormat(False)
        reader.setDecideFormatFromContent(False)
        reader.setFormat(QByteArray(fmt))
        size = reader.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return None
        if size.width() * size.height() > MAX_DECODE_PIXELS:
            return None
        image = reader.read()
    finally:
        buffer.close()
    if image is None or image.isNull():
        return None
    return image


class ImageRootPolicy:
    """Deny-by-default local reads for image names taken from a document.

    ``roots`` are the only directories a name may resolve into. An empty
    policy refuses everything, so a caller that forgets to pass roots
    leaks nothing.
    """

    def __init__(self, roots: Iterable[str | Path] = ()) -> None:
        canonical: list[str] = []
        for root in roots or ():
            if not root:
                continue
            try:
                real = os.path.realpath(str(root))
            except OSError:
                continue
            if real not in canonical:
                canonical.append(real)
        self._roots = canonical

    @property
    def roots(self) -> list[str]:
        return list(self._roots)

    def resolve(self, name: str) -> Optional[Path]:
        """Map an image name to a path inside a root, or None.

        Handles the shapes a ``QTextImageFormat.name()`` takes: a bare
        absolute path, a ``file://`` URL, and a relative path (resolved
        against the first root, which callers set to the document's own
        directory). Any other scheme, http(s) and ``data:`` included,
        resolves to nothing: those are not local reads.
        """
        if not name or not self._roots:
            return None
        candidate = self._candidate_path(name)
        if candidate is None:
            return None
        try:
            real = os.path.realpath(candidate)
        except OSError:
            return None
        # realpath before the containment test is what defeats a symlink
        # planted beside a hostile document.
        real_path = Path(real)
        for root in self._roots:
            try:
                inside = real_path.is_relative_to(root)
            except ValueError:
                continue
            if inside:
                return real_path if real_path.is_file() else None
        return None

    def read(self, name: str) -> Optional[bytes]:
        """Bytes of ``name`` when it resolves inside a root, else None."""
        path = self.resolve(name)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _candidate_path(self, name: str) -> Optional[str]:
        try:
            parts = urlsplit(name)
        except ValueError:
            return None
        scheme = (parts.scheme or "").lower()
        if scheme == "file":
            if parts.netloc and parts.netloc.lower() not in ("", "localhost"):
                return None  # a UNC target, not a local file
            return url2pathname(unquote(parts.path))
        # A single character is a Windows drive letter, not a scheme.
        if len(scheme) > 1:
            return None
        path = Path(name)
        if path.is_absolute():
            return str(path)
        return str(Path(self._roots[0]) / name)
