# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fakes for the Blossom and loader test-suites.

A fake QNetworkAccessManager plus a fake reply, both settling only when
a test says so, so request attributes stay assertable and no test opens
a socket. Modelled on ``tests/imports_fakes.py``.

On top of that, :class:`FakeBlossomServer` answers requests the way the
BUDs say a server should: it decodes the Authorization header as
base64url, applies BUD-11's validation rules, requires ``X-SHA-256`` on
``PUT /upload``, and paginates ``/list`` with BUD-12 cursors. A protocol
change that only satisfies our own reader is not a protocol change, so
the tests get a counterparty that checks.

Signing is faked too (:class:`FakeSigner`, :class:`FakeSessionPool`):
no relay, no bunker, no key material, per AD-8.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Dict, List, Optional

from PySide6.QtCore import QByteArray, QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest


PUBKEY = "ab" * 32

SERVER = "https://good.example"
MIRROR = "https://mirror.example"

# The body most tests upload, and its real hash. They have to agree:
# the client verifies the descriptor's sha256 against the bytes it sent,
# so a made-up constant would look like a hostile server.
BODY = b"abc"
SHA = hashlib.sha256(BODY).hexdigest()

OTHER_SHA = "d" * 64


def auth_event(server: str = SERVER, action: str = "upload") -> dict:
    """A signed-looking kind 24242 event scoped to ``server``.

    Deliberately keeps the FULL-ORIGIN ``server`` tag even though BUD-11
    mandates a bare domain: this is the legacy-token fixture that keeps
    the client's tolerant reader covered. Do not "fix" it. The value the
    app now emits is pinned in ``tests/test_blossom_server_tags.py``.
    """
    return {
        "id": "ff" * 32,
        "pubkey": PUBKEY,
        "kind": 24242,
        "created_at": 1,
        "content": f"Authorize {action}",
        "tags": [["t", action], ["expiration", "9999999999"],
                 ["server", server]],
        "sig": "aa" * 64,
    }


SIGNED_AUTH_EVENT = auth_event()


def decode_auth_header(value) -> dict:
    """Decode an ``Authorization: Nostr <base64url>`` header value.

    Strict about the alphabet on purpose: this is the assertion that
    BUD-11's "Base64 URL-safe without padding" is what actually went out
    on the wire, not merely what a lenient decoder would accept.
    """
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("ascii")
    if not value.startswith("Nostr "):
        raise ValueError("authorization scheme is not Nostr")
    payload = value[len("Nostr "):]
    for banned in ("=", "+", "/"):
        if banned in payload:
            raise ValueError(f"token is not base64url without padding: {banned!r}")
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def _header_name(name) -> str:
    """Normalize a header name. Qt's Python bindings hand these around
    as ``str`` and match case-insensitively, so the fakes do too."""
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("ascii")
    return str(name).lower()


def tag_values(event: dict, name: str) -> List[str]:
    """Every value of ``name`` in ``event``'s tags, in order."""
    return [
        str(tag[1])
        for tag in (event or {}).get("tags") or []
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == name
    ]


def first_tag(event: dict, name: str) -> Optional[str]:
    values = tag_values(event, name)
    return values[0] if values else None


