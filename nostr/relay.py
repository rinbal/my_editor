"""Nostr relay WebSocket pool — Qt-native, no asyncio.

References:
  - NIP-01: https://github.com/nostr-protocol/nips/blob/master/01.md
  - NIP-20 (OK response): folded into NIP-01

Wire messages we send (client -> relay):
  ["EVENT", <event>]                           # publish
  ["REQ", <sub_id>, <filter>, ...]             # subscribe
  ["CLOSE", <sub_id>]                          # cancel subscription

Wire messages we handle (relay -> client):
  ["OK", <event_id>, <bool>, <message>]        # publish ack
  ["EVENT", <sub_id>, <event>]                 # stored or live event
  ["EOSE", <sub_id>]                           # end of stored events
  ["NOTICE", <message>]                        # human-readable info / error
  ["CLOSED", <sub_id>, <message>]              # subscription terminated
  ["AUTH", <challenge>]                        # NIP-42 — not implemented yet

Design notes:
  - One Relay per URL, shared across all jobs. WebSocket stays warm so
    repeated publishes don't re-handshake.
  - PublishJob fans out a single EVENT to N relays and tracks per-relay
    state. Eager semantics: ``first_accept`` fires the instant one relay
    OKs; ``all_done`` fires after every relay has reported (success,
    rejection, error, or per-relay timeout).
  - No reconnection logic in this chunk. If a relay drops, the next
    publish opens a fresh socket.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket


# Per-relay ceiling for a publish ack. Buho_go uses 8 s; we match it.
DEFAULT_PUBLISH_TIMEOUT_MS: int = 8000

# How long to give a freshly-opened socket to complete its TLS handshake
# before we declare it dead. Folded into the publish timeout — this is
# just the wait for ``connected`` to fire.
DEFAULT_CONNECT_TIMEOUT_MS: int = 5000


PublishResult = Tuple[str, bool, str]  # (url, ok, message)


# --------------------------------------------------------------------------- #
# Relay — one WebSocket connection to one URL                                 #
# --------------------------------------------------------------------------- #

class Relay(QObject):
    """A single, long-lived WebSocket to one relay URL.

    Signals:
      connected()          — handshake completed
      disconnected()       — socket closed for any reason
      message(list)        — a parsed JSON array from the relay
      error(str)           — connection or socket-level error
    """

    connected = Signal()
    disconnected = Signal()
    message = Signal(list)
    error = Signal(str)

    def __init__(self, url: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._url = url
        self._ws = QWebSocket(parent=self)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text_message)
        self._ws.errorOccurred.connect(self._on_error)
        self._connected = False
        self._opening = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_opening(self) -> bool:
        return self._opening

    def open(self) -> None:
        """Begin the TLS handshake. No-op if already open or opening."""
        if self._connected or self._opening:
            return
        self._opening = True
        self._ws.open(QUrl(self._url))

    def close(self) -> None:
        self._ws.close()

    def send(self, message: list) -> bool:
        """Serialize and send a JSON message. Returns False if not connected."""
        if not self._connected:
            return False
        # Compact JSON — relays parse anything legal, but this is what every
        # mainstream client emits and keeps the bytes small.
        text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        return self._ws.sendTextMessage(text) > 0

    # -- Qt slots -----------------------------------------------------------

    def _on_connected(self) -> None:
        self._opening = False
        self._connected = True
        self.connected.emit()

    def _on_disconnected(self) -> None:
        self._opening = False
        self._connected = False
        self.disconnected.emit()

    def _on_text_message(self, text: str) -> None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, list) or not parsed:
            return
        self.message.emit(parsed)

    def _on_error(self, _code) -> None:
        self._opening = False
        self.error.emit(self._ws.errorString())


# --------------------------------------------------------------------------- #
# PublishJob — one EVENT, N relays, eager-first-accept                        #
# --------------------------------------------------------------------------- #

class PublishJob(QObject):
    """Track one EVENT publish across N relays in parallel.

    Signals:
      first_accept(str)              — URL of the first relay that OK'd ok=True.
                                       Fires at most once. May never fire if
                                       every relay rejects.
      relay_result(str, bool, str)   — per-relay outcome: (url, ok, message).
      all_done(list)                 — list of PublishResult tuples in the
                                       order results landed. Fires exactly once.
    """

    first_accept = Signal(str)
    relay_result = Signal(str, bool, str)
    all_done = Signal(list)

    def __init__(
        self,
        pool: "RelayPool",
        urls: List[str],
        event: dict,
        timeout_ms: int = DEFAULT_PUBLISH_TIMEOUT_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if "id" not in event or "sig" not in event:
            raise ValueError("PublishJob requires a fully signed event")

        self._pool = pool
        self._event = event
        self._event_id = event["id"]
        self._timeout_ms = timeout_ms
        # Preserve order but dedupe (a caller might pass the same URL twice).
        self._urls: List[str] = list(dict.fromkeys(_normalize(u) for u in urls))
        self._pending: set[str] = set(self._urls)
        self._results: List[PublishResult] = []
        self._first_accept_emitted = False
        self._sent: set[str] = set()
        # Track our signal connections so we can disconnect cleanly on finish.
        self._connections: List[tuple[Relay, str, object]] = []

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(timeout_ms)
        self._timer.timeout.connect(self._on_overall_timeout)

        if not self._urls:
            # Edge case: empty URL list — fire all_done on the next tick
            # so callers can wire up signals before the result lands.
            QTimer.singleShot(0, lambda: self.all_done.emit([]))
            return

        for url in self._urls:
            self._start_one(url)
        self._timer.start()

    # -- per-relay wiring ---------------------------------------------------

    def _start_one(self, url: str) -> None:
        relay = self._pool.get_or_create(url)

        msg_slot = lambda payload, u=url: self._on_message(u, payload)
        disc_slot = lambda u=url: self._on_disconnected(u)
        err_slot = lambda err, u=url: self._on_error(u, err)
        relay.message.connect(msg_slot)
        relay.disconnected.connect(disc_slot)
        relay.error.connect(err_slot)
        self._connections.append((relay, "message", msg_slot))
        self._connections.append((relay, "disconnected", disc_slot))
        self._connections.append((relay, "error", err_slot))

        if relay.is_connected:
            self._send(url)
            return

        conn_slot = lambda u=url: self._send(u)
        relay.connected.connect(conn_slot)
        self._connections.append((relay, "connected", conn_slot))
        relay.open()

    def _send(self, url: str) -> None:
        if url in self._sent or url not in self._pending:
            return
        self._sent.add(url)
        relay = self._pool.get_or_create(url)
        if not relay.send(["EVENT", self._event]):
            self._finish(url, False, "send failed (not connected)")

    # -- relay signal handlers ---------------------------------------------

    def _on_message(self, url: str, msg: list) -> None:
        if url not in self._pending:
            return
        # We only care about ["OK", <event_id>, <bool>, <message>] for this event.
        if len(msg) < 3 or msg[0] != "OK" or msg[1] != self._event_id:
            return
        ok = bool(msg[2])
        message = str(msg[3]) if len(msg) >= 4 else ""
        self._finish(url, ok, message)

    def _on_disconnected(self, url: str) -> None:
        if url in self._pending:
            self._finish(url, False, "relay disconnected before ack")

    def _on_error(self, url: str, err: str) -> None:
        if url in self._pending:
            self._finish(url, False, f"socket error: {err}")

    def _on_overall_timeout(self) -> None:
        for url in list(self._pending):
            self._finish(url, False, "publish timeout")

    # -- finalize -----------------------------------------------------------

    def _finish(self, url: str, ok: bool, message: str) -> None:
        if url not in self._pending:
            return
        self._pending.remove(url)
        self._results.append((url, ok, message))
        self.relay_result.emit(url, ok, message)
        if ok and not self._first_accept_emitted:
            self._first_accept_emitted = True
            self.first_accept.emit(url)
        if not self._pending:
            self._timer.stop()
            self._disconnect_all()
            self.all_done.emit(list(self._results))

    def _disconnect_all(self) -> None:
        for relay, sig_name, slot in self._connections:
            try:
                getattr(relay, sig_name).disconnect(slot)
            except (RuntimeError, TypeError):
                # Already disconnected, relay destroyed, etc. — harmless.
                pass
        self._connections.clear()


# --------------------------------------------------------------------------- #
# RelayPool — dict of Relay keyed by normalized URL                           #
# --------------------------------------------------------------------------- #

class RelayPool(QObject):
    """Process-wide cache of WebSocket connections.

    URLs are normalized (trailing slash stripped, lowercased scheme/host)
    so that callers using slightly different forms of the same URL share
    the same connection.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._relays: Dict[str, Relay] = {}

    def get_or_create(self, url: str) -> Relay:
        normalized = _normalize(url)
        relay = self._relays.get(normalized)
        if relay is None:
            relay = Relay(normalized, parent=self)
            self._relays[normalized] = relay
        return relay

    def publish(
        self,
        urls: List[str],
        event: dict,
        timeout_ms: int = DEFAULT_PUBLISH_TIMEOUT_MS,
    ) -> PublishJob:
        return PublishJob(self, urls, event, timeout_ms=timeout_ms, parent=self)

    def subscribe(
        self,
        urls: List[str],
        filters: List[Dict[str, Any]],
        sub_id: Optional[str] = None,
    ) -> "Subscription":
        return Subscription(self, urls, filters, sub_id=sub_id, parent=self)

    def close_all(self) -> None:
        for relay in self._relays.values():
            relay.close()
        self._relays.clear()


