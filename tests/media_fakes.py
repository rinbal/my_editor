# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fakes for the document-asset test-suite.

Everything settles synchronously and nothing touches a network, a
signer, a relay or the real home directory, so the state-machine tests
are deterministic without an event loop.

The two fakes stand in for the only external surfaces ``AssetManager``
depends on: the content-addressed blob cache (``ThumbnailLoader`` in the
app) and the upload orchestrator (``MediaStore``).
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal


# A real 3x2 PNG, so magic-byte sniffing behaves exactly as it does in
# production. Kept inline because a fixture file would be one more thing
# a test could fail on.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAACXBIWXMAAA7EAAAOxAGVKw4b"
    "AAAAFUlEQVQImWP8b5zGwMDAwMDAxAADAB2FAZztVg6VAAAAAElFTkSuQmCC"
)
PNG_SHA = hashlib.sha256(PNG_BYTES).hexdigest()
PNG_WIDTH, PNG_HEIGHT = 3, 2

GIF_BYTES = b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00;"
GIF_SHA = hashlib.sha256(GIF_BYTES).hexdigest()

SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg'><image href='/etc/passwd'/></svg>"
TEXT_BYTES = b"just some prose, not a picture at all"


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeImage:
    """Stands in for a QImage: only the size is ever read."""

    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


def fake_decoder(data: bytes):
    """Decoder seam: knows the two byte strings this suite uses."""
    if data.startswith(b"\x89PNG"):
        return FakeImage(PNG_WIDTH, PNG_HEIGHT)
    if data.startswith(b"GIF8"):
        return FakeImage(1, 1)
    return None


class FakeBlobStore(QObject):
    """Byte cache behind a directory, with the BlobStore surface."""

    ready = Signal(str, str, object)    # sha256, local_path, pixmap
    failed = Signal(str, str)           # sha256, reason

    def __init__(self, root, parent=None) -> None:
        super().__init__(parent)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.puts: list[str] = []       # every sha written, in order
        self.loads: list[tuple] = []    # every fetch asked for
        self.put_error: OSError | None = None

    # -- BlobStore protocol ------------------------------------------------

    def cache_path(self, sha256: str) -> Path:
        return self.root / sha256.lower()

    def has(self, sha256: str) -> bool:
        return self.cache_path(sha256).is_file()

    def put_bytes(self, data: bytes) -> str:
        if self.put_error is not None:
            raise self.put_error
        sha = sha_of(data)
        self.cache_path(sha).write_bytes(data)
        self.puts.append(sha)
        return sha

    def load(self, sha256: str, url: str) -> None:
        self.loads.append((sha256, url))

    # -- test drivers ------------------------------------------------------

    def deliver(self, sha256: str, data: bytes = PNG_BYTES, pixmap=None) -> None:
        """Complete a fetch: cache the bytes and emit ``ready``."""
        self.cache_path(sha256).write_bytes(data)
        image = pixmap if pixmap is not None else FakeImage(PNG_WIDTH, PNG_HEIGHT)
        self.ready.emit(sha256, str(self.cache_path(sha256)), image)

    def refuse(self, sha256: str, reason: str = "network error") -> None:
        self.failed.emit(sha256, reason)

    def evict(self, sha256: str) -> None:
        """Drop cached bytes the way a user clearing the cache would."""
        try:
            self.cache_path(sha256).unlink()
        except OSError:
            pass


class FakeUploader(QObject):
    """MediaStore's observed surface: a library map plus three signals."""

    upload_status = Signal(str, str)      # name, status
    upload_finished = Signal(str, object)  # name, media record
    upload_failed = Signal(str, str)      # name, reason

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.files: dict = {}
        self.calls: list = []
        # When set, ``upload_bytes`` fails inside the call, the way the
        # real store does when no profile or no server is available.
        self.fail_synchronously: str | None = None

    def upload_bytes(self, body: bytes, *, name: str,
                     mime_type: str = "application/octet-stream") -> None:
        self.calls.append(SimpleNamespace(name=name, mime_type=mime_type, body=body))
        if self.fail_synchronously is not None:
            self.upload_failed.emit(name, self.fail_synchronously)

    # -- test drivers ------------------------------------------------------

    def status(self, name: str, status: str) -> None:
        self.upload_status.emit(name, status)

    def finish(self, name: str, media) -> None:
        self.upload_finished.emit(name, media)

    def fail(self, name: str, reason: str) -> None:
        self.upload_failed.emit(name, reason)


def fake_message_box(click: str | None = None):
    """A QMessageBox stand-in class plus the list of dialogs it builds.

    Returns ``(cls, shown)``. Patch ``cls`` over the QMessageBox name a
    module imported, and every modal it raises lands in ``shown``
    instead of blocking. ``click`` names the button label to answer
    with; with no match the dialog answers with its own default button,
    which is what dismissing a modal does.

    A fresh class per call, so no answer can leak between tests.
    """
    shown: list = []

    class _Box:
        # Roles and standard buttons are only carried, never inspected,
        # so plain sentinels stand in for the Qt enums.
        AcceptRole = "accept"
        RejectRole = "reject"
        DestructiveRole = "destructive"
        Cancel = "cancel"
        Warning = "warning"

        def __init__(self, parent=None) -> None:
            self.parent = parent
            self.title = ""
            self.text = ""
            self.buttons: list = []
            self.default = None
            self.checkbox = None
            self._clicked = None
            shown.append(self)

        def setWindowTitle(self, title) -> None:
            self.title = title

        def setText(self, text) -> None:
            self.text = text

        def setIcon(self, icon) -> None:
            pass

        def setCheckBox(self, box) -> None:
            self.checkbox = box

        def setDefaultButton(self, button) -> None:
            self.default = button

        def addButton(self, label, role=None):
            button = SimpleNamespace(label=str(label), role=role)
            self.buttons.append(button)
            return button

        def exec(self) -> int:
            named = [b for b in self.buttons if b.label == click]
            self._clicked = named[0] if named else self.default
            return 0

        def clickedButton(self):
            return self._clicked

    return _Box, shown


def make_media(sha256: str, *, url=None, servers=("https://cdn.example",),
               mime="image/png", size=0):
    """A MediaFile-shaped record: hash, url, urls, mime_type, size."""
    primary = url if url is not None else f"{servers[0]}/{sha256}"
    return SimpleNamespace(
        hash=sha256,
        url=primary,
        urls=[{"server": s, "url": f"{s}/{sha256}"} for s in servers],
        mime_type=mime,
        size=size,
    )