class FakeReply(QObject):
    """Reply stand-in. Nothing happens until a test calls ``finish``."""

    finished = Signal()
    uploadProgress = Signal(int, int)
    downloadProgress = Signal(int, int)

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        error=QNetworkReply.NoError,
        error_string: str = "",
        attributes=None,
        raw_headers=None,
        url: str = "",
        content_type: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._status = status
        self._body = body
        self._error = error
        self._error_string = error_string
        self._content_type = content_type
        self._attributes = dict(attributes or {})
        self._raw_headers = {
            _header_name(k): bytes(v) for k, v in (raw_headers or {}).items()
        }
        self._url = url
        self._request = None
        self.aborted = False
        self.deleted = False
        self.settled = False

    # -- QNetworkReply surface --------------------------------------------

    def error(self):
        return self._error

    def errorString(self) -> str:
        return self._error_string

    def attribute(self, attr):
        if attr == QNetworkRequest.HttpStatusCodeAttribute:
            return self._status
        return self._attributes.get(attr)

    def header(self, header):
        if header == QNetworkRequest.ContentTypeHeader:
            return self._content_type or None
        return None

    def rawHeader(self, name) -> bytes:
        return self._raw_headers.get(_header_name(name), b"")

    def hasRawHeader(self, name) -> bool:
        return _header_name(name) in self._raw_headers

    def readAll(self) -> QByteArray:
        return QByteArray(self._body)

    def url(self) -> QUrl:
        if self._url:
            return QUrl(self._url)
        if self._request is not None:
            return self._request.url()
        return QUrl()

    def request(self):
        return self._request

    def abort(self) -> None:
        self.aborted = True
        self._error = QNetworkReply.OperationCanceledError

    def deleteLater(self) -> None:
        # Overridden so a fake never gets scheduled for real deletion
        # while a test still holds it.
        self.deleted = True

    # -- test driving ------------------------------------------------------

    def set_request(self, request) -> None:
        self._request = request

    def finish(self) -> None:
        self.settled = True
        self.finished.emit()

    def progress(self, received: int, total: int = -1) -> None:
        self.downloadProgress.emit(received, total)


class FakeNam(QObject):
    """Records every request and hands out scripted replies.

    ``replies`` are consumed in order. When the script runs out,
    ``responder`` (a :class:`FakeBlossomServer`, or any callable taking
    ``verb, request, body``) is asked for one; failing that, a bare 200.

    HEAD is scripted separately, through ``head_replies``, and answers
    404 when neither a script nor a responder covers it. A dedup probe
    is infrastructure that runs before most operations, so letting it
    eat the script would mean every test had to describe requests it is
    not about. Answering absent is also the safe default: it makes the
    store do the work rather than skip it.
    """

    def __init__(self, replies=None, parent=None, *, responder=None,
                 head_replies=None) -> None:
        super().__init__(parent)
        self.calls = []      # (verb, QNetworkRequest, body bytes or None)
        self.issued = []     # FakeReply objects, in order
        self._scripted = list(replies or [])
        self._head_scripted = list(head_replies or [])
        self._responder = responder

    def _issue(self, verb, request, body=None) -> FakeReply:
        self.calls.append((verb, request, body))
        if verb == "head":
            if self._head_scripted:
                reply = self._head_scripted.pop(0)
            elif self._responder is not None:
                reply = self._responder(verb, request, body)
            else:
                reply = FakeReply(status=404, error=QNetworkReply.ContentNotFoundError)
        elif self._scripted:
            reply = self._scripted.pop(0)
        elif self._responder is not None:
            reply = self._responder(verb, request, body)
        else:
            reply = FakeReply()
        reply.set_request(request)
        self.issued.append(reply)
        return reply

    def get(self, request) -> FakeReply:
        return self._issue("get", request)

    def head(self, request) -> FakeReply:
        return self._issue("head", request)

    def put(self, request, data) -> FakeReply:
        return self._issue("put", request, bytes(data))

    def post(self, request, data) -> FakeReply:
        return self._issue("post", request, bytes(data))

    def deleteResource(self, request) -> FakeReply:
        return self._issue("delete", request)

    # -- test driving ------------------------------------------------------

    def settle(self, *, limit: int = 200) -> None:
        """Drain the whole exchange, oldest first.

        Settling one reply usually issues the next request (a mirror
        after an upload, page two after page one), so this keeps going
        until nothing new appears rather than snapshotting the queue.
        """
        for _ in range(limit):
            if not self.settle_one(missing_ok=True):
                return
        raise AssertionError("fake transport did not settle")

    def settle_one(self, *, missing_ok: bool = False) -> bool:
        """Settle the oldest reply that has not settled yet.

        The step version of :meth:`settle`, for a test that needs to see
        the state of the world between two requests.
        """
        for reply in self.issued:
            if not reply.settled:
                reply.finish()
                return True
        if missing_ok:
            return False
        raise AssertionError("nothing left to settle")

    def settle_verb(self, verb: str, *, limit: int = 50) -> None:
        """Settle every outstanding reply to a ``verb`` request.

        Lets a test drain the dedup probes, which run before an upload
        and are not what it is about, and then look at what the store
        decided to do about them. Requests of other verbs are left in
        flight, including ones these replies cause.
        """
        for _ in range(limit):
            pending = [
                reply for (v, _rq, _b), reply in zip(self.calls, self.issued)
                if v == verb and not reply.settled
            ]
            if not pending:
                return
            pending[0].finish()
        raise AssertionError(f"fake transport kept issuing {verb}")

    def requests_to(self, suffix: str) -> List[QNetworkRequest]:
        return [r for _v, r, _b in self.calls if r.url().path().endswith(suffix)]


