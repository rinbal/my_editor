# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pictures from the disk, shown one safe way.

A picture in a library folder came from somewhere else, so it is read
only when it is a regular file, and only as far as a byte limit
(regular_files.py: a pipe or a symlink to ``/dev/zero`` with a
picture's name is never opened).

``read_picture`` is the one way the app turns a picture file into
pixels, for the picture tab, the file list's thumbnails and sizes, and
Quick Look alike:

- the formats image_safety sniffs by their first bytes are decoded as
  exactly that format (never a guess that could hand data to another
  decoder), within the pixel budget, turned the way the camera said;
- HEIC, HEIF and AVIF when this Qt has a plugin for them;
- an SVG only after everything in it that would make QtSvg read another
  file is taken out (QtSvg reads any local path an ``<image>`` names
  and has no switch to stop it), and only when what it would draw stays
  small: a few bytes of ``<use>`` elements that each repeat the one
  before ten times make billions of shapes, so the shapes a drawing
  would make are counted first and a drawing past USE_EXPANSION_LIMIT
  is refused.

All of it may run on a worker thread, and the tabs do run it there.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter

import image_safety
from regular_files import read_regular_file, read_regular_prefix

log = logging.getLogger(__name__)

# A picture file larger than this is not read at all.
PICTURE_FILE_LIMIT = 256 * 1024 * 1024
SVG_FILE_LIMIT = 16 * 1024 * 1024
# How much of a picture is read to learn its size from its header.
HEADER_BYTES = 1024 * 1024
# The longest side an SVG is drawn at.
SVG_RENDER_SIDE = 2048
# The most elements an SVG may draw once every <use> is counted out: a
# detailed map or drawing has tens of thousands, which QtSvg draws in
# well under a second; nested <use> elements reach billions in a few
# bytes.
USE_EXPANSION_LIMIT = 200_000

# Formats sniffed by their first bytes (image_safety's allowlist), by
# the name Qt gives each.
_SNIFFED_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif",
                    "image/webp": "webp", "image/bmp": "bmp"}
# Formats this Qt may or may not have a plugin for, by extension.
PLUGIN_FORMATS = {".heic": "heic", ".heif": "heif", ".avif": "avif"}

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_URL_REF = re.compile(r"url\(\s*['\"]?#([^)'\"\s]+)['\"]?\s*\)")


# --------------------------------------------------------------------------- #
# Decoding                                                                    #
# --------------------------------------------------------------------------- #

def qt_reads(fmt: str) -> bool:
    return fmt.encode() in {bytes(f) for f in QImageReader.supportedImageFormats()}


def svg_available() -> bool:
    try:
        importlib.import_module("PySide6.QtSvg")
    except ImportError:
        return False
    return True


