"""One-shot event queries layered on top of RelayPool subscriptions.

The pattern is the same for kind 0 (user metadata) and kind 10002 (relay
list): subscribe with a tight filter, wait for EOSE or a hard timeout,
hand the caller the most recent event by ``created_at``. We isolate that
shape here so the outbox and metadata loaders both stay tiny.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QTimer

from .relay import RelayPool, Subscription


def fetch_latest_event(
    pool: RelayPool,
    relays: List[str],
    filters: List[Dict[str, Any]],
    on_done: Callable[[Optional[dict]], None],
    *,
    timeout_ms: int = 6_000,
    parent: Optional[QObject] = None,
) -> "_LatestEventQuery":
    """Subscribe, collect matching events until EOSE-or-timeout, callback once.

    ``on_done`` receives the event with the largest ``created_at`` value, or
    ``None`` if no event arrived. It is invoked exactly once.

    The returned object owns the subscription and timer; the caller may
    discard it. It cleans itself up after firing the callback.
    """
    return _LatestEventQuery(pool, relays, filters, on_done, timeout_ms, parent)


class _LatestEventQuery(QObject):
    """Internal helper — see ``fetch_latest_event`` for the public API."""

    def __init__(
        self,
        pool: RelayPool,
        relays: List[str],
        filters: List[Dict[str, Any]],
        on_done: Callable[[Optional[dict]], None],
        timeout_ms: int,
        parent: Optional[QObject],
    ) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self._best: Optional[dict] = None
        self._finished = False

        self._sub = pool.subscribe(relays, filters)
        self._sub.event.connect(self._on_event)
        self._sub.eose.connect(self._finish)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(timeout_ms)
        self._timer.timeout.connect(self._finish)
        self._timer.start()

    def _on_event(self, event: dict) -> None:
        # Replaceable-event semantics: keep the newest by created_at.
        try:
            ts = int(event.get("created_at", 0))
        except (TypeError, ValueError):
            return
        if self._best is None or ts > int(self._best.get("created_at", 0)):
            self._best = event

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        self._sub.close()
        try:
            self._on_done(self._best)
        finally:
            self.deleteLater()
