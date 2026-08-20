# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP layer for Blossom: upload, mirror, probe, list, delete.

Built on ``QNetworkAccessManager`` so all I/O stays on the Qt event
loop without manual threading, the same pattern as ``AvatarLoader`` and
``RelayPool``. Every operation is callback-driven: caller hands in
``on_success`` and ``on_failure`` slots, and exactly one of them fires
per request.

No CORS proxy is involved (this is a desktop app, not a SPA), so every
endpoint is hit directly.

Auth events (kind 24242) are built here as *unsigned* dicts. Signing is
the caller's responsibility. The typical wire-up has the caller hand
the unsigned event to ``BunkerClient.sign_event`` and then pass the
signed result back into the matching ``*_with_auth`` method.

Every request carries a signed authorization event, and on PUT it
carries the file itself, so the response is treated as hostile:

- redirects are refused, never followed. Qt's default policy re-sends
  the caller's raw headers to whatever host ``Location`` names, which
  would hand the signed event and the upload body to a stranger.
- the auth event's ``server`` tag must name the host being contacted,
  checked before the request is issued.
- response bodies are capped mid-transfer, not after buffering.
- every URL a server hands back is validated, and rewritten to the
  canonical ``<origin>/<sha256>`` form when it is not acceptable.
- the sha256 a server reports back is checked against the hash of what
  was actually sent, because BUD-02 forbids the server from modifying
  the blob.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QByteArray, QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

import url_safety

from . import hashes
from .auth import to_auth_header
from .errors import ERROR_CODES, sanitize_reason


# Upload timeout is generous because the user could be pushing a 100 MiB
# video over a slow link. Set per-request via Qt's transfer-timeout,
# which resets each time bytes move, so a slow-but-progressing transfer
# isn't killed.
_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000     # five minutes of *idle* time
_LIST_TIMEOUT_MS = 30 * 1000
_DELETE_TIMEOUT_MS = 30 * 1000
_MIRROR_TIMEOUT_MS = 60 * 1000
# A dedup probe runs before an upload the user is waiting on, so it gets
# a short leash: a server that does not answer quickly is treated as not
# having the blob and the upload proceeds.
_HEAD_TIMEOUT_MS = 15 * 1000

_USER_AGENT = b"my-editor-blossom/1"

# Response caps, enforced mid-transfer. A blob descriptor is a few
# hundred bytes; a full blob list for a heavy user is still small JSON.
_MAX_RESPONSE_BYTES = 1024 * 1024        # 1 MiB: upload, mirror, delete
_MAX_LIST_BYTES = 8 * 1024 * 1024        # 8 MiB: /list


# A signed event dict, typed as ``dict`` for documentation only.
SignedEvent = dict


# Status codes the Blossom BUDs give a specific meaning, mapped to the
# app's stable codes. Everything else stays uncoded and falls back to
# the generic copy: guessing at a meaning the spec does not define is
# how a wrong sentence reaches a user.
_STATUS_CODES = {
    401: ERROR_CODES.AUTH_REJECTED,      # bud-02.md:60
    402: ERROR_CODES.PAYMENT_REQUIRED,   # bud-07.md:11
    403: ERROR_CODES.AUTH_REJECTED,      # bud-02.md:62
    409: ERROR_CODES.HASH_MISMATCH,      # bud-02.md:63
    413: ERROR_CODES.SERVER_TOO_LARGE,   # bud-02.md:65
    429: ERROR_CODES.RATE_LIMITED,       # bud-02.md:67
}

# BUD-07 payment methods, by header. Only the presence of a header is
# ever recorded: the payloads are bearer-shaped (a cashu token, a BOLT-11
# invoice) and this app implements no payment, so reading or logging one
# would be pure liability.
_PAYMENT_HEADERS = (
    ("X-Cashu", "cashu"),
    ("X-Lightning", "lightning"),
)

# NIP-94 field names, verbatim from the spec's own list. A ``nip94``
# array is remote input, so anything outside this set is dropped rather
# than carried into the app.
_NIP94_KEYS = frozenset({
    "url", "m", "x", "ox", "size", "dim", "magnet", "i", "blurhash",
    "thumb", "image", "summary", "alt", "fallback", "service",
})

