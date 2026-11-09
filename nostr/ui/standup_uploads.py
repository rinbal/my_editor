# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pictures of a submission (plan 10.2; editorial contract 5, 7).

:class:`BlossomUploads` is the ``upload`` port of
:mod:`nostr.standup.submitting`: it sends the encrypted pictures (SMF1)
to the writer's own media servers through the Media Library's store, the
first server, then the mirrors, never with the plaintext's type.
:func:`thumbnail` makes the revision's small cover (about 128 px, JPEG,
at most 24 KB). Both were part of :mod:`.standup_submit`, which still
offers them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from ..standup import formats
from ..standup.drafting import EncryptedPicture

THUMB_SIDE = 128


def thumbnail(data: bytes) -> Optional[dict]:
    """The revision's ``thumb`` (5) of a picture: at most 128 px, JPEG on
    white, at most 24 KB; None when it cannot be read or made that small."""
    image = QImage.fromData(QByteArray(data))
    if image.isNull():
        return None
    image = image.scaled(min(THUMB_SIDE, image.width()), min(THUMB_SIDE, image.height()),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(QColor("white"))
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    for quality in (82, 68, 52, 36):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        flat.save(buffer, "JPEG", quality)
        raw = bytes(buffer.data())
        if len(raw) <= formats.MAX_THUMB_BYTES:
            return {"mime": "image/jpeg", "data": formats.b64encode(raw)}
    return None


@dataclass
class _Upload:
    on_progress: Callable[[float], None]
    on_done: Callable[[Dict[str, Tuple[str, ...]]], None]
    on_failure: Callable[[str], None]
    expected: Dict[str, str]                        # name -> cipher sha256
    sizes: Dict[str, int]
    sent: Dict[str, int] = field(default_factory=dict)
    servers: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    left: Set[str] = field(default_factory=set)


class BlossomUploads(QObject):
    """The ``upload`` port: each SMF1 file as a plain Blossom upload
    through ``store`` (MediaStore), the first server, then the mirrors.
    Answers the servers that hold each file, by its hash."""

    def __init__(self, store, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._jobs: Dict[str, _Upload] = {}
        store.upload_progress.connect(self._progress)
        store.upload_finished.connect(self._finished)
        store.upload_failed.connect(self._failed)

    def __call__(self, pictures: Sequence[EncryptedPicture], on_progress, on_done,
                 on_failure) -> Callable[[], None]:
        names = {f"{p.sha256[:16]}.smf1": p for p in pictures}
        job = _Upload(on_progress, on_done, on_failure,
                      expected={n: p.sha256 for n, p in names.items()},
                      sizes={n: p.encryptor.cipher_size for n, p in names.items()},
                      left=set(names))
        for name in names:
            self._jobs[name] = job
        for name, picture in names.items():
            if self._jobs.get(name) is job:
                # Contract 7: never the plaintext's type. Sent whatever the
                # library says: a server said it misses a file (12.4).
                self._store.upload_bytes(picture.cipher, name=name,
                                         mime_type="application/octet-stream", force=True,
                                         submission=True)
        return lambda: self._stop(job)

    def _end(self, job: _Upload) -> None:
        for name in [n for n, j in self._jobs.items() if j is job]:
            del self._jobs[name]

    def _stop(self, job: _Upload) -> None:
        names = [n for n, j in self._jobs.items() if j is job]
        self._end(job)
        for name in names:
            self._store.cancel_upload(name)

    def _progress(self, name: str, sent: int, _total: int) -> None:
        job = self._jobs.get(name)
        if job is not None:
            job.sent[name] = sent
            job.on_progress(sum(job.sent.values()) / max(1, sum(job.sizes.values())))

    def _finished(self, name: str, media) -> None:
        job = self._jobs.get(name)
        if job is None:
            return
        if media.hash != job.expected[name]:
            self._failed(name, "")
            return
        job.servers[media.hash] = tuple(u.get("server", "") for u in media.urls
                                        if u.get("server"))
        job.left.discard(name)
        if not job.left:
            self._end(job)
            job.on_done(dict(job.servers))

    def _failed(self, name: str, reason: str) -> None:
        job = self._jobs.get(name)
        if job is not None:
            self._stop(job)
            job.on_failure(reason)


__all__ = ["BlossomUploads", "THUMB_SIDE", "thumbnail"]
