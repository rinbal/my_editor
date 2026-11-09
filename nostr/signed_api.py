# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The call pipeline shared by the app's clients of signed JSON APIs.

One call is: a fresh NIP-98 credential signed immediately before it is
sent (``nostr/nip98.py``; an event id is accepted once and only for 60
seconds), the request with exactly the bytes that were hashed, a reply
read within a size limit, and exactly one of ``on_success`` and
``on_failure``, never inside the call that started it: a call refused
before anything is sent answers on the next turn (``nostr/later.py``),
like every other answer. A credential that took most of its window to be
approved on the person's phone and then expired on the way is signed
once more. A cancelled call fires nothing, unless it was made not to be
cancellable (a revocation that must still reach the server). Nothing a
server sends raises into Qt.

What differs between APIs is left to subclasses: where a request goes
and which URL its signature names (``url_for``, ``request_url_for``),
what a successful answer looks like (``_parse_success``), how a refusal
reads (``_http_error``), the error type (``_error``), whether a call can
go at all (``_unavailable``), what else a request carries
(``_prepare_request``), and which refusals another attempt can cure
(``_retry``). The EINUNDZWANZIG membership client
(``nostr/einundzwanzig_api/client.py``) and the STANDUP submission
client (``nostr/standup/client.py``) are the two.
"""

from __future__ import annotations

import copy
import json
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from . import nip98
from .bunker import is_signer_silent
from .http_answers import clean_text
from .later import answer_later

# The generic failures every client reports, as plain strings: the
# membership client's ErrorCode uses exactly these values.
OFFLINE = "offline"
TIMEOUT = "timeout"
BAD_RESPONSE = "bad_response"
SIGNER_DECLINED = "signer_declined"
SIGNER_UNREACHABLE = "signer_unreachable"

# ``sign(unsigned_event, on_success, on_failure)``: hand an unsigned event
# to whatever signer the window holds. Exactly one callback must fire.
SignFn = Callable[[dict, Callable[[dict], None], Callable[[str], None]], None]


@dataclass
class Call:
    """One API call, from the first signature to the one callback. A
    client that keeps more about its calls subclasses this and names the
    subclass in :attr:`SignedJsonApi.call_type`."""

    method: str
    path: str
    body: Optional[bytes]
    signed: bool
    parse: Callable[[Any], Any]
    on_success: Callable[[Any], None]
    on_failure: Callable[[Any], None]
    generation: int
    resigned: bool = False
    done: bool = False
    # The answer is refused beyond this size (None: the client's limit).
    max_response_bytes: Optional[int] = None
    # Who signed the request that was sent (the NIP-98 event's pubkey).
    signed_by: Optional[str] = None
    # False for a call that cancel() leaves to finish and answer.
    cancellable: bool = True
    # Told once, right before the request is written to the network: from
    # then on it may arrive whatever happens here.
    on_sending: Optional[Callable[[], None]] = None


def json_body(payload: Any) -> bytes:
    """The one serialisation of a body: these bytes are hashed AND sent."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def reply_header(reply, name: str) -> bytes:
    """A response header's raw value, or ``b""``.

    The name goes in as ``str`` first: PySide6 6.11 accepts nothing
    else for ``QNetworkReply.rawHeader``, and a ``bytes`` name raises
    TypeError there. Older bindings took a QByteArray, hence the second
    try.
    """
    for key in (name, name.encode("ascii")):
        try:
            return bytes(reply.rawHeader(key))
        except (AttributeError, TypeError):
            continue
    return b""


_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f?#@]")


def usable_base_url(value: Any, *, http_hosts: frozenset = frozenset()) -> str:
    """``value`` as an address this app may send signed requests to,
    without a trailing slash, or ``""``.

    https to any host. Plain http only to the hosts in ``http_hosts``
    (this computer, for development), compared exactly, so
    ``http://localhost.evil.example`` is not local. No user name or
    password (``http://localhost@evil.example`` names evil.example), no
    query and no fragment: none of them belongs in a base address, which
    the request path is appended to.
    """
    text = value.strip().rstrip("/") if isinstance(value, str) else ""
    try:
        parts = urlsplit(text)
        parts.port  # an invalid port raises here
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    after_scheme = text[len(parts.scheme) + len("://"):]
    if not text.lower().startswith(parts.scheme + "://") or _UNSAFE_URL_CHARS.search(after_scheme):
        return ""
    if parts.scheme == "http" and parts.hostname not in http_hosts:
        return ""
    return text


def signer_failure_code(reason: str) -> str:
    """Sort a signer failure into "said no" and "never answered".

    The second is recoverable on the person's phone and gets different
    advice, so it must not read as a refusal. ``not connected`` is what
    ``BunkerClient`` says when its channel is down.
    """
    text = str(reason or "")
    unreachable = is_signer_silent(text) or "not connected" in text.lower()
    return SIGNER_UNREACHABLE if unreachable else SIGNER_DECLINED