def _reader(data: bytes, fmt: str):
    """A QImageReader for ``data`` that reads ``fmt`` and nothing else,
    and the buffer it reads from (keep both while reading)."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None, None
    reader = QImageReader(buffer, QByteArray(fmt.encode()))
    reader.setAutoDetectImageFormat(False)
    reader.setDecideFormatFromContent(False)
    return reader, buffer


def _decode_as(data: bytes, fmt: str, at_least: Optional[QSize]) -> Optional[QImage]:
    """``data`` decoded as ``fmt``, turned the way the camera said (EXIF),
    within the pixel budget; no larger than needed to cover ``at_least``."""
    reader, buffer = _reader(data, fmt)
    if reader is None:
        return None
    try:
        return image_safety.read_upright(reader, at_least)
    finally:
        buffer.close()


def _raster_format(data: bytes, ext: str) -> Optional[str]:
    """The one format ``data`` may be decoded as: the one its first bytes
    claim, or a plugin format named by its extension when they claim none."""
    mime = image_safety.sniff_image_mime(data)
    if mime in _SNIFFED_FORMATS:
        return _SNIFFED_FORMATS[mime]
    fmt = PLUGIN_FORMATS.get(ext)
    if fmt and mime is None and qt_reads(fmt):
        return fmt
    return None


# --------------------------------------------------------------------------- #
# SVG                                                                         #
# --------------------------------------------------------------------------- #

def _href(element) -> str:
    for key in (_XLINK_HREF, "href"):
        value = element.get(key)
        if value is not None:
            return value
    return ""


def _references(element) -> list:
    """The ids ``element`` draws again: a ``<use>``'s target and every
    ``url(#id)`` in its attributes (patterns, markers, masks)."""
    from lxml import etree
    found = []
    if etree.QName(element).localname == "use":
        href = _href(element)
        if href.startswith("#"):
            found.append(href[1:])
    for key, value in element.attrib.items():
        if key not in (_XLINK_HREF, "href") and "url(" in value:
            found.extend(_URL_REF.findall(value))
    return found


def _expansion_within(root, limit: int) -> bool:
    """Whether drawing ``root`` makes at most ``limit`` shapes, every
    ``<use>`` and ``url(#id)`` counted as a copy of what it names. A
    reference that leads back to itself counts as past the limit. The
    count is worked out once per element, without recursion, and stops
    as soon as it passes ``limit``."""
    ids = {}
    for element in root.iter():
        if isinstance(element.tag, str):
            ident = element.get("id")
            if ident and ident not in ids:
                ids[ident] = element
    cost: dict = {}
    visiting: set = set()
    stack = [(root, False)]
    while stack:
        element, expanded = stack.pop()
        if element in cost:
            continue
        children = [child for child in element if isinstance(child.tag, str)]
        targets = [ids[ident] for ident in _references(element) if ident in ids]
        if not expanded:
            if element in visiting:
                return False                         # a reference loop
            visiting.add(element)
            stack.append((element, True))
            for node in children + targets:
                if node not in cost:
                    if node in visiting:
                        return False
                    stack.append((node, False))
            continue
        visiting.discard(element)
        total = 1 + sum(cost.get(node, 0) for node in children + targets)
        if total > limit:
            return False
        cost[element] = total
    return cost.get(root, 0) <= limit


def safe_svg(data: bytes) -> Optional[bytes]:
    """The SVG with every reference to another file taken out: an
    ``<image>`` or ``<feImage>`` stays only as a data: picture
    image_safety would decode, a ``<use>`` only when it points at an
    element inside the file; scripts and foreign objects go. None when it
    is not XML, or when what it would draw is past USE_EXPANSION_LIMIT
    shapes."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=False, remove_pis=True)
    try:
        root = etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    if not isinstance(root.tag, str) or etree.QName(root).localname != "svg":
        return None
    ids = {element.get("id") for element in root.iter()
           if isinstance(element.tag, str) and element.get("id")}
    doomed = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element).localname
        # Every href the element has, xlink or plain: QtSvg reads either.
        hrefs = [element.get(key) for key in (_XLINK_HREF, "href")
                 if element.get(key) is not None]
        if name in ("script", "foreignObject"):
            doomed.append(element)
        elif name in ("image", "feImage"):
            for href in hrefs or [""]:
                payload = (image_safety.data_uri_bytes(href)
                           if href.lower().startswith("data:") else None)
                if payload is None or image_safety.sniff_image_mime(payload) \
                        not in image_safety.DECODABLE_MIMES:
                    doomed.append(element)
                    break
        elif any(href and not href.startswith("#") for href in hrefs):
            doomed.append(element)
        elif name == "use" and not any(href[1:] in ids for href in hrefs if href):
            doomed.append(element)                    # names nothing in the file
    for element in doomed:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    if not _expansion_within(root, USE_EXPANSION_LIMIT):
        log.info("Not drawing an SVG that would make more than %d shapes", USE_EXPANSION_LIMIT)
        return None
    return etree.tostring(root)


def _svg_natural_size(renderer) -> QSize:
    natural = renderer.defaultSize()
    if natural.width() <= 0 or natural.height() <= 0:
        natural = QSize(512, 512)
    return natural


def svg_size(data: bytes) -> Optional[QSize]:
    """The size an SVG says it has, read through the sanitizer."""
    from PySide6.QtSvg import QSvgRenderer
    clean = safe_svg(data)
    if clean is None:
        return None
    renderer = QSvgRenderer(QByteArray(clean))
    return _svg_natural_size(renderer) if renderer.isValid() else None


def render_svg(data: bytes, at_least: Optional[QSize] = None) -> Optional[QImage]:
    """An SVG drawn after ``safe_svg``: at most SVG_RENDER_SIDE on its long
    side and within the pixel budget; as large as that allows (so a zoom
    stays sharp), or just large enough to cover ``at_least``. The picture
    still measures its own size (its device pixel ratio says how)."""
    from PySide6.QtSvg import QSvgRenderer
    clean = safe_svg(data)
    if clean is None:
        return None
    renderer = QSvgRenderer(QByteArray(clean))
    if not renderer.isValid():
        return None
    natural = _svg_natural_size(renderer)
    if at_least is not None and at_least.width() > 0 and at_least.height() > 0:
        target = natural.scaled(at_least, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    else:
        target = QSize(SVG_RENDER_SIDE, SVG_RENDER_SIDE)
        target = natural.scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
    if max(target.width(), target.height()) > SVG_RENDER_SIDE:
        target = target.scaled(QSize(SVG_RENDER_SIDE, SVG_RENDER_SIDE),
                               Qt.AspectRatioMode.KeepAspectRatio)
    target = QSize(max(1, target.width()), max(1, target.height()))
    if target.width() * target.height() > image_safety.MAX_DECODE_PIXELS:
        return None
    image = QImage(target, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    image.setDevicePixelRatio(target.width() / natural.width())
    return image


# --------------------------------------------------------------------------- #
# Pictures                                                                    #
# --------------------------------------------------------------------------- #

def read_picture(path: str, at_least: Optional[QSize] = None) -> Optional[QImage]:
    """The picture at ``path``, decoded safely (see the module's words), or
    None. ``at_least`` decodes no larger than needed to cover that box,
    for a thumbnail."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        if not svg_available():
            return None
        data = read_regular_file(path, SVG_FILE_LIMIT)
        return render_svg(data, at_least) if data else None
    data = read_regular_file(path, PICTURE_FILE_LIMIT)
    if not data:
        return None
    fmt = _raster_format(data, ext)
    return _decode_as(data, fmt, at_least) if fmt else None


def picture_size(path: str) -> Optional[QSize]:
    """A picture's size in pixels, read from its header (the first
    HEADER_BYTES of the file), or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        if not svg_available():
            return None
        data = read_regular_file(path, SVG_FILE_LIMIT)
        return svg_size(data) if data else None
    data = read_regular_prefix(path, HEADER_BYTES)
    if not data:
        return None
    fmt = _raster_format(data, ext)
    if fmt is None:
        return None
    reader, buffer = _reader(data, fmt)
    if reader is None:
        return None
    try:
        # Upright, as the Finder says it: a phone's portrait photo, stored
        # on its side, is taller than wide.
        return image_safety.upright_size(reader)
    finally:
        buffer.close()