def redirect_reply(location: str = "https://evil.example/x") -> FakeReply:
    """A 302 the way Qt reports one under ManualRedirectPolicy: no
    transport error, a 3xx status, and the redirection target set."""
    return FakeReply(
        status=302,
        attributes={
            QNetworkRequest.Attribute.RedirectionTargetAttribute: QUrl(location),
        },
    )


def error_reply(status: int, *, reason: str = "", headers=None) -> FakeReply:
    """A refusal, optionally carrying the BUD-01 ``X-Reason`` header."""
    raw = dict(headers or {})
    if reason:
        raw[b"X-Reason"] = reason.encode("utf-8")
    return FakeReply(
        status=status,
        body=b"{}",
        error=QNetworkReply.UnknownContentError,
        error_string=f"HTTP {status}",
        raw_headers=raw,
    )


def descriptor(sha: str = SHA, *, server: str = SERVER, size: int = len(BODY),
               mime: str = "image/png", url: str = "", uploaded: int = 0,
               **extra) -> dict:
    """A BUD-02 blob descriptor."""
    payload = {
        "sha256": sha,
        "url": url or f"{server}/{sha}.png",
        "size": size,
        "type": mime,
        "uploaded": uploaded or int(time.time()),
    }
    payload.update(extra)
    return payload


def json_reply(payload, *, status: int = 200, headers=None) -> FakeReply:
    return FakeReply(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        raw_headers=dict(headers or {}),
    )


def collect(store: list):
    """Callback that appends whatever it is handed."""
    return lambda value=None: store.append(value)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

class FakeSigner:
    """Stands in for ``BunkerClient``. Signs nothing, records everything.

    ``defer=True`` holds every request until :meth:`release`, which is
    how a test proves an upload is sequential rather than a fan-out.
    """

    def __init__(self, *, defer: bool = False, failure: Optional[str] = None) -> None:
        self.requests: List[dict] = []     # unsigned events, in order
        self.signed: List[dict] = []
        self.pending: List[tuple] = []
        self.defer = defer
        self.failure = failure

    def sign_event(self, unsigned, on_success, on_failure, **_kw) -> None:
        self.requests.append(unsigned)
        if self.failure is not None:
            on_failure(self.failure)
            return
        if self.defer:
            self.pending.append((unsigned, on_success))
            return
        on_success(self._sign(unsigned))

    def _sign(self, unsigned: dict) -> dict:
        signed = {
            **unsigned,
            "pubkey": unsigned.get("pubkey") or PUBKEY,
            "id": f"{len(self.signed):064x}",
            "sig": "aa" * 64,
        }
        self.signed.append(signed)
        return signed

    def release(self) -> None:
        """Settle the oldest deferred signing request."""
        unsigned, on_success = self.pending.pop(0)
        on_success(self._sign(unsigned))

    def release_all(self) -> None:
        while self.pending:
            self.release()


class FakeSessionPool:
    """Stands in for ``BunkerSessionPool``. Counts prompts."""

    def __init__(self, signer: Optional[FakeSigner] = None,
                 error: Optional[str] = None) -> None:
        self.signer = signer or FakeSigner()
        self.error = error
        self.calls = 0

    def get(self, profile, on_ready=None, on_error=None) -> None:
        self.calls += 1
        if self.error is not None:
            if on_error is not None:
                on_error(self.error)
            return
        if on_ready is not None:
            on_ready(self.signer)


def FakeProfile(pubkey: str = PUBKEY):
    """The two attributes the Blossom paths read off a profile."""
    return SimpleNamespace(user_pubkey=pubkey, bunker_relays=[])


# ---------------------------------------------------------------------------
# A server that validates
# ---------------------------------------------------------------------------

