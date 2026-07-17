# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot event queries layered on top of RelayPool subscriptions.

The pattern is the same for kind 0 (user metadata) and kind 10002 (relay
list): subscribe with a tight filter, wait for EOSE or a hard timeout,
hand the caller the most recent event by ``created_at``. We isolate that
shape here so the outbox and metadata loaders both stay tiny.

``fetch_addressable_events`` is a variant for addressable / parameterized-
replaceable kinds (30000–39999, e.g. NIP-23 long-form, NIP-37 drafts):
events are deduplicated by ``(kind, pubkey, d-tag)`` and the newest
event per tuple is returned together.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

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
        # Defense in depth: events can still race in between
        # ``_sub.close()`` and the eventual ``deleteLater`` cycle.
        if self._finished:
            return
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


# --------------------------------------------------------------------------- #
# Addressable-event bulk fetch                                                #
# --------------------------------------------------------------------------- #

# (kind, pubkey, d-tag value)
AddressableKey = Tuple[int, str, str]


def fetch_addressable_events(
    pool: RelayPool,
    relays: List[str],
    filters: List[Dict[str, Any]],
    on_done: Callable[[List[dict]], None],
    *,
    timeout_ms: int = 6_000,
    parent: Optional[QObject] = None,
) -> "_AddressableEventsQuery":
    """Collect newest-per-(kind,pubkey,d) events matching ``filters``.

    Use this for addressable kinds where the user holds many distinct
    documents — every NIP-37 draft is its own ``d``-tag, and we want the
    newest version of *each* draft, not just the newest event overall.

    ``on_done`` is invoked exactly once with the list of winning events
    (order: descending ``created_at``). Events without a ``d``-tag are
    silently skipped — they're not addressable in the protocol sense
    and we couldn't dedupe them anyway.
    """
    return _AddressableEventsQuery(pool, relays, filters, on_done, timeout_ms, parent)


class _AddressableEventsQuery(QObject):
    """Internal helper — see ``fetch_addressable_events``."""

    def __init__(
        self,
        pool: RelayPool,
        relays: List[str],
        filters: List[Dict[str, Any]],
        on_done: Callable[[List[dict]], None],
        timeout_ms: int,
        parent: Optional[QObject],
    ) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self._best: Dict[AddressableKey, dict] = {}
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
        if self._finished:
            return
        try:
            kind = int(event.get("kind", -1))
            pubkey = str(event.get("pubkey", "")).lower()
            created_at = int(event.get("created_at", 0))
        except (TypeError, ValueError):
            return
        if not pubkey:
            return
        d_value: Optional[str] = None
        for tag in event.get("tags", []):
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d":
                d_value = str(tag[1])
                break
        # An empty d-tag is technically the "no parameter" form of an
        # addressable event. Treat it the same as missing — for the
        # drafts use case we explicitly require a non-empty identifier
        # so callers and dedup are aligned with ``parse_wrap_event``.
        if not d_value:
            return

        key: AddressableKey = (kind, pubkey, d_value)
        existing = self._best.get(key)
        if existing is None or created_at > int(existing.get("created_at", 0)):
            self._best[key] = event

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        self._sub.close()
        # Newest first — the drafts panel scrolls from most-recent down.
        ordered = sorted(
            self._best.values(),
            key=lambda e: int(e.get("created_at", 0)),
            reverse=True,
        )
        try:
            self._on_done(ordered)
        finally:
            self.deleteLater()
