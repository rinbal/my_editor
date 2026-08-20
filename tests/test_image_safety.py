# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the decode allowlist and the trusted-root file policy.

The defect classes this file guards against:
- SVG reaching a renderer. Qt's SVG backend resolves ``xlink:href``
  against the local filesystem, and both blob bytes and any stranger's
  avatar reach this decoder,
- a header claiming an enormous canvas allocating gigabytes before a
  single pixel is checked,
- an exporter reading a file the document named but the user never
  meant to publish, including through a symlink planted beside a
  hostile document,
- a name that carries its own address (a data: URI, a foreign URL)
  being mistaken for a local path, or its payload skipping the
  allowlist because it arrived inside the name.
"""

from __future__ import annotations

import base64
import os
import struct
import sys
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import image_safety
from image_safety import (
    ImageRootPolicy,
    data_uri_bytes,
    decode_image_bytes,
    is_portable_image_source,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


SVG_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<image xlink:href="/etc/passwd"/></svg>'
)


def _png_bytes(color="teal", size=4) -> bytes:
    img = QImage(size, size, QImage.Format_RGB32)
    img.fill(QColor(color))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _png_header_claiming(width: int, height: int) -> bytes:
    """A valid PNG signature and IHDR, nothing else. Truncated, so a
    reader cannot report a size for it at all."""
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    crc = zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    return (b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
            + struct.pack(">I", crc))


def _png_declaring(width: int, height: int) -> bytes:
    """A complete PNG whose IHDR claims a canvas it does not carry.

    A truncated header is rejected for being unreadable, which says
    nothing about the pixel budget. Rewriting the IHDR of a whole file
    keeps every chunk a reader needs, so the declared size is what the
    budget actually sees.
    """
    data = bytearray(_png_bytes())
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


# --------------------------------------------------------------------------- #
# Decode allowlist
# --------------------------------------------------------------------------- #

def test_decode_accepts_a_real_png():
    image = decode_image_bytes(_png_bytes())
    assert image is not None
    assert image.width() == 4


def test_decode_refuses_svg():
    assert decode_image_bytes(SVG_BYTES) is None


def test_decode_refuses_a_truncated_header():
    assert decode_image_bytes(_png_header_claiming(30000, 30000)) is None


def test_decode_refuses_a_decompression_bomb_header():
    assert decode_image_bytes(_png_declaring(30000, 30000)) is None


def test_decode_enforces_the_pixel_budget(monkeypatch):
    # The budget is the line doing the work, so it is pinned against a
    # picture that decodes perfectly well once the budget allows it.
    picture = _png_bytes(size=4)
    assert decode_image_bytes(picture) is not None

    monkeypatch.setattr(image_safety, "MAX_DECODE_PIXELS", 15)

    assert decode_image_bytes(picture) is None


def test_the_pixel_budget_stays_within_a_defensible_allocation():
    # At 4 bytes per pixel the budget is an upper bound on how much
    # memory one hostile header can ask for. Raised far enough it stops
    # being a guard at all, and no decode test would notice.
    assert image_safety.MAX_DECODE_PIXELS <= 100_000_000


def test_decode_refuses_text_and_empty():
    assert decode_image_bytes(b"not an image at all") is None
    assert decode_image_bytes(b"") is None


# --------------------------------------------------------------------------- #
# Trusted-root reads
# --------------------------------------------------------------------------- #

def _inside(tmp_path, name="a" * 64):
    path = tmp_path / name
    path.write_bytes(_png_bytes())
    return path


def test_policy_resolves_absolute_relative_and_file_url(tmp_path):
    path = _inside(tmp_path)
    policy = ImageRootPolicy([str(tmp_path)])
    assert policy.resolve(str(path)) == path
    assert policy.resolve(path.name) == path
    assert policy.resolve(path.as_uri()) == path
    assert policy.read(str(path)) == path.read_bytes()


def test_policy_refuses_scheme_names(tmp_path):
    _inside(tmp_path)
    policy = ImageRootPolicy([str(tmp_path)])
    for name in ("https://x.example/i.png", "http://x.example/i.png",
                 "data:image/png;base64,AAAA",
                 "myeditor-asset:" + "a" * 64):
        assert policy.resolve(name) is None
        assert policy.read(name) is None


def test_policy_refuses_paths_outside_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(_png_bytes("red"))
    root = tmp_path / "root"
    root.mkdir()
    policy = ImageRootPolicy([str(root)])
    assert policy.resolve(str(secret)) is None
    assert policy.read("../outside/secret.png") is None
    assert policy.read("../../etc/passwd") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
def test_policy_refuses_a_symlink_that_escapes_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(_png_bytes("red"))
    root = tmp_path / "root"
    root.mkdir()
    link = root / "innocent.png"
    link.symlink_to(secret)
    policy = ImageRootPolicy([str(root)])
    assert policy.resolve(str(link)) is None
    assert policy.read(str(link)) is None


def test_empty_policy_refuses_everything(tmp_path):
    path = _inside(tmp_path)
    policy = ImageRootPolicy(())
    assert policy.roots == []
    assert policy.resolve(str(path)) is None
    assert policy.read(str(path)) is None


def test_multiple_roots_all_count(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    a = first / "a.png"
    b = second / "b.png"
    a.write_bytes(_png_bytes())
    b.write_bytes(_png_bytes("red"))
    policy = ImageRootPolicy([str(first), str(second)])
    assert policy.read(str(a)) is not None
    assert policy.read(str(b)) is not None
    # Relative names resolve against the first root only.
    assert policy.resolve("a.png") == a
    assert policy.resolve("b.png") is None


# --------------------------------------------------------------------------- #
# Names that carry their own address
# --------------------------------------------------------------------------- #

def test_portable_sources_are_recognised_and_local_paths_are_not():
    for name in ("https://x.example/a.png", "HTTP://x.example/a.png",
                 "data:image/png;base64,AAAA", "DATA:image/png,%00"):
        assert is_portable_image_source(name)
    for name in ("", "note_media/a.png", "/etc/passwd",
                 "file:///etc/passwd", "myeditor-asset:" + "a" * 64,
                 "javascript:alert(1)"):
        assert not is_portable_image_source(name)


def test_data_uri_bytes_reads_both_encodings():
    payload = _png_bytes()
    encoded = base64.b64encode(payload).decode("ascii")
    assert data_uri_bytes(f"data:image/png;base64,{encoded}") == payload
    assert data_uri_bytes("data:image/svg+xml,%3Csvg%2F%3E") == b"<svg/>"
    assert data_uri_bytes("data:,plain") == b"plain"


def test_data_uri_bytes_refuses_anything_else():
    assert data_uri_bytes("") is None
    assert data_uri_bytes("https://x.example/a.png") is None
    assert data_uri_bytes("data:image/png;base64") is None  # no comma
    assert data_uri_bytes("data:image/png;base64,!!!not base64!!!") is None


def test_a_data_uri_still_faces_the_decode_allowlist():
    # The bytes arrive in the name rather than from disk; they are no
    # more trusted for it.
    svg = base64.b64encode(SVG_BYTES).decode("ascii")
    assert decode_image_bytes(data_uri_bytes(f"data:image/svg+xml;base64,{svg}")) is None