# NIP-94 keys whose value is a URL, and therefore has to clear the media
# policy before it can be stored.
_NIP94_URL_KEYS = frozenset({"url", "thumb", "image", "fallback"})

_MAX_NIP94_PAIRS = 32
_MAX_NIP94_VALUE_CHARS = 512


# Result shapes returned via callbacks ---------------------------------------

class BlossomError(Exception):
    """Raised when a Blossom request fails. ``status`` is the HTTP status
    when known (0 for transport failures), ``body`` is the response body
    (truncated to keep logs sane), ``code`` is a stable
    ``nostr.blossom.errors`` code when one applies, so the UI never has
    to string-match a transport message.

    ``detail`` is the server's sanitized ``X-Reason``, safe to show a
    user. It is diagnostic copy only: BUD-01 and every status table
    since say clients MUST NOT parse it for control flow, so branching
    stays on ``status`` and ``code``.

    ``payment_methods`` names the BUD-07 methods a 402 offered. Names
    only, never the header payloads."""

    def __init__(
        self,
        reason: str,
        *,
        status: int = 0,
        body: str = "",
        code: str = "",
        detail: str = "",
        payment_methods: Sequence[str] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.body = body[:500]
        self.code = code
        self.detail = detail
        self.payment_methods = tuple(payment_methods)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_HEX_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def server_origin(server_url: str) -> str:
    """Return ``scheme://host[:port]`` for ``server_url`` (no path, no
    trailing slash). Used to scope the ``server`` tag on auth events to
    a consistent value across upload / list / delete on the same host.

    Delegates the parse so an IPv6 host keeps its brackets: reassembling
    a bare ``::1`` yields ``http://::1:3000``, which nothing can parse
    back."""
    origin = url_safety.origin_of(server_url)
    if origin is None:
        raise ValueError(f"not a usable server URL: {server_url!r}")
    return origin


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


def _list_url(
    server: str,
    pubkey_hex: str,
    *,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
) -> str:
    """``/list/<pubkey>`` with BUD-12 pagination parameters attached.

    Built through ``QUrlQuery`` so the pubkey stays in the path and the
    parameters are encoded once, by Qt, rather than by string joining.
    """
    url = QUrl(f"{server.rstrip('/')}/list/{pubkey_hex.lower()}")
    query = QUrlQuery()
    if cursor:
        query.addQueryItem("cursor", cursor.lower())
    if limit:
        query.addQueryItem("limit", str(int(limit)))
    if not query.isEmpty():
        url.setQuery(query)
    return url.toString()


def safe_blob_url(candidate, server: str, sha256: str) -> str:
    """Return a blob URL that is safe to cache, store and publish.

    The server-supplied ``candidate`` is kept only when it is a media
    URL on the server's own origin AND its path names this blob. Anything
    else, a ``file://`` path, a ``data:`` URI, another host, or the
    server's own address for a DIFFERENT blob, is replaced by the
    canonical ``<origin>/<sha256>`` form rather than dropped: BUD-01
    guarantees ``GET /<sha256>`` on the same origin serves the blob, so
    the descriptor keeps working while the boundary closes.

    The hash half of that check is the BUD-03 rule (``hashes``): query
    and fragment are ignored, so a signed or tokenised URL still passes,
    and a URL with no hex run at all is accepted because plenty of
    servers address blobs by an opaque path.
    """
    origin = url_safety.origin_of(server) or str(server).rstrip("/")
    canonical = hashes.blob_url(origin, sha256)
    if not candidate:
        return canonical
    text = str(candidate)
    if not url_safety.is_safe_media_url(text, allowed_origin=server):
        return canonical
    if not hashes.url_agrees_with_hash(text, sha256):
        return canonical
    return text


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

def parse_nip94(raw, sha256: str) -> List[List[str]]:
    """Validated BUD-08 ``nip94`` pairs, or ``[]``.

    BUD-08 lets a server return "a JSON array with KV pairs as defined
    in NIP-94" alongside a blob descriptor. It is remote input on the
    happy path, so every pair has to earn its place: a known NIP-94 key,
    two strings, a clipped value, a URL that clears the media policy,
    and an ``x`` that agrees with the sha256 already verified. A server
    does not get to rename this app's blob.

    A malformed ``nip94`` is ignored entirely. It must never be the
    reason an otherwise good upload fails.

    Scope boundary: Task 2 captures these and stops. They are NOT fed
    into the NIP-92 ``imeta`` tags the publisher emits, because a
    server-supplied ``dim`` is a measurement this app never made and
    AD-14 forbids publishing one.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    pairs: List[List[str]] = []
    for item in raw:
        if len(pairs) >= _MAX_NIP94_PAIRS:
            break
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
            continue
        if len(item) < 2:
            continue
        key, value = item[0], item[1]
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key = key.strip().lower()
        if key not in _NIP94_KEYS:
            continue
        value = value.strip()[:_MAX_NIP94_VALUE_CHARS]
        if not value:
            continue
        if key in _NIP94_URL_KEYS and not url_safety.is_safe_media_url(value):
            continue
        if key == "x" and value.lower() != sha256:
            continue
        pairs.append([key, value])
    return pairs


class UploadResult(dict):
    """Server response from ``PUT /upload`` (or ``/mirror``).

    Kept as a ``dict`` subclass so callers can treat it like the parsed
    JSON it came from, with named accessors for the fields the rest of
    the app cares about. The Blossom spec calls this a Blob Descriptor.

    Required fields:
        hash, url, size, mime_type, server, existed, nip94
    """

    @classmethod
    def from_json(
        cls,
        data: dict,
        server: str,
        *,
        expected_sha: Optional[str] = None,
    ) -> "UploadResult":
        """Parse and verify one blob descriptor.

        ``expected_sha`` is the hash of the bytes that were actually
        sent (or, for a mirror, the hash the ``x`` tag authorised). A
        descriptor naming a different hash is refused rather than
        stored: BUD-02 says the server "MUST NOT modify the blob in any
        way and MUST compute the sha256 hash over the exact bytes
        received", so a disagreement means the blob was altered or the
        response is about somebody else's file.

        ``PUT /media`` (BUD-05) is the one endpoint where the hash
        legitimately changes. This app does not use it, and this check
        must not be copied there if it is ever added.
        """
        sha = (data.get("sha256") or "").lower()
        if not looks_like_sha256(sha):
            raise BlossomError(
                f"server response missing or malformed sha256: {data!r}"
            )
        if expected_sha and sha != expected_sha.lower():
            raise BlossomError(
                "server described a different blob than the one sent",
                code=ERROR_CODES.HASH_MISMATCH,
            )
        result = cls(
            hash=sha,
            url=safe_blob_url(data.get("url"), server, sha),
            size=int(data.get("size") or 0),
            mime_type=str(data.get("type") or "application/octet-stream"),
            server=server,
            existed=False,
            nip94=parse_nip94(data.get("nip94"), sha),
        )
        return result


# ---------------------------------------------------------------------------
# Response guards
# ---------------------------------------------------------------------------

def _auth_server_hosts(auth_event: SignedEvent) -> List[str]:
    """Every hostname the auth event's ``server`` tags name.

    BUD-11: "Multiple ``server`` tags may be present to allow the token
    to be used on multiple servers", and a server validates by checking
    "its domain name appears in at least one ``server`` tag". Reading
    only the first tag would refuse a perfectly valid multi-server
    token, so all of them are collected.

    Tolerates both spellings: the bare lowercase domain BUD-11 mandates,
    and the full origin older tokens carry.
    """
    hosts: List[str] = []
    for tag in (auth_event or {}).get("tags") or []:
        if not isinstance(tag, (list, tuple)) or len(tag) < 2:
            continue
        if tag[0] != "server":
            continue
        value = str(tag[1]).strip()
        host = url_safety.host_of(value) if "://" in value else value.rstrip("/").lower()
        if host:
            hosts.append(host)
    return hosts


def _auth_hashes(auth_event: SignedEvent) -> List[str]:
    """Lowercase hashes the auth event's ``x`` tags scope it to."""
    hashes: List[str] = []
    for tag in (auth_event or {}).get("tags") or []:
        if not isinstance(tag, (list, tuple)) or len(tag) < 2:
            continue
        if tag[0] != "x":
            continue
        value = str(tag[1]).strip().lower()
        if value:
            hashes.append(value)
    return hashes


def _require_matching_host(url: str, auth_event: SignedEvent) -> None:
    """Refuse to send an auth event to a host it does not name.

    The signed event is a bearer credential. A request that carries one
    to a different origin than the user authorised is the exact leak the
    redirect policy also guards against, arriving by another route.

    A token with no ``server`` tag is unscoped and valid everywhere, so
    there is nothing to check.
    """
    hosts = _auth_server_hosts(auth_event)
    if not hosts:
        return
    if (url_safety.host_of(url) or "") not in hosts:
        raise BlossomError(
            "auth event does not match the request host",
            code=ERROR_CODES.HOST_MISMATCH,
        )


def _require_authorized_hash(body_sha: str, auth_event: SignedEvent) -> None:
    """Refuse to send bytes a scoped token does not cover.

    BUD-11 rule 6: a server "MUST verify that at least one ``x`` tag
    matches the blob hash implied by the endpoint", and for ``PUT
    /upload`` that hash is the ``X-SHA-256`` header this request is
    about to send. Checking locally turns a mis-wired caller into an
    error before anything leaves the process, instead of a rejected
    upload plus a spent signer prompt.
    """
    hashes = _auth_hashes(auth_event)
    if not hashes:
        return
    if body_sha.lower() not in hashes:
        raise BlossomError(
            "auth event does not authorize this blob hash",
            code=ERROR_CODES.HASH_MISMATCH,
        )


def _read_reason(reply) -> str:
    """The server's ``X-Reason``, sanitized, or ``""``.

    Read on every failure path and carried as diagnostic copy only.
    Control flow never touches it.
    """
    getter = getattr(reply, "rawHeader", None)
    if getter is None:
        return ""
    try:
        return sanitize_reason(bytes(getter("X-Reason")))
    except (TypeError, ValueError):
        return ""


def _payment_methods(reply) -> Tuple[str, ...]:
    """BUD-07 payment method names a 402 offered. Names only."""
    has = getattr(reply, "hasRawHeader", None)
    if has is None:
        return ()
    methods: List[str] = []
    for header, name in _PAYMENT_HEADERS:
        try:
            present = bool(has(header))
        except (TypeError, ValueError):
            present = False
        if present:
            methods.append(name)
    return tuple(methods)


def _http_error(reply, status: int, body_text: str) -> BlossomError:
    """The failure for a response the server answered but refused."""
    return BlossomError(
        reply.errorString() or f"HTTP {status}",
        status=status,
        body=body_text,
        code=_STATUS_CODES.get(status)
        or (ERROR_CODES.NETWORK_UNAVAILABLE if status == 0 else ""),
        detail=_read_reason(reply),
        payment_methods=_payment_methods(reply) if status == 402 else (),
    )


def _sanitize_list_urls(payload: List[dict], server: str) -> List[dict]:
    """Rewrite unacceptable blob URLs in a ``/list`` response.

    Runs at parse time so no consumer downstream, the library, the
    cache, the document, ever sees a ``file://`` or cross-origin URL.
    Entries without a usable sha256 pass through untouched: the store
    already skips them, and rewriting one would invent a URL.
    """
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        sha = str(entry.get("sha256") or "").lower()
        if not looks_like_sha256(sha):
            continue
        entry["url"] = safe_blob_url(entry.get("url"), server, sha)
    return payload


def _redirect_error(reply, status: int) -> Optional[BlossomError]:
    """A refused 3xx, or None when the response is not a redirect.

    Both tests are needed: with ``ManualRedirectPolicy`` Qt leaves
    ``error()`` at ``NoError`` on a 3xx, and some replies carry the
    redirection target attribute without a status this layer can see.
    """
    target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
    if target is None and not (300 <= status < 400):
        return None
    return BlossomError(
        "server redirected the request; it was not resent",
        status=status,
        code=ERROR_CODES.REDIRECT_REFUSED,
    )


def _oversize_error(status: int) -> BlossomError:
    return BlossomError(
        "server response exceeded the size limit",
        status=status,
        code=ERROR_CODES.TOO_LARGE,
    )


def _guard_response_size(reply, max_bytes: int) -> dict:
    """Abort ``reply`` the moment the body passes ``max_bytes``.

    Returns the flag dict the finished handler reads, so an abort is
    reported as a size rejection rather than a generic network failure.
    The announced total is honoured too: a server that declares a huge
    Content-Length is stopped before it sends the first chunk.
    """
    oversize = {"hit": False}

    def _on_progress(received: int, total: int, r=reply) -> None:
        if oversize["hit"]:
            return
        if received > max_bytes or (total > 0 and total > max_bytes):
            oversize["hit"] = True
            r.abort()

    reply.downloadProgress.connect(_on_progress)
    return oversize


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
        max_bytes: int = _MAX_RESPONSE_BYTES,
        expected_sha: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._reply = reply
        self._server = server
        self._on_success = on_success
        self._on_failure = on_failure
        self._on_progress = on_progress
        self._expected_sha = expected_sha
        self._oversize = _guard_response_size(reply, max_bytes)

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
            redirect = _redirect_error(reply, status)
            if redirect is not None:
                self._on_failure(redirect)
                return
            if self._oversize["hit"]:
                self._on_failure(_oversize_error(status))
                return
            raw_body = bytes(reply.readAll())
            body_text = raw_body.decode("utf-8", errors="replace")
            if err != QNetworkReply.NoError or not (200 <= status < 300):
                self._on_failure(_http_error(reply, status, body_text))
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
                result = UploadResult.from_json(
                    payload, self._server, expected_sha=self._expected_sha
                )
            except BlossomError as exc:
                self._on_failure(exc)
                return
            # BUD-02: 201 means the blob was newly stored, 200 means the
            # server already had it. Both are success; only 201 is new.
            result["existed"] = status == 200
            self._on_success(result)
        finally:
            reply.deleteLater()


class BlossomClient(QObject):
    """HTTP-level Blossom client. Stateless apart from the shared QNAM.

    All methods take the *signed* auth event (a dict with ``id``,
    ``pubkey``, ``sig`` etc.) and the target server origin. The signing
    handshake with the bunker happens one level up in ``MediaStore``.
    """

    def __init__(self, parent: Optional[QObject] = None, *, nam=None) -> None:
        super().__init__(parent)
        # ``nam`` is a seam for tests: a fake transport keeps the request
        # attributes assertable without a network.
        self._nam = nam or QNetworkAccessManager(self)
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
        *,
        sha256: Optional[str] = None,
    ) -> None:
        """Upload ``body`` (raw bytes) to ``server``'s ``/upload``.

        Blossom convention is ``PUT /upload`` with ``Authorization: Nostr
        <base64url>``. The server computes its own sha256 and rejects if
        it doesn't match the ``x`` tag signed into the auth event.

        ``X-SHA-256`` is sent unconditionally. BUD-02 words it as a MAY
        for the client, but BUD-11's endpoint table makes it the blob
        hash implied by ``PUT /upload``, so a server that enforces
        authorization cannot validate the token's ``x`` tag without it.

        ``sha256`` is computed from ``body`` when omitted. Deriving it
        here rather than trusting a caller means the header can never
        disagree with the bytes that were actually sent.
        """
        sha = (sha256 or hashlib.sha256(body).hexdigest()).lower()
        try:
            request = self._prepare(
                f"{server.rstrip('/')}/upload",
                auth_event=auth_event,
                content_type=mime_type or "application/octet-stream",
                timeout_ms=_UPLOAD_TIMEOUT_MS,
                x_sha256=sha,
            )
        except BlossomError as exc:
            on_failure(exc)
            return
        reply = self._nam.put(request, QByteArray(body))
        # Track the wrapper by reply id so we don't leak.
        wrapper = _InflightUpload(
            reply,
            server,
            on_success=lambda r, key=id(reply): self._finish(key, lambda: on_success(r)),
            on_failure=lambda e, key=id(reply): self._finish(key, lambda: on_failure(e)),
            on_progress=on_progress,
            parent=self,
            expected_sha=sha,
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
        *,
        sha256: Optional[str] = None,
    ) -> None:
        """Ask ``server`` to fetch a blob from ``source_url`` and host it
        too. BUD-04 defines the endpoint and its JSON body ``{"url":
        source_url}``.

        BUD-11 owns the authorization: ``PUT /mirror`` takes ``t=upload``
        and REQUIRES an ``x`` tag whose value is the sha256 of the
        mirrored blob. ``sha256`` is that same hash, and the descriptor
        the server returns is checked against it.

        No ``X-SHA-256`` header here: the request body is the JSON, not
        the blob, so a hash of the body would describe the wrong thing.
        """
        try:
            request = self._prepare(
                f"{server.rstrip('/')}/mirror",
                auth_event=auth_event,
                content_type="application/json",
                timeout_ms=_MIRROR_TIMEOUT_MS,
            )
        except BlossomError as exc:
            on_failure(exc)
            return
        body = json.dumps({"url": source_url}, separators=(",", ":")).encode("utf-8")
        reply = self._nam.put(request, QByteArray(body))
        wrapper = _InflightUpload(
            reply,
            server,
            on_success=lambda r, key=id(reply): self._finish(key, lambda: on_success(r)),
            on_failure=lambda e, key=id(reply): self._finish(key, lambda: on_failure(e)),
            on_progress=None,
            parent=self,
            expected_sha=(sha256 or "").lower() or None,
        )
        self._inflight[id(reply)] = wrapper

    # -- has-blob probe ----------------------------------------------------

    def head_blob(
        self,
        server: str,
        sha256: str,
        on_present: Callable[[], None],
        on_absent: Callable[[], None],
    ) -> None:
        """Ask ``server`` whether it already holds ``sha256``.

        BUD-06: "Clients that want to check whether a blob already exists
        on the server SHOULD use ``HEAD /<sha256>`` from BUD-01", where
        BUD-01 gives ``200 OK`` as "the blob exists and the server returns
        the same metadata headers as ``GET /<sha256>`` without a response
        body".

        No authorization event is built or sent. BUD-11 makes auth
        optional on this endpoint, and an unauthenticated probe cannot
        leak a credential to a redirect target, so the feature cannot
        weaken the boundary it is asking about.

        Only ``200`` answers present. A 3xx says the blob lives at
        another URL, and following it would defeat the redirect policy,
        so it is reported absent; the cost of being wrong is one upload
        the server answers with ``200`` and an existing descriptor. A
        404, a transport failure, a timeout and every other status also
        answer absent: BUD-06 warns the probe "is not a guarantee of the
        eventual ``PUT /upload`` outcome", so an inconclusive result must
        degrade to uploading, never to skipping.
        """
        sha = (sha256 or "").lower()
        if not looks_like_sha256(sha):
            on_absent()
            return
        request = self._prepare(
            f"{server.rstrip('/')}/{sha}",
            auth_event=None,
            timeout_ms=_HEAD_TIMEOUT_MS,
        )
        reply = self._nam.head(request)
        key = id(reply)
        oversize = _guard_response_size(reply, _MAX_RESPONSE_BYTES)

        def _finished() -> None:
            try:
                status = int(
                    reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                )
                present = (
                    reply.error() == QNetworkReply.NoError
                    and status == 200
                    and not oversize["hit"]
                )
                self._finish(key, on_present if present else on_absent)
            finally:
                reply.deleteLater()

        reply.finished.connect(_finished)
        self._inflight[key] = reply

    # -- list --------------------------------------------------------------

    def list_for_pubkey(
        self,
        server: str,
        pubkey_hex: str,
        auth_event: Optional[SignedEvent],
        on_success: Callable[[List[dict]], None],
        on_failure: Callable[[BlossomError], None],
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        """``GET /list/<pubkey>`` returns the user's blob descriptors.

        The endpoint itself is BUD-12, which also defines the ``cursor``
        and ``limit`` query parameters: the cursor "MUST be the sha256
        hash of the last blob in the previous page", results come back
        newest first, and the blob at the cursor is excluded. BUD-11's
        endpoint table is what makes an ``x`` tag not applicable here
        and authorization optional.

        ``auth_event`` is optional: some servers serve the list
        publicly. It is always sent when available; the store retries
        without auth on 401/403 to match STANDUP's fallback behaviour.
        """
        if cursor is not None and not looks_like_sha256(cursor):
            on_failure(
                BlossomError(
                    "list cursor is not a sha256",
                    code=ERROR_CODES.HASH_MISMATCH,
                )
            )
            return
        url = _list_url(server, pubkey_hex, cursor=cursor, limit=limit)
        try:
            request = self._prepare(
                url,
                auth_event=auth_event,
                accept=b"application/json",
                timeout_ms=_LIST_TIMEOUT_MS,
            )
        except BlossomError as exc:
            on_failure(exc)
            return

        reply = self._nam.get(request)
        key = id(reply)
        oversize = _guard_response_size(reply, _MAX_LIST_BYTES)

        def _finished() -> None:
            try:
                err = reply.error()
                status = int(
                    reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                )
                redirect = _redirect_error(reply, status)
                if redirect is not None:
                    self._finish(key, lambda: on_failure(redirect))
                    return
                if oversize["hit"]:
                    self._finish(key, lambda: on_failure(_oversize_error(status)))
                    return
                raw_body = bytes(reply.readAll())
                body_text = raw_body.decode("utf-8", errors="replace")
                if err != QNetworkReply.NoError or not (200 <= status < 300):
                    failure = _http_error(reply, status, body_text)
                    self._finish(key, lambda: on_failure(failure))
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
                sanitized = _sanitize_list_urls(payload, server)
                self._finish(key, lambda: on_success(sanitized))
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
        try:
            request = self._prepare(
                url,
                auth_event=auth_event,
                timeout_ms=_DELETE_TIMEOUT_MS,
            )
        except BlossomError as exc:
            on_failure(exc)
            return

        reply = self._nam.deleteResource(request)
        key = id(reply)
        oversize = _guard_response_size(reply, _MAX_RESPONSE_BYTES)

        def _finished() -> None:
            try:
                err = reply.error()
                status = int(
                    reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                )
                redirect = _redirect_error(reply, status)
                if redirect is not None:
                    self._finish(key, lambda: on_failure(redirect))
                    return
                if oversize["hit"]:
                    self._finish(key, lambda: on_failure(_oversize_error(status)))
                    return
                raw_body = bytes(reply.readAll())
                if err == QNetworkReply.NoError and 200 <= status < 300:
                    self._finish(key, on_success)
                    return
                body_text = raw_body.decode("utf-8", errors="replace")
                failure = _http_error(reply, status, body_text)
                self._finish(key, lambda: on_failure(failure))
            finally:
                reply.deleteLater()

        reply.finished.connect(_finished)
        self._inflight[key] = reply

    # -- internals ---------------------------------------------------------

    def _prepare(
        self,
        url: str,
        *,
        auth_event: Optional[SignedEvent] = None,
        content_type: Optional[str] = None,
        accept: Optional[bytes] = None,
        timeout_ms: int,
        x_sha256: Optional[str] = None,
    ) -> QNetworkRequest:
        """Build the request for every Blossom verb.

        One builder on purpose: a second one is a place for the redirect
        policy to be forgotten. Raises :class:`BlossomError` when the
        auth event does not match the host or does not authorize the
        blob hash, before anything is sent.
        """
        if auth_event is not None:
            _require_matching_host(url, auth_event)
            if x_sha256:
                _require_authorized_hash(x_sha256, auth_event)

        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setTransferTimeout(timeout_ms)
        # Qt's default follows up to 50 hops and re-sends the raw headers,
        # which would copy the signed auth event and, on PUT, the whole
        # body to whatever host ``Location`` names. Manual policy stops
        # at the 3xx and lets the finished handler refuse it.
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setMaximumRedirectsAllowed(0)
        if content_type:
            request.setHeader(QNetworkRequest.ContentTypeHeader, content_type)
        if accept:
            request.setRawHeader(b"Accept", accept)
        if x_sha256:
            request.setRawHeader(b"X-SHA-256", x_sha256.lower().encode("ascii"))
        if auth_event is not None:
            request.setRawHeader(
                b"Authorization", to_auth_header(auth_event).encode("ascii")
            )
        return request

    def _finish(self, key: int, callback: Callable[[], None]) -> None:
        """Pop the inflight wrapper before invoking the user callback.

        Order matters: the callback may re-enter (e.g. uploading the
        next file in a queue), so we must release the slot first."""
        self._inflight.pop(key, None)
        try:
            callback()
        except Exception:  # noqa: BLE001, never let one callback break another
            # Surface unexpected callback errors via stderr but keep the
            # event loop healthy. We don't have a logger plumbed in here
            # yet; the rest of the codebase prints to stderr similarly.
            import traceback
            traceback.print_exc()