class AuthRejected(Exception):
    """Raised inside the fake server when BUD-11 validation fails."""


class FakeBlossomServer:
    """An in-process Blossom server that applies the BUDs to us.

    Plugged into :class:`FakeNam` as ``responder``. It is deliberately
    strict, because the point of the protocol work is that a spec-
    compliant counterparty accepts what this app sends:

    - the Authorization header MUST be base64url without padding
      (BUD-11), decoded by :func:`decode_auth_header`;
    - ``created_at`` MUST be in the past and ``expiration`` in the
      future (BUD-11 validation rules 2 and 3);
    - a ``server`` tag, when present, MUST name this server's bare
      domain (rule 5), and a full origin is REFUSED the way a strict
      server would refuse it;
    - ``PUT /upload`` MUST carry ``X-SHA-256`` matching the body, and an
      ``x`` tag matching that hash (BUD-11's endpoint table);
    - ``PUT /mirror`` and ``DELETE /<sha256>`` MUST carry a matching
      ``x`` tag;
    - ``GET /list`` needs neither;
    - ``HEAD /<sha256>`` answers 200 when the blob is held and 404 when
      it is not (BUD-01), and takes no authorization at all.

    Every refusal answers with a status from the BUD-02 or BUD-12 table
    and a human-readable ``X-Reason``.
    """

    def __init__(
        self,
        origin: str = SERVER,
        *,
        blobs: Optional[List[dict]] = None,
        require_auth: bool = True,
        page_size: Optional[int] = None,
        ignore_cursor: bool = False,
        upload_status: int = 201,
        url_override: Optional[str] = None,
        sha_override: Optional[str] = None,
        nip94: Optional[list] = None,
        fail_with: Optional[int] = None,
        fail_reason: str = "",
        fail_headers: Optional[dict] = None,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.host = QUrl(self.origin).host().lower()
        self.blobs: Dict[str, dict] = {b["sha256"]: b for b in (blobs or [])}
        self.require_auth = require_auth
        self.page_size = page_size
        self.ignore_cursor = ignore_cursor
        self.upload_status = upload_status
        self.url_override = url_override
        self.sha_override = sha_override
        self.nip94 = nip94
        self.fail_with = fail_with
        self.fail_reason = fail_reason
        self.fail_headers = fail_headers or {}

        self.uploads: List[bytes] = []
        self.mirrors: List[str] = []
        self.deletes: List[str] = []
        self.probes: List[str] = []           # hashes asked about by HEAD
        self.list_queries: List[tuple] = []   # (cursor, limit)
        self.tokens: List[dict] = []          # every decoded auth event

    # -- responder ---------------------------------------------------------

    def __call__(self, verb, request, body) -> FakeReply:
        url = request.url()
        path = url.path()
        try:
            if verb == "put" and path == "/upload":
                return self._upload(request, body)
            if verb == "put" and path == "/mirror":
                return self._mirror(request, body)
            if verb == "get" and path.startswith("/list/"):
                return self._list(request, url)
            if verb == "head":
                return self._has_blob(path.lstrip("/"))
            if verb == "delete":
                return self._delete(request, path.lstrip("/"))
        except AuthRejected as exc:
            return error_reply(401, reason=str(exc))
        return error_reply(404, reason="No such endpoint.")

    # -- endpoints ---------------------------------------------------------

    def _upload(self, request, body: bytes) -> FakeReply:
        sha = hashlib.sha256(body).hexdigest()
        header_sha = bytes(request.rawHeader("X-SHA-256")).decode("ascii")
        if not header_sha:
            return error_reply(400, reason="Missing X-SHA-256.")
        if header_sha != sha:
            return error_reply(409, reason="The provided X-SHA-256 does not match the request body.")
        self._authorize(request, action="upload", blob_hash=sha)
        if self.fail_with:
            return error_reply(self.fail_with, reason=self.fail_reason,
                               headers=self.fail_headers)
        self.uploads.append(body)
        self.blobs.setdefault(sha, descriptor(sha, server=self.origin,
                                              size=len(body)))
        return self._descriptor_reply(sha, size=len(body))

    def _mirror(self, request, body: bytes) -> FakeReply:
        payload = json.loads(bytes(body).decode("utf-8"))
        source = payload.get("url", "")
        # A real server would fetch the source. Here the hash comes from
        # the token, which is exactly the value BUD-11 requires.
        token = self._authorize(request, action="upload")
        hashes = tag_values(token, "x") if token else []
        if not hashes:
            raise AuthRejected("Mirror requires an x tag.")
        if self.fail_with:
            return error_reply(self.fail_with, reason=self.fail_reason,
                               headers=self.fail_headers)
        self.mirrors.append(source)
        self.blobs.setdefault(hashes[0], descriptor(hashes[0], server=self.origin))
        return self._descriptor_reply(hashes[0])

    def _list(self, request, url: QUrl) -> FakeReply:
        query = QUrlQuery(url.query())
        cursor = query.queryItemValue("cursor") or ""
        raw_limit = query.queryItemValue("limit") or ""
        limit = int(raw_limit) if raw_limit.isdigit() else None
        self.list_queries.append((cursor, limit))
        if self.require_auth:
            self._authorize(request, action="list", optional=True)
        if self.fail_with:
            return error_reply(self.fail_with, reason=self.fail_reason,
                               headers=self.fail_headers)

        # BUD-12: newest first, and never include the blob at the cursor.
        ordered = sorted(self.blobs.values(), key=lambda b: -int(b.get("uploaded") or 0))
        if cursor and not self.ignore_cursor:
            names = [b["sha256"] for b in ordered]
            if cursor in names:
                ordered = ordered[names.index(cursor) + 1:]
        size = limit or self.page_size
        if size:
            ordered = ordered[:size]
        return json_reply(ordered)

    def _has_blob(self, sha: str) -> FakeReply:
        """BUD-01 ``HEAD /<sha256>``: 200 when held, 404 when not.

        No authorization is required or read. BUD-11 marks auth optional
        on this endpoint, and a probe that demanded a token would cost
        the prompt the probe exists to save.
        """
        sha = sha.split(".", 1)[0].lower()
        self.probes.append(sha)
        if sha in self.blobs:
            return FakeReply(status=200)
        return error_reply(404, reason="The blob does not exist.")

    def _delete(self, request, sha: str) -> FakeReply:
        self._authorize(request, action="delete", blob_hash=sha)
        if self.fail_with:
            return error_reply(self.fail_with, reason=self.fail_reason,
                               headers=self.fail_headers)
        self.deletes.append(sha)
        self.blobs.pop(sha, None)
        return FakeReply(status=200, body=b"{}")

    # -- validation --------------------------------------------------------

    def _authorize(self, request, *, action: str,
                   blob_hash: str = "", optional: bool = False):
        raw = bytes(request.rawHeader("Authorization"))
        if not raw:
            if optional or not self.require_auth:
                return None
            raise AuthRejected("Authorization is required.")
        try:
            event = decode_auth_header(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthRejected(f"Malformed authorization token: {exc}")
        self.tokens.append(event)

        if event.get("kind") != 24242:
            raise AuthRejected("Authorization event is not kind 24242.")
        now = int(time.time())
        if int(event.get("created_at", 0)) > now:
            raise AuthRejected("Authorization created_at is in the future.")
        expiration = first_tag(event, "expiration")
        if not expiration or int(expiration) <= now:
            raise AuthRejected("Authorization has expired.")
        if first_tag(event, "t") != action:
            raise AuthRejected(f"Authorization is not for {action}.")

        servers = tag_values(event, "server")
        if servers and self.host not in servers:
            raise AuthRejected("Authorization names another server.")
        if blob_hash:
            hashes = tag_values(event, "x")
            if not hashes or blob_hash not in hashes:
                raise AuthRejected("Authorization does not cover this blob.")
        return event

    # -- responses ---------------------------------------------------------

    def _descriptor_reply(self, sha: str, *, size: int = 0) -> FakeReply:
        payload = descriptor(
            self.sha_override or sha,
            server=self.origin,
            size=size or 1,
            url=self.url_override or "",
        )
        if self.nip94 is not None:
            payload["nip94"] = self.nip94
        return json_reply(payload, status=self.upload_status)
