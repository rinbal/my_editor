# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strip what a picture reveals before it becomes public.

A photo straight off a phone or a camera roll carries the time it was
taken, the device that took it, and often the coordinates of wherever
that was. None of that is visible in the picture, all of it travels in
the file, and a home address published by accident cannot be taken back
once other people have the bytes. So the publish path scrubs, and it
scrubs by default rather than on request.

The method is deliberately blunt: decode the pixels and write a fresh
file from them. Anything that was not pixels does not survive, which
means this cannot be defeated by a metadata container nobody thought to
strip. It costs a re-encode, which is acceptable exactly once, on the
copy being published.

Decoding goes through the same allowlist the rest of the app uses, so a
format outside it, an SVG that could pull in external resources, or a
header claiming an implausible size is refused here too rather than
being re-encoded into something that looks trustworthy.

A GIF is not re-encoded: a still-image decoder would silently reduce an
animation to its first frame. GIF does carry text a person cannot see,
though: comment blocks, and application blocks such as XMP, which can
hold a location. Those are cut out of the file, and every frame (with
its timing and the loop) is kept byte for byte.

``can_scrub`` says which kinds of file this module can make safe, so the
question before a private file is shared promises the removal only for
those, and the others are not offered for sharing at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

import image_safety


# Formats we re-encode, mapped to what we write them back out as. JPEG
# stays JPEG so a photo does not balloon into a lossless format; the
# rest become PNG, which every client can read and which carries no
# camera metadata of its own.
_REENCODE_AS: Final[dict] = {
    "image/jpeg": (b"JPEG", "image/jpeg"),
    "image/png": (b"PNG", "image/png"),
    "image/webp": (b"PNG", "image/png"),
    "image/bmp": (b"PNG", "image/png"),
}

# GIF application blocks that are part of the animation (the loop), by
# their identifier and code: kept. Every other application block (XMP,
# colour profiles, vendors' own) is not.
_GIF_KEPT_APPLICATIONS: Final[frozenset] = frozenset({b"NETSCAPE2.0", b"ANIMEXTS1.0"})

# High enough that a re-encoded photo is visually unchanged, low enough
# that publishing does not inflate the file.
_JPEG_QUALITY: Final[int] = 92


class ScrubError(Exception):
    """The bytes could not be made safe to publish."""


@dataclass(frozen=True)
class ScrubResult:
    """The bytes to publish, and whether anything was stripped."""

    data: bytes
    mime: str
    scrubbed: bool
    note: str = ""


