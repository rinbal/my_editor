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

import os
from dataclasses import dataclass

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt, QLocale, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPalette,
    QPdfWriter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
)

import export_pictures
import media_files
import photo_group
from atomic_file import read_json, write_json
from fonts import monospace_font
from i18n import _
from image_safety import ImageRootPolicy, decode_image_bytes

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
    data = read_json(_CONFIG_PATH, dict)
    if data:
        if data.get("page_size") in PAGE_SIZES:
            setup["page_size"] = data["page_size"]
        if data.get("orientation") in ("portrait", "landscape"):
            setup["orientation"] = data["orientation"]
        margins = data.get("margins_mm")
        if isinstance(margins, (int, float)) and _MIN_MARGIN_MM <= margins <= _MAX_MARGIN_MM:
            setup["margins_mm"] = float(margins)
    return setup


def save_page_setup(setup: dict) -> None:
    write_json(_CONFIG_PATH, setup, indent=2)


def make_page_layout(setup: dict) -> QPageLayout:
    orientation = (QPageLayout.Landscape if setup["orientation"] == "landscape"
                   else QPageLayout.Portrait)
    m = setup["margins_mm"]
    return QPageLayout(QPageSize(PAGE_SIZES[setup["page_size"]]), orientation,
                       QMarginsF(m, m, m, m), QPageLayout.Millimeter)


# A stand-in is drawn this many times finer than its size on the page, so
# its words stay sharp on paper.
_STAND_IN_SCALE = 3
_STAND_IN_SIZE = (240, 160)


def _placeholder_image(alt: str = "") -> QImage:
    """The quiet stand-in for a picture the export cannot carry (one it may
    not read, or a web picture that did not come): a plate in the page's
    quiet grey with the picture's description, in place, and nothing else
    (lesson 36: data missing for the reader is one quiet line where it
    is, never an alert)."""
    width, height = _STAND_IN_SIZE
    image = QImage(width * _STAND_IN_SCALE, height * _STAND_IN_SCALE, QImage.Format_RGB32)
    image.fill(QColor("#f1f0ec"))
    words = " ".join(alt.split())
    if words:
        painter = QPainter(image)
        font = QFont(QGuiApplication.font())
        font.setPixelSize(13 * _STAND_IN_SCALE)
        painter.setFont(font)
        painter.setPen(QColor("#6b6760"))
        margin = 14 * _STAND_IN_SCALE
        box = QRectF(margin, margin, image.width() - 2 * margin, image.height() - 2 * margin)
        metrics = QFontMetricsF(font)
        lines = max(1, int(box.height() // metrics.lineSpacing()))
        text = _elided_lines(words, metrics, box.width(), lines)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), text)
        painter.end()
    image.setDevicePixelRatio(_STAND_IN_SCALE)
    # Its shape is not the picture's: a photo group lays it out at 3:2.
    return photo_group.mark_placeholder(image)


def _elided_lines(words: str, metrics: QFontMetricsF, width: float, lines: int) -> str:
    """``words`` wrapped to at most ``lines`` lines of ``width``, the last
    one ending in an ellipsis when they do not all fit."""
    out: list[str] = []
    rest = words.split(" ")
    while rest and len(out) < lines:
        line = rest.pop(0)
        while rest and metrics.horizontalAdvance(line + " " + rest[0]) <= width:
            line += " " + rest.pop(0)
        out.append(line)
    if rest or (out and metrics.horizontalAdvance(out[-1]) > width):
        out[-1] = metrics.elidedText(out[-1] + " " + " ".join(rest), Qt.TextElideMode.ElideRight,
                                     width)
    return "\n".join(out)


def _prime_clone_images(clone, policy: ImageRootPolicy, asset_resolver,
                        remote_pictures=None) -> dict:
    """Register every image as a resource on ``clone`` and return them by
    name (the stand-in for one that cannot be read).

    Mandatory, not defensive: the clone is a plain QTextDocument, so it
    resolves image names itself unless they are already registered, and
    a name that resolves to nothing renders as an empty box. Priming
    also means Qt never touches the disk during layout or paint, which
    is what keeps the trusted-root policy in force.

    The bytes come from export_pictures.picture_bytes (a web picture from
    ``remote_pictures``, export_pictures.gather), read without #wide; a
    picture that cannot be had is a quiet stand-in with its description.
    """
    sizes: dict[str, object] = {}
    for name, alt in _image_names_and_alts(clone):
        if name in sizes:
            continue
        data, _source = export_pictures.picture_bytes(
            name, policy, asset_resolver=asset_resolver, remote_pictures=remote_pictures)
        image = decode_image_bytes(data) if data else None
        if image is None:
            image = _placeholder_image(alt)
        clone.addResource(QTextDocument.ImageResource, QUrl(name), image)
        sizes[name] = image
    return sizes


def _image_names_and_alts(doc):
    """Each picture's name and description, in document order."""
    for name, fmt in _iter_image_formats(doc):
        yield name, str(fmt.property(QTextFormat.Property.ImageAltText) or "")


def _media_as_links(clone, asset_resolver) -> None:
    """Audio and video on paper: a link to the file once it is on a media
    server, as the HTML export writes it; left out while it is not. Never
    a picture of it, never the grey stand-in."""
    if asset_resolver is None:
        return
    found = []
    block = clone.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fragment.isValid() and fmt.isImageFormat():
                resolved = asset_resolver(fmt.toImageFormat().name())
                mime = str(getattr(resolved, "mime", "") or "")
                if media_files.kind_of(mime) is not None:
                    found.append((fragment.position(), fragment.length(), resolved, fmt))
            it += 1
        block = block.next()
    for position, length, resolved, fmt in reversed(found):
        cursor = QTextCursor(clone)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        remote = str(getattr(resolved, "remote_url", "") or "")
        if not remote:
            cursor.removeSelectedText()
            continue
        mime = str(resolved.mime)
        name = str(fmt.toImageFormat().property(QTextImageFormat.ImageAltText) or "")
        link = QTextCharFormat()
        link.setAnchor(True)
        link.setAnchorHref(media_files.address_with_extension(remote, mime))
        link.setFontUnderline(True)
        cursor.insertText(media_files.link_words(mime, name or remote.rsplit("/", 1)[-1]), link)


