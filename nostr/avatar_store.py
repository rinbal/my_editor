"""In-memory avatar cache + throttled batch loader.

Two cooperating objects:

  ``AvatarStore``       Holds the loaded ``QPixmap`` for each pubkey we've
                        already resolved, and emits ``avatar_added`` whenever
                        a new one lands. Widgets subscribe to that signal so
                        they can repaint themselves when a previously-blank
                        row gains an avatar.

  ``AvatarBatchLoader`` A thin throttle on top of ``AvatarLoader``. Holding
                        the slot count at a sane ceiling matters when the
                        contact-list pass resolves hundreds of pubkeys all
                        at once — opening 200 parallel HTTPS sockets would
                        annoy the OS, the user's router, and the image
                        hosts in roughly that order.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from .metadata import AvatarLoader


# Cap on simultaneous HTTP requests. Matches common browser per-host limits;
# the queue absorbs anything beyond.
DEFAULT_MAX_CONCURRENT: int = 6


# --------------------------------------------------------------------------- #
# AvatarStore                                                                 #
# --------------------------------------------------------------------------- #

class AvatarStore(QObject):
    """Pubkey -> QPixmap, with a change signal so views can repaint.

    Dict-shaped enough for the existing call sites (``.get`` / ``in``) so
    widgets that used to receive a plain dict can swap in an ``AvatarStore``
    transparently — they just gain the option to subscribe to
    ``avatar_added`` for live refresh.
    """

    avatar_added = Signal(str, object)  # pubkey_hex, QPixmap

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._map: dict[str, QPixmap] = {}

    # -- dict-shaped surface ----------------------------------------------

    def get(self, pubkey_hex: str, default: Optional[QPixmap] = None) -> Optional[QPixmap]:
        return self._map.get(pubkey_hex, default)

    def __contains__(self, pubkey_hex: object) -> bool:
        return isinstance(pubkey_hex, str) and pubkey_hex in self._map

    def __len__(self) -> int:
        return len(self._map)

    # -- mutation ---------------------------------------------------------

    def put(self, pubkey_hex: str, pixmap: QPixmap) -> None:
        if pixmap is None or pixmap.isNull():
            return
        self._map[pubkey_hex] = pixmap
        self.avatar_added.emit(pubkey_hex, pixmap)

    def pop(self, pubkey_hex: str, default: Optional[QPixmap] = None) -> Optional[QPixmap]:
        return self._map.pop(pubkey_hex, default)

    def clear(self) -> None:
        self._map.clear()


# --------------------------------------------------------------------------- #
# AvatarBatchLoader                                                           #
# --------------------------------------------------------------------------- #

class AvatarBatchLoader(QObject):
    """Throttled wrapper around ``AvatarLoader``.

    Maintains a FIFO queue and a slot count. Each call to ``request``
    enqueues at most one fetch per pubkey for the lifetime of this batcher
    (re-requesting the same pubkey is a no-op even after success).

    Pipe both ``ready`` and ``failed`` from the underlying loader so the
    batcher's slot accounting stays correct regardless of outcome.
    """

    ready = Signal(str, object)   # forwarded from AvatarLoader

    def __init__(
        self,
        loader: AvatarLoader,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._loader = loader
        self._max = max(1, max_concurrent)
        self._queue: deque[tuple[str, str]] = deque()
        self._in_flight: set[str] = set()
        self._requested: set[str] = set()

        self._loader.ready.connect(self._on_ready)
        self._loader.failed.connect(self._on_failed)

    def request(self, pubkey_hex: str, picture_url: str) -> None:
        """Queue an avatar fetch. No-op for any pubkey already requested."""
        if not picture_url or pubkey_hex in self._requested:
            return
        self._requested.add(pubkey_hex)
        if len(self._in_flight) < self._max:
            self._start(pubkey_hex, picture_url)
        else:
            self._queue.append((pubkey_hex, picture_url))

    def _start(self, pubkey_hex: str, picture_url: str) -> None:
        self._in_flight.add(pubkey_hex)
        self._loader.load(pubkey_hex, picture_url)

    def _release_slot(self, pubkey_hex: str) -> None:
        self._in_flight.discard(pubkey_hex)
        if self._queue:
            self._start(*self._queue.popleft())

    def _on_ready(self, pubkey_hex: str, pixmap) -> None:
        self._release_slot(pubkey_hex)
        # Forward only for pubkeys we actually requested via this batcher —
        # otherwise we'd amplify someone else's direct ``loader.load`` calls.
        if pubkey_hex in self._requested:
            self.ready.emit(pubkey_hex, pixmap)

    def _on_failed(self, pubkey_hex: str, _reason: str) -> None:
        self._release_slot(pubkey_hex)
