"""HTTP layer for Blossom: upload, list, delete, mirror.

Built on ``QNetworkAccessManager`` so all I/O stays on the Qt event
loop without manual threading — same pattern as ``AvatarLoader`` and
``RelayPool``. Every operation is callback-driven: caller hands in
``on_success`` and ``on_failure`` slots, and exactly one of them fires
per request.

No CORS proxy is involved (this is a desktop app, not a SPA), so every
endpoint is hit directly.

Auth events (kind 24242) are built here as *unsigned* dicts. Signing is
the caller's responsibility — the typical wire-up has the caller hand
the unsigned event to ``BunkerClient.sign_event`` and then pass the
signed result back into the matching ``*_with_auth`` method.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from .auth import to_auth_header


# Upload timeout is generous because the user could be pushing a 100 MiB
# video over a slow link. Set per-request via Qt's transfer-timeout,
# which resets each time bytes move — so a slow-but-progressing transfer
# isn't killed.
_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000     # five minutes of *idle* time
_LIST_TIMEOUT_MS = 30 * 1000
_DELETE_TIMEOUT_MS = 30 * 1000
_MIRROR_TIMEOUT_MS = 60 * 1000

_USER_AGENT = b"my-editor-blossom/1"


# A signed event dict — typed as ``dict`` for documentation only.
SignedEvent = dict


# Result shapes returned via callbacks ---------------------------------------

class BlossomError(Exception):
    """Raised when a Blossom request fails. ``status`` is the HTTP status
    when known (0 for transport failures), ``body`` is the response body
    (truncated to keep logs sane)."""

    def __init__(self, reason: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.body = body[:500]


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_HEX_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def server_origin(server_url: str) -> str:
    """Return ``scheme://host[:port]`` for ``server_url`` (no path, no
    trailing slash). Used to scope the ``server`` tag on auth events to
    a consistent value across upload / list / delete on the same host."""
    parsed = urlparse(server_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"not a usable server URL: {server_url!r}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def extract_server_from_blob_url(blob_url: str) -> Optional[str]:
    """Best-effort: given a blob URL like ``https://blossom.band/<hash>``,
    return ``https://blossom.band``. Returns None if the URL is malformed.
    Used so delete requests target the same server the blob actually
    lives on, not the configured primary."""
    try:
        return server_origin(blob_url)
    except ValueError:
        return None


def looks_like_sha256(value: str) -> bool:
    return isinstance(value, str) and bool(_HEX_SHA256_RE.fullmatch(value.lower()))


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

class UploadResult(dict):
    """Server response from ``PUT /upload`` (or ``/mirror``).

    Kept as a ``dict`` subclass so callers can treat it like the parsed
    JSON it came from, with named accessors for the fields the rest of
    the app cares about. The Blossom spec calls this a Blob Descriptor.

    Required fields:
        hash, url, size, mime_type, server
    """

    @classmethod
    def from_json(cls, data: dict, server: str) -> "UploadResult":
        sha = (data.get("sha256") or "").lower()
        if not looks_like_sha256(sha):
            raise BlossomError(
                f"server response missing or malformed sha256: {data!r}"
            )
        url = data.get("url") or f"{server.rstrip('/')}/{sha}"
        result = cls(
            hash=sha,
            url=str(url),
            size=int(data.get("size") or 0),
            mime_type=str(data.get("type") or "application/octet-stream"),
            server=server,
        )
        return result


# ---------------------------------------------------------------------------
# BlossomClient
# ---------------------------------------------------------------------------

class _InflightUpload(QObject):
    """One in-flight ``PUT /upload``. Wraps the reply so we can route
    progress + finished into ``BlossomClient`` callbacks without lambda
    spaghetti."""

    progress = Signal(int, int)   # bytes_sent, bytes_total

    def __init__(
        self,
        reply: QNetworkReply,
        server: str,
        on_success: Callable[[UploadResult], None],
        on_failure: Callable[[BlossomError], None],
        on_progress: Optional[Callable[[int, int], None]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._reply = reply
        self._server = server
        self._on_success = on_success
        self._on_failure = on_failure
        self._on_progress = on_progress

        reply.uploadProgress.connect(self._emit_progress)
        reply.finished.connect(self._on_finished)

    def _emit_progress(self, sent: int, total: int) -> None:
        if self._on_progress is not None:
            self._on_progress(int(sent), int(total))

    def _on_finished(self) -> None:
        reply = self._reply
        try:
            err = reply.error()
            status = int(
                reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
            )
            raw_body = bytes(reply.readAll())
            body_text = raw_body.decode("utf-8", errors="replace")
            if err != QNetworkReply.NoError or not (200 <= status < 300):
                self._on_failure(
                    BlossomError(
                        reply.errorString() or f"HTTP {status}",
                        status=status,
                        body=body_text,
                    )
                )
                return
            try:
                payload = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                self._on_failure(
                    BlossomError(
                        "server returned non-JSON upload response",
                        status=status,
                        body=body_text,
                    )
                )
                return
            if not isinstance(payload, dict):
                self._on_failure(
                    BlossomError(
                        "server returned non-object upload response",
                        status=status,
                        body=body_text,
                    )
                )
                return
            try:
                result = UploadResult.from_json(payload, self._server)
            except BlossomError as exc:
                self._on_failure(exc)
                return
            self._on_success(result)
        finally:
            reply.deleteLater()


class BlossomClient(QObject):
    """HTTP-level Blossom client. Stateless apart from the shared QNAM.

    All methods take the *signed* auth event (a dict with ``id``,
    ``pubkey``, ``sig`` etc.) and the target server origin. The signing
    handshake with the bunker happens one level up in ``MediaStore``.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        # Strong refs to in-flight wrappers so they live until ``finished``.
        self._inflight: Dict[int, QObject] = {}

    # -- upload ------------------------------------------------------------

    def upload(
        self,
        server: str,
        body: bytes,
        mime_type: str,
        auth_event: SignedEvent,
        on_success: Callable[[UploadResult], None],
        on_failure: Callable[[BlossomError], None],
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Upload ``body`` (raw bytes) to ``server``'s ``/upload``.

        Blossom convention is ``PUT /upload`` with ``Authorization: Nostr
        <base64>``. The server computes its own sha256 and rejects if it
        doesn't match the ``x`` tag we signed into the auth event.
        """
        request = self._build_request(
            f"{server.rstrip('/')}/upload",
            auth_event,
            content_type=mime_type or "application/octet-stream",
            timeout_ms=_UPLOAD_TIMEOUT_MS,
        )
        reply = self._nam.put(request, QByteArray(body))
        # Track the wrapper by reply id so we don't leak.
        wrapper = _InflightUpload(
            reply,
            server,
            on_success=lambda r, key=id(reply): self._finish(key, lambda: on_success(r)),
            on_failure=lambda e, key=id(reply): self._finish(key, lambda: on_failure(e)),
            on_progress=on_progress,
            parent=self,
        )
        self._inflight[id(reply)] = wrapper

    # -- mirror ------------------------------------------------------------

    def mirror(
        self,
        server: str,
        source_url: str,
        auth_event: SignedEvent,
        on_success: Callable[[UploadResult], None],
        on_failure: Callable[[BlossomError], None],
    ) -> None:
        """Ask ``server`` to fetch a blob from ``source_url`` and host it
        too. BUD-04: ``PUT /mirror`` with JSON ``{"url": source_url}``.

        The auth event for /mirror uses ``t=upload`` per BUD-04 — the
        server treats /mirror as an upload-by-URL.
        """
        request = self._build_request(
            f"{server.rstrip('/')}/mirror",
            auth_event,
            content_type="application/json",
            timeout_ms=_MIRROR_TIMEOUT_MS,
        )
        body = json.dumps({"url": source_url}, separators=(",", ":")).encode("utf-8")
        reply = self._nam.put(request, QByteArray(body))
        wrapper = _InflightUpload(
            reply,
            server,
            on_success=lambda r, key=id(reply): self._finish(key, lambda: on_success(r)),
            on_failure=lambda e, key=id(reply): self._finish(key, lambda: on_failure(e)),
            on_progress=None,
            parent=self,
        )
        self._inflight[id(reply)] = wrapper

    # -- list --------------------------------------------------------------

    def list_for_pubkey(
        self,
        server: str,
        pubkey_hex: str,
        auth_event: Optional[SignedEvent],
        on_success: Callable[[List[dict]], None],
        on_failure: Callable[[BlossomError], None],
    ) -> None:
        """``GET /list/<pubkey>`` — returns the user's blob descriptors.

        ``auth_event`` is optional: some servers serve the list publicly
        (BUD-02 says auth MAY be required). We always send it when we
        have it; the store retries without auth on 401/403 to match
        STANDUP's fallback behaviour.
        """
        url = f"{server.rstrip('/')}/list/{pubkey_hex.lower()}"
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setRawHeader(b"Accept", b"application/json")
        request.setTransferTimeout(_LIST_TIMEOUT_MS)
        if auth_event is not None:
            request.setRawHeader(b"Authorization", to_auth_header(auth_event).encode("ascii"))

        reply = self._nam.get(request)
        key = id(reply)

        def _finished() -> None:
            try:
                err = reply.error()
                status = int(
                    reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                )
                raw_body = bytes(reply.readAll())
                body_text = raw_body.decode("utf-8", errors="replace")
                if err != QNetworkReply.NoError or not (200 <= status < 300):
                    self._finish(
                        key,
                        lambda: on_failure(
                            BlossomError(
                                reply.errorString() or f"HTTP {status}",
                                status=status,
                                body=body_text,
                            )
                        ),
                    )
                    return
                try:
                    payload = json.loads(body_text) if body_text else []
                except json.JSONDecodeError:
                    self._finish(
                        key,
                        lambda: on_failure(
                            BlossomError(
                                "list returned non-JSON",
                                status=status,
                                body=body_text,
                            )
                        ),
                    )
                    return
                if not isinstance(payload, list):
                    self._finish(
                        key,
                        lambda: on_failure(
                            BlossomError(
                                "list returned non-array payload",
                                status=status,
                                body=body_text,
                            )
                        ),
                    )
                    return
                self._finish(key, lambda: on_success(payload))
            finally:
                reply.deleteLater()

        reply.finished.connect(_finished)
        # Keep a strong ref via the inflight map.
        self._inflight[key] = reply

    # -- delete ------------------------------------------------------------

    def delete(
        self,
        server: str,
        file_hash: str,
        auth_event: SignedEvent,
        on_success: Callable[[], None],
        on_failure: Callable[[BlossomError], None],
    ) -> None:
        """``DELETE /<sha256>`` with auth."""
        url = f"{server.rstrip('/')}/{file_hash.lower()}"
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setRawHeader(
            b"Authorization", to_auth_header(auth_event).encode("ascii")
        )
        request.setTransferTimeout(_DELETE_TIMEOUT_MS)

        reply = self._nam.deleteResource(request)
        key = id(reply)

        def _finished() -> None:
            try:
                err = reply.error()
                status = int(
                    reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                )
                raw_body = bytes(reply.readAll())
                if err == QNetworkReply.NoError and 200 <= status < 300:
                    self._finish(key, on_success)
                    return
                body_text = raw_body.decode("utf-8", errors="replace")
                self._finish(
                    key,
                    lambda: on_failure(
                        BlossomError(
                            reply.errorString() or f"HTTP {status}",
                            status=status,
                            body=body_text,
                        )
                    ),
                )
            finally:
                reply.deleteLater()

        reply.finished.connect(_finished)
        self._inflight[key] = reply

    # -- internals ---------------------------------------------------------

    def _build_request(
        self,
        url: str,
        auth_event: SignedEvent,
        *,
        content_type: str,
        timeout_ms: int,
    ) -> QNetworkRequest:
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setRawHeader(
            b"Authorization", to_auth_header(auth_event).encode("ascii")
        )
        request.setHeader(QNetworkRequest.ContentTypeHeader, content_type)
        request.setTransferTimeout(timeout_ms)
        return request

    def _finish(self, key: int, callback: Callable[[], None]) -> None:
        """Pop the inflight wrapper before invoking the user callback.

        Order matters: the callback may re-enter (e.g. uploading the
        next file in a queue), so we must release the slot first."""
        self._inflight.pop(key, None)
        try:
            callback()
        except Exception:  # noqa: BLE001 — never let one callback break another
            # Surface unexpected callback errors via stderr but keep the
            # event loop healthy. We don't have a logger plumbed in here
            # yet; the rest of the codebase prints to stderr similarly.
            import traceback
            traceback.print_exc()
