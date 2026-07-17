"""AvatarStore + AvatarBatchLoader behavior.

The HTTP fetch path is exercised via a fake AvatarLoader subclass — we
don't hit the network in unit tests. What we're verifying:

  * AvatarStore.put emits avatar_added and starts answering .get
  * AvatarBatchLoader respects max-concurrent
  * The queue drains as ready/failed signals arrive
  * Re-requesting the same pubkey is a no-op
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from nostr.avatar_store import AvatarBatchLoader, AvatarStore


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --------------------------------------------------------------------------- #
# Fake AvatarLoader that records calls and lets the test drive signals       #
# --------------------------------------------------------------------------- #

class FakeAvatarLoader(QObject):
    ready = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def load(self, pubkey_hex: str, url: str) -> None:
        self.calls.append((pubkey_hex, url))


# --------------------------------------------------------------------------- #
# AvatarStore                                                                 #
# --------------------------------------------------------------------------- #

def test_store_put_emits_signal_and_persists_in_memory(qapp):
    store = AvatarStore()
    received: list[tuple[str, object]] = []
    store.avatar_added.connect(lambda pk, pix: received.append((pk, pix)))

    pix = QPixmap(4, 4)
    pix.fill()
    store.put("ab" * 32, pix)

    assert received == [("ab" * 32, pix)]
    assert "ab" * 32 in store
    assert store.get("ab" * 32) is pix
    assert store.get("missing") is None


def test_store_ignores_null_pixmap(qapp):
    store = AvatarStore()
    received = []
    store.avatar_added.connect(lambda *a: received.append(a))

    store.put("ab" * 32, QPixmap())  # null
    assert received == []
    assert "ab" * 32 not in store


def test_store_pop_removes_and_returns_pixmap(qapp):
    """Sign-out drops the active profile's avatar via .pop. The store has
    to answer it like a dict, or the sign-out handler dies partway through
    and the profile stays on screen."""
    store = AvatarStore()
    pix = QPixmap(4, 4)
    pix.fill()
    store.put("ab" * 32, pix)

    assert store.pop("ab" * 32) is pix
    assert "ab" * 32 not in store
    assert store.get("ab" * 32) is None


def test_store_pop_missing_key_returns_default(qapp):
    store = AvatarStore()
    assert store.pop("missing") is None
    assert store.pop("missing", "fallback") == "fallback"


# --------------------------------------------------------------------------- #
# AvatarBatchLoader                                                           #
# --------------------------------------------------------------------------- #

def test_batcher_dispatches_up_to_max_concurrent(qapp):
    loader = FakeAvatarLoader()
    batcher = AvatarBatchLoader(loader, max_concurrent=3)

    for i in range(5):
        batcher.request(f"{i:02d}" * 32, f"https://example/{i}.png")

    # Only 3 started; 2 are queued.
    assert len(loader.calls) == 3
    assert [c[0] for c in loader.calls] == ["00" * 32, "01" * 32, "02" * 32]


def test_batcher_drains_queue_on_ready(qapp):
    loader = FakeAvatarLoader()
    batcher = AvatarBatchLoader(loader, max_concurrent=2)
    forwarded: list[str] = []
    batcher.ready.connect(lambda pk, _pix: forwarded.append(pk))

    for i in range(4):
        batcher.request(f"{i:02d}" * 32, f"https://example/{i}.png")
    assert len(loader.calls) == 2

    pix = QPixmap(2, 2); pix.fill()
    loader.ready.emit("00" * 32, pix)
    # Slot freed → next queued kicks off
    assert len(loader.calls) == 3
    assert loader.calls[2][0] == "02" * 32

    loader.ready.emit("01" * 32, pix)
    assert len(loader.calls) == 4
    assert loader.calls[3][0] == "03" * 32

    # Forwarded only fires for the two successes so far.
    assert forwarded == ["00" * 32, "01" * 32]


def test_batcher_drains_queue_on_failure(qapp):
    """A failed download must also free a slot — otherwise a few bad URLs
    can wedge the entire queue."""
    loader = FakeAvatarLoader()
    batcher = AvatarBatchLoader(loader, max_concurrent=1)

    batcher.request("00" * 32, "https://bad/0.png")
    batcher.request("01" * 32, "https://good/1.png")

    assert len(loader.calls) == 1
    loader.failed.emit("00" * 32, "kaboom")
    assert len(loader.calls) == 2
    assert loader.calls[1][0] == "01" * 32


def test_batcher_dedupes_repeat_requests(qapp):
    loader = FakeAvatarLoader()
    batcher = AvatarBatchLoader(loader, max_concurrent=4)

    pk = "ab" * 32
    batcher.request(pk, "https://example/a.png")
    batcher.request(pk, "https://example/a.png")
    batcher.request(pk, "https://example/different.png")

    assert len(loader.calls) == 1
    assert loader.calls[0] == (pk, "https://example/a.png")


def test_batcher_skips_empty_url(qapp):
    loader = FakeAvatarLoader()
    batcher = AvatarBatchLoader(loader, max_concurrent=2)

    batcher.request("00" * 32, "")
    batcher.request("01" * 32, "https://example/1.png")

    # Empty-URL request never reaches the loader.
    assert loader.calls == [("01" * 32, "https://example/1.png")]