class SignedJsonApi(QObject):
    """The pipeline; see the module docstring. Seams: ``sign`` (see
    :data:`SignFn`), ``nam`` and ``clock`` (unix seconds), so no test
    touches the network, a signer or the wall clock."""

    # Network budget per request. Qt resets this whenever bytes move, so a
    # slow but progressing answer is not cut off.
    network_timeout_ms: int = 15_000
    # A credential older than this when it is sent may already be outside
    # the server's 60 second window by the time it arrives.
    stale_signature_seconds: int = 45
    max_response_bytes: int = 1024 * 1024
    max_redirects: int = 3
    user_agent: bytes = b"my-editor/1"
    call_type: type = Call

    def __init__(self, sign: SignFn, *, nam: Optional[QNetworkAccessManager] = None,
                 clock: Optional[Callable[[], float]] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sign = sign
        self._nam = nam or QNetworkAccessManager(self)
        self._clock = clock or time.time
        self._generation = 0
        self._inflight: Dict[int, Any] = {}

    # -- what subclasses decide ---------------------------------------------

    def url_for(self, path: str) -> str:
        """The URL a NIP-98 ``u`` tag names for ``path``."""
        raise NotImplementedError

    def request_url_for(self, path: str) -> str:
        """Where the request for ``path`` travels (by default the same)."""
        return self.url_for(path)

    def _error(self, code: str, **fields) -> Any:
        """The client's error for ``code``: status, message, retry_after..."""
        raise NotImplementedError

    def _unavailable(self, call: Call) -> Optional[Any]:
        """An error when the call cannot go at all (no server), else None."""
        return None

    def _parse_success(self, call: Call, status: int, raw: bytes):
        """``(value,)`` for a 2xx answer, or an error."""
        raise NotImplementedError

    def _http_error(self, status: int, raw: bytes, reply) -> Any:
        raise NotImplementedError

    def _prepare_request(self, call: Call, request) -> None:
        """Anything else the request carries (a session token)."""

    def _retry(self, call: Call, error: Any, stale: bool) -> bool:
        """True when the refusal ``error`` was answered by trying again
        (the subclass started that attempt). By default nothing is."""
        return False

    def _signer_error(self, reason: str) -> Any:
        return self._error(signer_failure_code(reason), message=clean_text(reason))

    # -- the pipeline -------------------------------------------------------

    def cancel(self) -> None:
        """Drop every call in progress. No callback fires for them.

        A signer prompt already on the person's phone cannot be withdrawn;
        its answer is simply ignored.
        """
        self._generation += 1
        inflight, self._inflight = self._inflight, {}
        for reply in inflight.values():
            try:
                reply.abort()
            except RuntimeError:  # already deleted on the C++ side
                pass

    def drop(self, call: Call) -> None:
        """``call`` is answered no more, and a call still waiting for its
        signature is never sent. One already sent is not taken back."""
        call.done = True

    def _new_call(self, method, path, body, signed, parse, on_success, on_failure,
                  **fields) -> Call:
        """A call of :attr:`call_type`; ``fields`` are the subclass's own."""
        return self.call_type(method, path, body, signed, parse, on_success, on_failure,
                              self._generation, **fields)

    def _start(self, method, path, body, signed, parse, on_success, on_failure) -> Call:
        call = self._new_call(method, path, body, signed, parse, on_success, on_failure)
        self._run(call)
        return call

    def _run(self, call: Call) -> None:
        problem = self._unavailable(call)
        if problem is not None:
            answer_later(self._fail, call, problem)
            return
        if call.signed:
            self._sign_and_send(call)
        else:
            self._send(call, None)

    def _live(self, call: Call) -> bool:
        return not call.done and (not call.cancellable or call.generation == self._generation)

    def _sign_and_send(self, call: Call) -> None:
        # Built here, immediately before signing, every time: an event id
        # is accepted once and its timestamp only for 60 seconds.
        unsigned = nip98.build_unsigned_auth_event(
            self.url_for(call.path), call.method, call.body, now=int(self._clock()),
        )
        answered = {"done": False}

        def _signed(event: dict) -> None:
            if answered["done"]:
                return
            answered["done"] = True
            if not self._live(call):
                return
            problem = nip98.signed_event_problem(event, unsigned)
            if problem:
                self._fail(call, self._error(SIGNER_DECLINED, message=problem))
                return
            self._send(call, event)

        def _refused(reason: str) -> None:
            if answered["done"]:
                return
            answered["done"] = True
            if self._live(call):
                self._fail(call, self._signer_error(reason))

        try:
            # A copy, so a signer that edits what it is handed cannot
            # change what its answer is checked against.
            self._sign(copy.deepcopy(unsigned), _signed, _refused)
        except Exception as exc:  # noqa: BLE001, a broken adapter must not reach Qt
            if not answered["done"]:
                answered["done"] = True
                if self._live(call):
                    self._fail(call, self._error(
                        SIGNER_UNREACHABLE, message=clean_text(f"signer unavailable: {exc}")))

    def _send(self, call: Call, signed: Optional[dict]) -> None:
        request = QNetworkRequest(QUrl(self.request_url_for(call.path)))
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"User-Agent", self.user_agent)
        request.setTransferTimeout(self.network_timeout_ms)
        # A redirect re-sends the headers, the signature included, so it
        # may only stay on the service's own origin.
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy,
        )
        request.setMaximumRedirectsAllowed(self.max_redirects)

        stale = False
        if signed is not None:
            request.setRawHeader(b"Authorization", nip98.authorization_header_value(signed))
            stale = (self._clock() - signed["created_at"]) > self.stale_signature_seconds
            call.signed_by = signed["pubkey"]
        self._prepare_request(call, request)

        if call.on_sending is not None:
            on_sending, call.on_sending = call.on_sending, None
            try:
                on_sending()
            except Exception:  # noqa: BLE001, a caller's bug must not unwind into Qt
                traceback.print_exc()

        if nip98.has_body(call.body):
            # Exactly this value: anything else is refused with 415.
            request.setRawHeader(b"Content-Type", nip98.JSON_CONTENT_TYPE.encode("ascii"))
            if call.method == "POST":
                reply = self._nam.post(request, QByteArray(call.body))
            else:
                reply = self._nam.sendCustomRequest(request, call.method.encode("ascii"),
                                                    QByteArray(call.body))
        elif call.method == "GET":
            reply = self._nam.get(request)
        elif call.method == "DELETE":
            reply = self._nam.deleteResource(request)
        else:
            # No body means no Content-Type and no payload tag. Qt adds no
            # Content-Type of its own to an empty POST.
            reply = self._nam.post(request, QByteArray())

        key = id(reply)
        if call.cancellable:
            self._inflight[key] = reply
        flags = {"oversize": False}
        limit = call.max_response_bytes or self.max_response_bytes

        def _guard(received: int, total: int) -> None:
            if flags["oversize"]:
                return
            if received > limit or total > limit:
                flags["oversize"] = True
                reply.abort()

        reply.downloadProgress.connect(_guard)
        reply.finished.connect(lambda: self._on_finished(call, reply, key, flags, stale, limit))

    def _on_finished(self, call: Call, reply, key: int, flags: dict, stale: bool,
                     limit: int) -> None:
        self._inflight.pop(key, None)
        try:
            if not self._live(call):
                return
            outcome = self._read(call, reply, flags, limit)
        finally:
            reply.deleteLater()

        if isinstance(outcome, tuple):
            self._succeed(call, outcome[0])
            return
        if self._retry(call, outcome, stale):
            return
        self._fail(call, outcome)

    def _read(self, call: Call, reply, flags: dict, limit: int):
        """``(value,)`` on success, else the error."""
        status = int(reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0)
        if flags["oversize"]:
            return self._error(BAD_RESPONSE, status=status or None,
                               message="the answer was larger than allowed")
        if 300 <= status < 400:
            return self._error(BAD_RESPONSE, status=status,
                               message="the server redirected the request")
        if status == 0:
            return self._transport_error(reply.error())

        raw = bytes(reply.readAll())
        if len(raw) > limit:
            return self._error(BAD_RESPONSE, status=status,
                               message="the answer was larger than allowed")
        if not 200 <= status < 300:
            return self._http_error(status, raw, reply)
        return self._parse_success(call, status, raw)

    def _transport_error(self, error) -> Any:
        if error in (QNetworkReply.NetworkError.TimeoutError,
                     QNetworkReply.NetworkError.OperationCanceledError):
            # Qt reports its own transfer timeout as a cancelled operation.
            # Our deliberate aborts never get here: an oversize answer is
            # caught above and a cancelled call is no longer live.
            return self._error(TIMEOUT)
        if error in (QNetworkReply.NetworkError.InsecureRedirectError,
                     QNetworkReply.NetworkError.TooManyRedirectsError):
            return self._error(BAD_RESPONSE, message="the server redirected the request")
        if error == QNetworkReply.NetworkError.NoError:
            return self._error(BAD_RESPONSE, message="the answer carried no status")
        return self._error(OFFLINE)

    # -- delivery -----------------------------------------------------------

    def _succeed(self, call: Call, value: Any) -> None:
        if not self._live(call):
            return
        call.done = True
        try:
            call.on_success(value)
        except Exception:  # noqa: BLE001, a caller's bug must not unwind into Qt
            traceback.print_exc()

    def _fail(self, call: Call, error: Any) -> None:
        if not self._live(call):
            return
        call.done = True
        try:
            call.on_failure(error)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
