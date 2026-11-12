# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A picture's size and its tags on release (contract 5, vector
``picture_imeta``).

A picture entry of ``media[]`` may carry ``w`` and ``h``: its upright
pixel size, after its EXIF orientation, integers from 1 to 65,535, both
or neither, never on audio. The writer takes them from the file it
encrypts (:func:`upright_size`, which reads only the file's header).
The server never sees them, so a reader judges the pair
(:func:`picture_size`): a bad one gives the picture no size, and nothing
else is refused for it.

The kind 30023 that releases a revision carries one NIP-92 ``imeta`` per
picture entry, in ``media[]`` order (:func:`picture_imeta`): ``url`` the
public address that replaces the entry's ref, ``m``, ``x`` the plain
file's hash, ``dim`` when it has a size, ``alt`` tidied and left out
when empty; one per address.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageIOHandler, QImageReader

from . import formats

MAX_SIDE = 65535
# The pictures a revision may hold (5); an audio entry has no size.
PICTURE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})
# Runs of U+0020, U+0009, U+000D and U+000A become one space in alt (5).
_ALT_SPACE = re.compile("[ \t\r\n]+")


def upright_size(data: bytes) -> Optional[Tuple[int, int]]:
    """``(w, h)`` of the picture ``data`` as it is shown, after its EXIF
    orientation, read from its header only (nothing is decoded); None
    when it cannot be told or a side is outside 1..65535."""
    buffer = QBuffer()
    buffer.setData(QByteArray(bytes(data)))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid():
            return None
        w, h = size.width(), size.height()
        # size() is the stored size; a quarter turn shows it the other way.
        if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
            w, h = h, w
    finally:
        buffer.close()
    return (w, h) if _side(w) and _side(h) else None


def _side(value: Any) -> bool:
    # bool is an int in Python: True is no width of 1.
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_SIDE


def picture_size(entry: Mapping) -> Optional[Tuple[int, int]]:
    """``(w, h)`` of a media entry as a reader takes it: both, each an
    integer from 1 to 65,535, on a picture; anything else is no size."""
    if entry.get("mime") not in PICTURE_MIMES:
        return None
    w, h = entry.get("w"), entry.get("h")
    return (w, h) if _side(w) and _side(h) else None


def tidy_alt(text: Any) -> str:
    """``alt`` as a release writes it: every run of spaces, tabs and line
    breaks one space, trimmed."""
    return _ALT_SPACE.sub(" ", text).strip(" ") if isinstance(text, str) else ""


def picture_imeta(media: Sequence[Mapping], urls: Mapping[str, str]) -> List[List[str]]:
    """One ``imeta`` per picture entry of ``media`` that ``urls`` gives a
    public address (``{ref: url}``), in their order, one per address."""
    tags: List[List[str]] = []
    seen = set()
    for entry in media:
        url = urls.get(entry.get("ref")) if isinstance(entry.get("ref"), str) else None
        if entry.get("mime") not in PICTURE_MIMES or not url or url in seen \
                or not formats.is_hex64(entry.get("plain_sha256")):
            continue
        seen.add(url)
        tag = ["imeta", f"url {url}", f"m {entry['mime']}", f"x {entry['plain_sha256']}"]
        size = picture_size(entry)
        if size is not None:
            tag.append(f"dim {size[0]}x{size[1]}")
        alt = tidy_alt(entry.get("alt"))
        if alt:
            tag.append(f"alt {alt}")
        tags.append(tag)
    return tags


def imeta_url(tag: Sequence[str]) -> str:
    """The address an ``imeta`` describes, or ``""``."""
    return next((part[4:] for part in tag[1:] if isinstance(part, str)
                 and part.startswith("url ")), "")


def one_per_address(tags: Sequence[List[str]]) -> List[List[str]]:
    """``tags`` with only the first ``imeta`` of each address (92.md:
    one per URL); every other tag as it is."""
    kept, seen = [], set()
    for tag in tags:
        url = imeta_url(tag) if tag[:1] == ["imeta"] else ""
        if url and url in seen:
            continue
        seen.add(url)
        kept.append(tag)
    return kept


__all__ = ["MAX_SIDE", "PICTURE_MIMES", "imeta_url", "one_per_address", "picture_imeta",
           "picture_size", "tidy_alt", "upright_size"]
