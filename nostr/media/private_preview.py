# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Show a private picture without ever writing it down.

Holding the key is the whole point of a private library, so a private
file should look like itself in the grid rather than like a grey square
with an apology. The catch is where the pixels are allowed to exist. The
app has one content-addressed blob cache, shared by the thumbnail grid
and the document asset layer, and an export walks that cache. A
decrypted photo written there is a private photo one export away from a
folder the user hands to somebody else.

So this module decrypts to a QImage and stops. There is no path from
here to a file: no cache write, no temp file, no put_bytes. The
plaintext is a local that is dropped before the function returns, and
the only thing that survives is a QImage the caller shows and Qt frees
with the widget. The ciphertext on disk stays ciphertext.

The decode goes through the same allowlist as every other picture in the
app. A private file is not a trusted file: the bytes came off a media
server and were merely encrypted by a key the user holds, so an SVG or a
decompression bomb inside the envelope is exactly as unwelcome here as
it is anywhere else.

Nothing here logs, and no reason string this module produces carries the
key, the ciphertext or any part of either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, Optional

from PySide6.QtGui import QImage

import image_safety

from .filecrypto import FileCryptoError, decrypt_file, looks_encrypted


# A thumbnail is a thumbnail. Decrypting costs roughly twice the file in
# memory and this runs on the UI thread, so a private file larger than
# the grid could ever usefully show is refused with a sentence rather
# than freezing the window. Matches ``ThumbnailLoader``'s download cap,
# so a blob that cannot be fetched for preview cannot reach this either.
MAX_PREVIEW_BYTES: Final[int] = 25 * 1024 * 1024

# The vocabulary of "why there is no picture", in the same voice as the
# library's existing preview reasons. These are shown to a person.
REASON_NOT_ENCRYPTED: Final[str] = "these bytes are not an encrypted file"
REASON_TOO_LARGE: Final[str] = "this file is too large to preview"
REASON_WRONG_KEY: Final[str] = "the key in your library does not open this file"
REASON_NOT_AN_IMAGE: Final[str] = "the file inside is not an image this app can show"


@dataclass(frozen=True)
class PreviewOutcome:
    """A decrypted picture, or the reason there is not one.

    Exactly one of the two is meaningful. ``reason`` is written for a
    person and never repeats bytes from the envelope.
    """

    image: Optional[QImage] = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.image is not None


def preview_from_envelope(
    envelope: bytes,
    key_hex: str,
    *,
    decoder: Optional[Callable[[bytes], Optional[QImage]]] = None,
) -> PreviewOutcome:
    """Decrypt ``envelope`` and decode it, in memory, for display only.

    ``decoder`` exists so a test can drive the failure branches without
    a real image; production passes nothing and gets the app's own
    allowlist decoder.

    Returns a :class:`PreviewOutcome` rather than raising, because a
    library of two hundred files should lose one tile to a bad record,
    not the whole grid.
    """
    if not looks_encrypted(envelope):
        # Cheap and deliberately weak, exactly as ``looks_encrypted``
        # documents. It only decides whether decrypting is worth trying.
        return PreviewOutcome(reason=REASON_NOT_ENCRYPTED)
    if len(envelope) > MAX_PREVIEW_BYTES:
        return PreviewOutcome(reason=REASON_TOO_LARGE)

    try:
        plaintext = decrypt_file(bytes(envelope), key_hex)
    except FileCryptoError:
        # The exception text is not repeated. Nothing in it carries a key
        # today, and a preview tooltip is the last place that should be
        # where a change to that first shows up.
        return PreviewOutcome(reason=REASON_WRONG_KEY)

    try:
        decode = decoder or image_safety.decode_image_bytes
        image = decode(plaintext)
    finally:
        # The pixels have no business outliving the decode. Qt has its
        # own copy by now, and this one is the private original.
        del plaintext

    if image is None or image.isNull():
        return PreviewOutcome(reason=REASON_NOT_AN_IMAGE)
    return PreviewOutcome(image=image)