# --------------------------------------------------------------------------- #
# Subscription — REQ/EVENT/EOSE/CLOSED lifecycle                              #
# --------------------------------------------------------------------------- #

class Subscription(QObject):
    """A live subscription across N relays.

    Signals:
      event(dict)        — one inner event from ["EVENT", sub_id, event]
      eose()             — every relay has signalled EOSE (initial backlog done)
      closed(str)        — at least one relay closed the sub with the given reason

    The subscription stays open until ``close()`` is called; new events
    matching the filter continue to fire ``event`` after EOSE.
    """

    event = Signal(dict)
    eose = Signal()
    closed = Signal(str)

    def __init__(
        self,
        pool: "RelayPool",
        urls: List[str],
        filters: List[Dict[str, Any]],
        sub_id: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._pool = pool
        self._urls: List[str] = list(dict.fromkeys(_normalize(u) for u in urls))
        self._filters = list(filters)
        # 8 random bytes -> 16 hex chars; well under the 64-char NIP-01 cap.
        self._sub_id = sub_id or secrets.token_hex(8)
        self._eose_seen: set[str] = set()
        self._eose_emitted = False
        self._closed = False
        self._connections: List[tuple[Relay, str, object]] = []

        for url in self._urls:
            self._start_one(url)

    @property
    def sub_id(self) -> str:
        return self._sub_id

    # -- per-relay wiring ---------------------------------------------------

    def _start_one(self, url: str) -> None:
        relay = self._pool.get_or_create(url)

        msg_slot = lambda payload, u=url: self._on_message(u, payload)
        relay.message.connect(msg_slot)
        self._connections.append((relay, "message", msg_slot))

        if relay.is_connected:
            self._send_req(url)
            return

        conn_slot = lambda u=url: self._send_req(u)
        relay.connected.connect(conn_slot)
        self._connections.append((relay, "connected", conn_slot))
        relay.open()

    def _send_req(self, url: str) -> None:
        if self._closed:
            return
        relay = self._pool.get_or_create(url)
        relay.send(["REQ", self._sub_id] + self._filters)

    # -- relay signal handlers ---------------------------------------------

    def _on_message(self, url: str, msg: list) -> None:
        if self._closed:
            return
        if len(msg) < 2 or msg[1] != self._sub_id:
            return
        verb = msg[0]
        if verb == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
            self.event.emit(msg[2])
        elif verb == "EOSE":
            self._eose_seen.add(url)
            if not self._eose_emitted and self._eose_seen >= set(self._urls):
                self._eose_emitted = True
                self.eose.emit()
        elif verb == "CLOSED":
            reason = str(msg[2]) if len(msg) >= 3 else ""
            self.closed.emit(reason)

    # -- close --------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for url in self._urls:
            relay = self._pool.get_or_create(url)
            if relay.is_connected:
                relay.send(["CLOSE", self._sub_id])
        for relay, sig_name, slot in self._connections:
            try:
                getattr(relay, sig_name).disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()


# --------------------------------------------------------------------------- #
# Utilities                                                                    #
# --------------------------------------------------------------------------- #

def _normalize(url: str) -> str:
    """Strip a single trailing slash and lowercase scheme + host.

    Path-aware relays exist (rare) so we leave the path alone other than
    the trailing slash. This is the same shape the JS ecosystem uses.
    """
    s = url.strip().rstrip("/")
    # split scheme://host[/path]
    if "://" in s:
        scheme, rest = s.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
            return f"{scheme.lower()}://{host.lower()}/{path}"
        return f"{scheme.lower()}://{rest.lower()}"
    return s
