#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native Qt PDF exporter.

Replaces the old bare doc.print_() call with a custom pagination loop so
the output carries what a native OS export would: document metadata
(title, creator), a locale-aware default page size, persisted page setup,
a "Page N of M" footer, and images capped to the printable width.

Geometry notes, established empirically against QPdfDocument:
- The painter origin on a QPdfWriter sits at the top-left of the margin
  (paint) rect, and the device clip covers the paint rect only.
- Below 600 dpi Qt emits per-glyph text runs, which wrecks selection and
  extraction in PDF viewers; 1200 dpi (the old QPrinter.HighResolution
  behavior) keeps runs contiguous.
- QTextImageFormat sizes are in CSS pixels (96 per inch): an image with
  no explicit size renders at intrinsic_px * 72/96 points.
- The same pagination prints (printing.py): paginate() and paint_pages()
  work on any paged device, so a printed page matches the exported one.
- doc.clone() returns a plain QTextDocument, so any resource resolver
  installed on the editor widget does not apply to it. Every image is
  therefore primed into the clone before layout: without that a cold
  document (one never shown on screen) exports with no images at all,
  and Qt would resolve image names against the filesystem itself,
  outside the trusted-root policy.
"""

import json
import os
from dataclasses import dataclass

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt, QLocale, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPalette,
    QPdfWriter,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)

from constants import MONO_FONT
from image_safety import ImageRootPolicy, data_uri_bytes, decode_image_bytes

CREATOR = "minimal texteditor"

# 1200 dpi keeps text extractable/selectable (see module docstring) and
# matches the resolution of the previous QPrinter-based export.
RESOLUTION = 1200

BODY_POINT_SIZE = 11
FOOTER_POINT_SIZE = 8
FOOTER_BAND_PT = 30.0
FOOTER_COLOR = "#808080"

_CONFIG_PATH = os.path.expanduser("~/.config/my_editor/page_setup.json")

# Offered in the Page Setup dialog; keys are stored in page_setup.json.
PAGE_SIZES = {
    "A4": QPageSize.A4,
    "Letter": QPageSize.Letter,
    "Legal": QPageSize.Legal,
    "A5": QPageSize.A5,
}

_MIN_MARGIN_MM = 5.0
_MAX_MARGIN_MM = 50.0


def default_page_setup() -> dict:
    """Letter for imperial locales (US, CA, ...), A4 everywhere else."""
    imperial = QLocale().measurementSystem() != QLocale.MetricSystem
    return {
        "page_size": "Letter" if imperial else "A4",
        "orientation": "portrait",
        "margins_mm": 20.0,
    }


def load_page_setup() -> dict:
    setup = default_page_setup()
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return setup
    if isinstance(data, dict):
        if data.get("page_size") in PAGE_SIZES:
            setup["page_size"] = data["page_size"]
        if data.get("orientation") in ("portrait", "landscape"):
            setup["orientation"] = data["orientation"]
        margins = data.get("margins_mm")
        if isinstance(margins, (int, float)) and _MIN_MARGIN_MM <= margins <= _MAX_MARGIN_MM:
            setup["margins_mm"] = float(margins)
    return setup


def save_page_setup(setup: dict) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(setup, f, indent=2)


def make_page_layout(setup: dict) -> QPageLayout:
    orientation = (QPageLayout.Landscape if setup["orientation"] == "landscape"
                   else QPageLayout.Portrait)
    m = setup["margins_mm"]
    return QPageLayout(QPageSize(PAGE_SIZES[setup["page_size"]]), orientation,
                       QMarginsF(m, m, m, m), QPageLayout.Millimeter)


def _placeholder_image() -> QImage:
    """Neutral stand-in for an image the export is not allowed to read."""
    image = QImage(160, 100, QImage.Format_RGB32)
    image.fill(QColor("#c8c8c8"))
    return image


def _prime_clone_images(clone, policy: ImageRootPolicy, asset_resolver) -> dict:
    """Register every image as a resource on ``clone`` and return sizes.

    Mandatory, not defensive: the clone is a plain QTextDocument, so it
    resolves image names itself unless they are already registered, and
    a name that resolves to nothing renders as an empty box. Priming
    also means Qt never touches the disk during layout or paint, which
    is what keeps the trusted-root policy in force.
    """
    sizes: dict[str, object] = {}
    for name in _iter_image_names(clone):
        if name in sizes:
            continue
        image = None
        resolved = asset_resolver(name) if asset_resolver else None
        if resolved is not None and getattr(resolved, "data", b""):
            image = decode_image_bytes(resolved.data)
        if image is None:
            # A document reopened from HTML names its embedded images by
            # the whole data: URI, so the bytes are in the name; without
            # this every such image prints as the placeholder.
            data = policy.read(name) or data_uri_bytes(name)
            if data:
                image = decode_image_bytes(data)
        if image is None:
            image = _placeholder_image()
        clone.addResource(QTextDocument.ImageResource, QUrl(name), image)
        sizes[name] = image.size()
    return sizes


def _iter_image_names(doc):
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if frag.isValid() and fmt.isImageFormat():
                yield fmt.toImageFormat().name()
            it += 1
        block = block.next()


def _cap_image_widths(doc, available_css_px: float, sizes: dict) -> None:
    """Shrink images wider than the printable area, keeping aspect ratio.

    Sizes are in CSS pixels (96/inch). Images with no explicit size are
    measured from the primed resources: opening the file again here
    would put back the untrusted disk read that priming removed, and an
    asset key is not a filename any reader could open anyway.
    """
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if frag.isValid() and fmt.isImageFormat():
                imf = fmt.toImageFormat()
                width = imf.width()
                height = imf.height()
                if width <= 0:
                    intrinsic = sizes.get(imf.name())
                    if intrinsic is not None and intrinsic.isValid():
                        width = float(intrinsic.width())
                        if height <= 0:
                            height = float(intrinsic.height())
                if width > available_css_px:
                    scale = available_css_px / width
                    imf.setWidth(available_css_px)
                    if height > 0:
                        imf.setHeight(height * scale)
                    cursor = QTextCursor(doc)
                    cursor.setPosition(frag.position())
                    cursor.setPosition(frag.position() + frag.length(),
                                       QTextCursor.KeepAnchor)
                    cursor.setCharFormat(imf)
            it += 1
        block = block.next()


@dataclass
class PagedDocument:
    """A document laid out into pages for one paged device.

    ``clone`` is the private copy that was laid out; sizes are in the
    device's pixels. Shared by export_pdf() and printing.py, so a printed
    page and an exported page are the same page.
    """
    clone: QTextDocument
    page_width: float
    content_height: float
    footer_height: float
    page_count: int


def paginate(doc, device, resolution: int, *, image_roots=(), asset_resolver=None) -> PagedDocument:
    """Lay ``doc`` out into pages for ``device`` (a QPdfWriter or a QPrinter).

    Works on a clone, so the editor's document is never touched. See the
    module docstring for why images are primed and fonts are measured
    against the device.
    """
    paint = device.pageLayout().paintRectPixels(resolution)
    page_w = float(paint.width())
    footer_px = FOOTER_BAND_PT * resolution / 72.0
    content_h = float(paint.height()) - footer_px

    clone = doc.clone()
    sizes = _prime_clone_images(clone, ImageRootPolicy(image_roots), asset_resolver)
    # Layout must measure fonts against the device's dpi, exactly as
    # doc.print_() would.
    clone.documentLayout().setPaintDevice(device)
    # Print in points, not the editor's 14 screen pixels, and force a white
    # page regardless of the active theme. Palette colors (Material 600)
    # are chosen to stay readable on white.
    clone.setDefaultFont(QFont(MONO_FONT, BODY_POINT_SIZE))
    frame_fmt = clone.rootFrame().frameFormat()
    frame_fmt.setBackground(QColor("white"))
    clone.rootFrame().setFrameFormat(frame_fmt)

    paint_width_pt = page_w * 72.0 / resolution
    _cap_image_widths(clone, paint_width_pt * 96.0 / 72.0, sizes)

    clone.setTextWidth(page_w)
    clone.setPageSize(QSizeF(page_w, content_h))
    return PagedDocument(clone, page_w, content_h, footer_px, clone.pageCount())


def paint_pages(painter: QPainter, device, paged: PagedDocument, page_numbers) -> None:
    """Paint the given 1-based pages of ``paged`` onto ``device``.

    The footer always counts against the whole document, so page 3 of a
    printed range still says "Page 3 of 7".
    """
    layout = paged.clone.documentLayout()
    footer_font = QFont(MONO_FONT, FOOTER_POINT_SIZE)
    content_h = paged.content_height
    for n, number in enumerate(page_numbers):
        page = number - 1
        if n:
            device.newPage()
        painter.save()
        painter.translate(0, -page * content_h)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.clip = QRectF(0, page * content_h, paged.page_width, content_h)
        # Unset-color text must not inherit the app palette: in dark
        # theme that would paint near-white on the white page.
        ctx.palette.setColor(QPalette.Text, Qt.black)
        layout.draw(painter, ctx)
        painter.restore()

        if paged.page_count > 1:
            painter.save()
            painter.setPen(QColor(FOOTER_COLOR))
            painter.setFont(footer_font)
            painter.drawText(QRectF(0, content_h, paged.page_width, paged.footer_height),
                             Qt.AlignHCenter | Qt.AlignVCenter,
                             f"Page {number} of {paged.page_count}")
            painter.restore()


def export_pdf(doc, path: str, title: str = "", page_setup: dict | None = None,
               *, image_roots=(), asset_resolver=None) -> None:
    """Write the document to ``path`` as a paginated PDF.

    ``image_roots`` are the directories a local image name may be read
    from; empty (the default) reads nothing. ``asset_resolver`` supplies
    bytes for images this app created and holds in its own cache.

    Raises OSError when the file cannot be written.
    """
    setup = page_setup or load_page_setup()

    writer = QPdfWriter(path)
    writer.setResolution(RESOLUTION)
    writer.setTitle(title)
    writer.setCreator(CREATOR)
    writer.setPageLayout(make_page_layout(setup))

    paged = paginate(doc, writer, RESOLUTION, image_roots=image_roots,
                     asset_resolver=asset_resolver)
    painter = QPainter(writer)
    if not painter.isActive():
        raise OSError(f"could not open {path!r} for writing")
    try:
        paint_pages(painter, writer, paged, range(1, paged.page_count + 1))
    finally:
        painter.end()