def _iter_image_formats(doc):
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if frag.isValid() and fmt.isImageFormat():
                image = fmt.toImageFormat()
                yield image.name(), image
            it += 1
        block = block.next()


def _iter_image_names(doc):
    for name, _fmt in _iter_image_formats(doc):
        yield name


def _cap_image_widths(doc, available_css_px: float, images: dict) -> None:
    """Shrink images wider than the printable area, keeping aspect ratio.

    Sizes are in CSS pixels (96/inch). Images with no explicit size are
    measured from the primed resources: opening the file again here
    would put back the untrusted disk read that priming removed, and an
    asset key is not a filename any reader could open anyway. The
    pictures of a photo group are left to its layout.
    """
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().hasProperty(photo_group.UNIT):
            block = block.next()
            continue
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if frag.isValid() and fmt.isImageFormat():
                imf = fmt.toImageFormat()
                width = imf.width()
                height = imf.height()
                if width <= 0:
                    image = images.get(imf.name())
                    intrinsic = image.size() if image is not None else None
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


def _keep_rows_on_pages(clone, content_h: float) -> None:
    """A row of a photo group that would run over the end of a page, and
    fits on one, starts the next page (as print CSS keeps a figure whole).
    One row after the other: moving one moves the rest."""
    layout = clone.documentLayout()
    for frame in clone.rootFrame().childFrames():
        if not photo_group.is_group_table(frame):
            continue
        box = layout.frameBoundingRect(frame)
        if box.height() >= content_h or box.height() <= 0:
            continue
        first = int(box.top() // content_h)
        last = int((box.bottom() - 0.5) // content_h)
        if first != last:
            fmt = frame.format()
            fmt.setPageBreakPolicy(QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore)
            frame.setFormat(fmt)


def paginate(doc, device, resolution: int, *, image_roots=(), asset_resolver=None,
             remote_pictures=None) -> PagedDocument:
    """Lay ``doc`` out into pages for ``device`` (a QPdfWriter or a QPrinter).

    Works on a copy, so the editor's document is never touched; a copy
    PySide frees once dropped (a clone() lives as long as the document,
    with every picture of it: the memory of each export kept until the
    tab closed). See the module docstring for why images are primed and
    fonts are measured against the device. A photo group is laid out as
    on a reader's screen the size of the page (on paper narrower than 600
    CSS pixels as on a phone, its strips unrolled), and each of its rows
    stays on one page.
    """
    paint = device.pageLayout().paintRectPixels(resolution)
    page_w = float(paint.width())
    footer_px = FOOTER_BAND_PT * resolution / 72.0
    content_h = float(paint.height()) - footer_px

    clone = photo_group.copy_of(doc)
    _media_as_links(clone, asset_resolver)
    images = _prime_clone_images(clone, ImageRootPolicy(image_roots), asset_resolver,
                                 remote_pictures)
    groups = photo_group.has_groups(clone)
    if groups:
        photo_group.install(clone, photo_group.table_sizes(images))
    # Layout must measure fonts against the device's dpi, exactly as
    # doc.print_() would.
    clone.documentLayout().setPaintDevice(device)
    # Print in points, not the editor's 14 screen pixels, and force a white
    # page regardless of the active theme. Palette colors (Material 600)
    # are chosen to stay readable on white.
    clone.setDefaultFont(monospace_font(BODY_POINT_SIZE))
    frame_fmt = clone.rootFrame().frameFormat()
    frame_fmt.setBackground(QColor("white"))
    clone.rootFrame().setFrameFormat(frame_fmt)

    paint_width_pt = page_w * 72.0 / resolution
    _cap_image_widths(clone, paint_width_pt * 96.0 / 72.0, images)

    if groups and paint_width_pt * 96.0 / 72.0 < photo_group.NARROW_BELOW:
        photo_group.refold(clone, narrow=True)
    clone.setTextWidth(page_w)
    clone.setPageSize(QSizeF(page_w, content_h))
    if groups:
        photo_group.set_screen_height(clone, content_h)
        _keep_rows_on_pages(clone, content_h)
    return PagedDocument(clone, page_w, content_h, footer_px, clone.pageCount())


def paint_pages(painter: QPainter, device, paged: PagedDocument, page_numbers) -> None:
    """Paint the given 1-based pages of ``paged`` onto ``device``.

    The footer always counts against the whole document, so page 3 of a
    printed range still says "Page 3 of 7".
    """
    layout = paged.clone.documentLayout()
    footer_font = monospace_font(FOOTER_POINT_SIZE)
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
                             _("Page {number} of {count}").format(
                                 number=number, count=paged.page_count))
            painter.restore()


def export_pdf(doc, path: str, title: str = "", page_setup: dict | None = None,
               *, image_roots=(), asset_resolver=None, remote_pictures=None) -> None:
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
                     asset_resolver=asset_resolver, remote_pictures=remote_pictures)
    painter = QPainter(writer)
    if not painter.isActive():
        raise OSError(f"could not open {path!r} for writing")
    try:
        paint_pages(painter, writer, paged, range(1, paged.page_count + 1))
    finally:
        painter.end()
