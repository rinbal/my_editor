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

Animated GIFs are passed through untouched. Re-encoding one through a
still-image decoder would silently reduce it to its first frame, and
GIF is not a format that carries location or camera metadata, so the
trade runs the wrong way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from PySide6.QtCore import QBuffer, QIODevice

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

# Passed through as-is, with the reason recorded.
_PASS_THROUGH: Final[dict] = {
    "image/gif": "re-encoding would drop the animation",
}

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

    passthrough_reason = _PASS_THROUGH.get(mime)
    if passthrough_reason:
        return ScrubResult(
            data=bytes(data), mime=mime, scrubbed=False, note=passthrough_reason,
        )

    target = _REENCODE_AS.get(mime)
    if target is None:
        # Sniffed as an image, but not one we are willing to re-encode.
        # Publishing it unscrubbed would defeat the point of this module.
        raise ScrubError(f"{mime} cannot be prepared for publication")

    image = image_safety.decode_image_bytes(bytes(data))
    if image is None or image.isNull():
        raise ScrubError("this image could not be read")

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


def would_scrub(data: bytes) -> Optional[bool]:
    """Whether publishing these bytes would re-encode them.

    None when they are not publishable at all, so a caller can tell
    "nothing to strip" apart from "cannot publish this".
    """
    mime = image_safety.sniff_image_mime(bytes(data)) if data else None
    if mime is None:
        return None
    if mime in _PASS_THROUGH:
        return False
    return mime in _REENCODE_AS or None
