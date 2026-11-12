# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The photo fixtures with coloured corners, and the check that a picture
is shown whole (SPEC 17 of the photo grid work).

Each fixture has a magenta border and a square of colour in each corner:
red top left, green top right, blue bottom left, yellow bottom right.
A picture shown whole shows all four, each in its own corner; a cropped
one (object-fit: cover, a cut edge) loses at least one. The same PNGs are
STANDUP's tests/fixtures/photos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage

PHOTOS = Path(__file__).resolve().parent / "fixtures" / "photos"

# name -> (width, height) in pixels
SHAPES: Dict[str, Tuple[int, int]] = {
    "portrait.png": (809, 1800),
    "landscape.png": (1800, 809),
    "square.png": (1400, 1400),
    "panorama.png": (3600, 900),
    "photo.png": (1800, 1200),
    "plan.png": (1800, 1273),
}

# Top left, top right, bottom left, bottom right.
CORNERS = (QColor("#E5484D"), QColor("#30A46C"), QColor("#0090FF"), QColor("#FFB224"))


def path_of(name: str) -> Path:
    return PHOTOS / name


def image_of(name: str) -> QImage:
    image = QImage(str(path_of(name)))
    assert not image.isNull(), name
    return image


def jpeg_turned(width: int, height: int, orientation: int) -> bytes:
    """A JPEG ``width`` x ``height`` with an EXIF orientation, as a phone
    writes it (6: a portrait photo, stored on its side)."""
    out = QByteArray()
    buffer = QBuffer(out)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkCyan)
    image.save(buffer, "JPEG")
    tiff = (b"II*\x00\x08\x00\x00\x00\x01\x00" + b"\x12\x01\x03\x00\x01\x00\x00\x00"
            + orientation.to_bytes(2, "little") + b"\x00\x00" + b"\x00\x00\x00\x00")
    exif = b"Exif\x00\x00" + tiff
    return bytes(out)[:2] + b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif \
        + bytes(out)[2:]


def corner_colours(shot: QImage, box: QRectF) -> List[QColor]:
    """The colours 3 % of the box's short side inside each of its corners,
    and one pixel more: a picture is drawn on whole pixels, so its edge
    can be a pixel off its box, which in a picture 100 pixels wide is a
    third of the way from 3 % to the white edge of a corner's square
    (its colour runs from 2.3 % to 10.2 % of the short side)."""
    inset = min(box.width(), box.height()) * 0.03 + 1.0
    left, top = box.left() + inset, box.top() + inset
    right, bottom = box.right() - inset, box.bottom() - inset
    ratio = shot.devicePixelRatio() or 1.0
    return [QColor(shot.pixel(int(x * ratio), int(y * ratio)))
            for x, y in ((left, top), (right, top), (left, bottom), (right, bottom))]


def _close(a: QColor, b: QColor, tolerance: int = 48) -> bool:
    return (abs(a.red() - b.red()) <= tolerance and abs(a.green() - b.green()) <= tolerance
            and abs(a.blue() - b.blue()) <= tolerance)


def shows_whole(shot: QImage, box: QRectF) -> bool:
    """Whether the picture in ``box`` of ``shot`` shows all four corners."""
    return all(_close(found, wanted) for found, wanted in zip(corner_colours(shot, box), CORNERS))


def pdf_pages(path) -> List[QImage]:
    """Each page of the PDF at ``path``, drawn at 72 dots an inch."""
    from PySide6.QtPdf import QPdfDocument
    pdf = QPdfDocument()
    assert pdf.load(str(path)) == QPdfDocument.Error.None_
    pages = []
    for page in range(pdf.pageCount()):
        size = pdf.pagePointSize(page)
        pages.append(pdf.render(page, QSize(int(size.width()), int(size.height()))))
    pdf.close()
    return pages


def has_colour(image: QImage, colour: QColor, tolerance: int = 24) -> bool:
    """Whether any pixel of ``image`` (every other one, each way) is
    ``colour``."""
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            found = QColor(image.pixel(x, y))
            if (abs(found.red() - colour.red()) <= tolerance
                    and abs(found.green() - colour.green()) <= tolerance
                    and abs(found.blue() - colour.blue()) <= tolerance):
                return True
    return False


def pages_show_a_picture(pages) -> bool:
    """Whether a page shows a fixture picture: all four corner colours
    (a stand-in shows none)."""
    return any(all(has_colour(page, colour) for colour in CORNERS) for page in pages)


def gathered(urls, **kw) -> dict:
    """export_pictures.Gathering's pictures once it finished: the test
    waits in a loop of its own (the app never does)."""
    from PySide6.QtCore import QEventLoop, QTimer

    import export_pictures
    gathering = export_pictures.Gathering(urls, **kw)
    loop = QEventLoop()
    gathering.finished.connect(loop.quit)
    gathering.start()
    if gathering._running:
        QTimer.singleShot(120_000, loop.quit)
        loop.exec()
    return gathering.pictures