def scrub_for_publication(data: bytes, declared_mime: str = "") -> ScrubResult:
    """Return bytes safe to hand to the public internet.

    ``declared_mime`` is only a hint and is never trusted: the real type
    is sniffed from the bytes, because a file claiming to be a PNG
    decides nothing about what it actually contains.

    Raises :class:`ScrubError` for anything this app will not publish,
    which is deliberately the same set it will not decode.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ScrubError("there are no bytes to publish")

    mime = image_safety.sniff_image_mime(bytes(data))
    if mime is None:
        raise ScrubError("this file is not an image this app can publish")

    if mime == "image/gif":
        return ScrubResult(data=_gif_frames_only(bytes(data)), mime=mime, scrubbed=True)

    target = _REENCODE_AS.get(mime)
    if target is None:
        # Sniffed as an image, but not one we are willing to re-encode.
        # Publishing it unscrubbed would defeat the point of this module.
        raise ScrubError(f"{mime} cannot be prepared for publication")

    # Decoded upright: the camera's orientation (EXIF) goes with the rest
    # of what is not pixels, so the copy is turned the way it said first,
    # or a phone's portrait photo would be published on its side.
    image = image_safety.decode_image_bytes(bytes(data))
    if image is None or image.isNull():
        raise ScrubError("this image could not be read")

    image = _pixels_only(image)
    fmt, out_mime = target
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    ok = (
        image.save(buffer, fmt.decode("ascii"), _JPEG_QUALITY)
        if fmt == b"JPEG"
        else image.save(buffer, fmt.decode("ascii"))
    )
    buffer.close()
    if not ok:
        raise ScrubError("this image could not be re-encoded")

    out = bytes(buffer.data())
    if not out:
        raise ScrubError("re-encoding produced nothing")
    return ScrubResult(data=out, mime=out_mime, scrubbed=True)


def _pixels_only(image: QImage) -> QImage:
    """A new image holding the pixels and nothing else.

    QImage keeps the text a decoder found (PNG text chunks, a JPEG
    comment, XMP) and writes it back out on save, location included; so
    the pixel rows are copied into a fresh image that never had any. A
    plain copy of the bytes keeps the result the same on every run."""
    # Kept opaque when it was, so a photo does not grow an alpha channel.
    form = (QImage.Format.Format_ARGB32 if image.hasAlphaChannel()
            else QImage.Format.Format_RGB32)
    source = image.convertToFormat(form)
    pixels = bytes(source.constBits())
    clean = QImage(pixels, source.width(), source.height(), source.bytesPerLine(),
                   form).copy()
    # A new image takes its density from the screen; the picture's own
    # keeps the file the same wherever it is made.
    clean.setDotsPerMeterX(source.dotsPerMeterX())
    clean.setDotsPerMeterY(source.dotsPerMeterY())
    return clean


def can_scrub(mime: str) -> bool:
    """Whether a file of this type can be made safe to publish here: its
    camera and location details removed."""
    return mime == "image/gif" or mime in _REENCODE_AS


def _gif_frames_only(data: bytes) -> bytes:
    """The GIF ``data`` with only what showing it needs: the header and
    palettes, each frame, its graphic control (delay, transparency) and
    the loop, copied as they are. Every other block goes, whatever its
    label: comments, application blocks such as XMP, plain text, labels
    no decoder knows, and anything after the loop count in a loop block.
    Raises ScrubError when it cannot be read to its end."""
    def sub_blocks(at: int):
        """Where the run of sub-blocks starting at ``at`` ends, and where
        its first loop sub-block (three bytes, the first 1) starts, if
        it has one."""
        loop = None
        while True:
            if at >= len(data):
                raise ScrubError("this GIF ends early")
            size = data[at]
            if size == 0:
                return at + 1, loop
            if size == 3 and loop is None and at + 1 < len(data) and data[at + 1] == 1:
                loop = at
            at += 1 + size

    if len(data) < 13:
        raise ScrubError("this GIF ends early")
    flags = data[10]
    at = 13 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)
    out = bytearray(data[:at])
    while at < len(data):
        introducer = data[at]
        if introducer == 0x3B:                         # trailer
            return bytes(out) + b"\x3b"
        if introducer == 0x2C:                         # a frame
            if at + 10 > len(data):
                raise ScrubError("this GIF ends early")
            local = data[at + 9]
            start = at
            at += 10 + (3 * 2 ** ((local & 7) + 1) if local & 0x80 else 0) + 1
            at, _loop = sub_blocks(at)
            out += data[start:at]
            continue
        if introducer != 0x21 or at + 2 > len(data):
            raise ScrubError("this GIF has a block it cannot read")
        label = data[at + 1]
        end, loop = sub_blocks(at + 2)
        size = data[at + 2]
        first = data[at + 3:at + 3 + size]
        if label == 0xF9 and size == 4:                # graphic control
            out += b"\x21\xf9\x04" + first + b"\x00"
        elif label == 0xFF and first in _GIF_KEPT_APPLICATIONS and loop is not None:
            out += b"\x21\xff" + data[at + 2:at + 3 + size] + data[loop:loop + 4] + b"\x00"
        at = end
    raise ScrubError("this GIF ends early")


def would_scrub(data: bytes) -> Optional[bool]:
    """Whether publishing these bytes would re-encode them.

    None when they are not publishable at all, so a caller can tell
    "nothing to strip" apart from "cannot publish this".
    """
    mime = image_safety.sniff_image_mime(bytes(data)) if data else None
    if mime is None:
        return None
    return True if can_scrub(mime) else None
