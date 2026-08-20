# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins what is stripped before a picture becomes public.

The failure this guards against is silent and permanent: a photo
published with the coordinates of where it was taken still embedded.
Nobody sees it, nothing errors, and once other people hold the bytes it
cannot be recalled. So the test that matters most is not that scrubbing
runs, it is that the metadata is genuinely gone from the output.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from nostr.media.scrub import ScrubError, scrub_for_publication, would_scrub

from tests.media_fakes import GIF_BYTES


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def encoded(fmt="PNG", color="red", size=(8, 6)):
    img = QImage(*size, QImage.Format_RGB32)
    img.fill(QColor(color))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    assert img.save(buf, fmt)
    buf.close()
    return bytes(buf.data())


def jpeg_with_gps():
    """A JPEG carrying a recognisable EXIF payload.

    Built by splicing an APP1 segment in after SOI, which is exactly how
    a camera writes one, so the marker really is in the file rather than
    merely appended where a decoder would ignore it.
    """
    base = encoded("JPEG")
    secret = b"GPSLatitude=51.5074;GPSLongitude=-0.1278;Make=SecretCamera"
    payload = b"Exif\x00\x00" + secret
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return base[:2] + app1 + base[2:], secret


# --------------------------------------------------------------------- #
# The guarantee                                                         #
# --------------------------------------------------------------------- #

def test_exif_and_gps_do_not_survive_publication():
    data, secret = jpeg_with_gps()
    assert secret in data, "fixture is wrong, the marker was never present"
    out = scrub_for_publication(data)
    assert out.scrubbed
    assert secret not in out.data
    assert b"GPSLatitude" not in out.data
    assert b"SecretCamera" not in out.data


def test_the_picture_itself_survives():
    # Stripping metadata must not cost the user their image.
    out = scrub_for_publication(encoded("PNG", "blue", (12, 9)))
    restored = QImage.fromData(out.data)
    assert not restored.isNull()
    assert restored.size().width() == 12 and restored.size().height() == 9
    assert QColor(restored.pixel(4, 4)).name() == QColor("blue").name()


def test_scrubbing_produces_different_bytes_and_therefore_a_new_blob():
    data, _ = jpeg_with_gps()
    out = scrub_for_publication(data)
    assert out.data != data


# --------------------------------------------------------------------- #
# Formats                                                               #
# --------------------------------------------------------------------- #

def test_a_jpeg_stays_a_jpeg():
    # A photo must not balloon into a lossless format on publication.
    out = scrub_for_publication(encoded("JPEG"))
    assert out.mime == "image/jpeg"
    assert out.data[:2] == b"\xff\xd8"


def test_a_png_stays_a_png():
    out = scrub_for_publication(encoded("PNG"))
    assert out.mime == "image/png"
    assert out.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_bmp_becomes_a_png():
    out = scrub_for_publication(encoded("BMP"))
    assert out.mime == "image/png" and out.scrubbed


def test_an_animated_gif_is_passed_through_rather_than_flattened():
    # A still-image re-encode would silently reduce it to frame one, and
    # GIF carries no location metadata, so the trade runs the wrong way.
    gif = GIF_BYTES
    out = scrub_for_publication(gif)
    assert out.data == gif
    assert out.scrubbed is False
    assert "animation" in out.note


# --------------------------------------------------------------------- #
# Refusals                                                              #
# --------------------------------------------------------------------- #

def test_an_svg_is_refused():
    # SVG can pull in external resources when rendered, so it is not
    # something this app hands to the public internet.
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="/etc/passwd"/></svg>'
    with pytest.raises(ScrubError):
        scrub_for_publication(svg)


@pytest.mark.parametrize("junk", [b"", b"not an image at all", b"\x00" * 64])
def test_non_images_are_refused(junk):
    with pytest.raises(ScrubError):
        scrub_for_publication(junk)


def test_an_encrypted_blob_is_refused():
    # A private envelope reaching the publish path unencrypted would be
    # a serious bug; refusing it is the backstop.
    envelope = b"\x02" + os.urandom(80)
    with pytest.raises(ScrubError):
        scrub_for_publication(envelope)


def test_a_declared_mime_is_never_trusted_over_the_bytes():
    with pytest.raises(ScrubError):
        scrub_for_publication(b"still not an image", declared_mime="image/png")


def test_a_truncated_image_is_refused_rather_than_half_published():
    data = encoded("PNG")
    with pytest.raises(ScrubError):
        scrub_for_publication(data[: len(data) // 3])


# --------------------------------------------------------------------- #
# Telling the user in advance                                           #
# --------------------------------------------------------------------- #

def test_would_scrub_reports_what_publishing_will_do():
    assert would_scrub(encoded("JPEG")) is True
    assert would_scrub(encoded("PNG")) is True
    assert would_scrub(GIF_BYTES) is False


def test_would_scrub_separates_nothing_to_strip_from_cannot_publish():
    # None means "not publishable", which a confirm dialog must not
    # report as "nothing will be stripped".
    assert would_scrub(b"not an image") is None
    assert would_scrub(b"") is None
