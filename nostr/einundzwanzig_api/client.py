# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Qt client for the membership API, and where its requests go.

:class:`MembershipApi` signs (through a plain callable, see
:func:`session_signer`) and sends every call to the membership service
(:func:`service_url`), naming the association (:func:`upstream_url`) in
each signature. The package docstring has the reasoning.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

import url_safety

from .. import nip98
from ..bunker import is_signer_silent
from .messages import application_text_problem, email_problem, nip05_handle_problem
from .models import (
    ApiError,
    Erasure,
    ErrorCode,
    FeeEntry,
    Invoice,
    MembershipConfig,
    MembershipExport,
    MembershipStatus,
    _clean_field_errors,
    _clean_text,
    parse_config,
    parse_erasure,
    parse_export,
    parse_invoice,
    parse_membership,
    parse_payments,
    parse_retry_after,
)


# --------------------------------------------------------------------------- #
# Endpoint and limits                                                          #
# --------------------------------------------------------------------------- #

# The server rebuilds the URL it verifies the NIP-98 ``u`` tag against from
# its own configured scheme and host, so this must be exactly the server
# URL the spec lists: no trailing slash, no explicit port.
BASE_URL: str = "https://verein.einundzwanzig.space"
API_PREFIX: str = "/api/v1/membership"

# Network budget per request. Qt resets this whenever bytes move, so a
# slow but progressing answer is not cut off.
NETWORK_TIMEOUT_MS: int = 15_000

# How long a remote signer gets to answer one signing request. Passed to
# ``BunkerClient.sign_event`` by :func:`session_signer`. It is long enough
# for a person to find their phone and short enough that an approved
# event usually still lands inside the server's 60 second window.
SIGN_TIMEOUT_MS: int = 60_000

# A credential older than this when it is sent may already be outside the
# server's 60 second window by the time it arrives. If such a request is
# refused with 401, it is signed once more and resent; see ``_on_finished``.
STALE_SIGNATURE_SECONDS: int = 45

# Every answer of this API is a small JSON document. The data export is
# the largest and is still a few kilobytes.
MAX_RESPONSE_BYTES: int = 1024 * 1024

# Same-origin redirects only, and not many of them.
_MAX_REDIRECTS: int = 3

_USER_AGENT = b"my-editor-membership/1"


# --------------------------------------------------------------------------- #
# The membership service                                                       #
# --------------------------------------------------------------------------- #

# Developers and self-hosters set this; it wins over the build's service.
ENV_SERVICE_URL: str = "MYEDITOR_MEMBERSHIP_SERVICE"

# Development only: the association's address the signatures name, for a
# sidecar whose E21_UPSTREAM is a test system or a stand-in. Signatures
# must name the URL the association behind the sidecar checks, so the two
# settings go together. Unset (always, in production), it is BASE_URL.
ENV_UPSTREAM_URL: str = "MYEDITOR_MEMBERSHIP_UPSTREAM"

# How long the app waits for the service to say whether it can help.
STATUS_TIMEOUT_MS: int = 8_000


# Plain http is allowed to exactly these hosts, for development.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f?#@]")


def _usable_service(value: Any) -> str:
    """``value`` as an address this app may send signed requests to,
    without a trailing slash, or ``""``.

    https to any host. Plain http only to this computer, for development:
    the host must be exactly ``localhost``, ``127.0.0.1`` or ``[::1]``,
    so ``http://localhost.evil.example`` is not local. No user name or
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
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        return ""
    return text


def service_url() -> str:
    """The membership service this build uses, or ``""``.

    ``MYEDITOR_MEMBERSHIP_SERVICE`` from the environment first, then the
    build's ``MEMBERSHIP_SERVICE_URL`` (constants.py). An address
    :func:`_usable_service` refuses counts as none.
    """
    from_env = _usable_service(os.environ.get(ENV_SERVICE_URL, ""))
    if from_env:
        return from_env
    try:
        from constants import MEMBERSHIP_SERVICE_URL
    except ImportError:
        return ""
    return _usable_service(MEMBERSHIP_SERVICE_URL)


def upstream_url() -> str:
    """The association's address the signatures name.

    :data:`BASE_URL`, unless ``MYEDITOR_MEMBERSHIP_UPSTREAM`` names a
    usable address (the same rules as the service's): a developer's way
    to test against the sidecar's ``E21_UPSTREAM``.
    """
    return _usable_service(os.environ.get(ENV_UPSTREAM_URL, "")) or BASE_URL


# --------------------------------------------------------------------------- #
# Signer failures                                                              #
# --------------------------------------------------------------------------- #

def _signer_error(reason: str) -> ApiError:
    """Sort a signer failure into "said no" and "never answered".

    The second is recoverable on the user's phone and gets different
    advice, so it must not read as a refusal. ``not connected`` is what
    ``BunkerClient`` says when its channel is down.
    """
    text = str(reason or "")
    lowered = text.lower()
    unreachable = is_signer_silent(text) or "not connected" in lowered
    code = ErrorCode.SIGNER_UNREACHABLE if unreachable else ErrorCode.SIGNER_DECLINED
    return ApiError(code, message=_clean_text(text))


# --------------------------------------------------------------------------- #
# The client                                                                   #
# --------------------------------------------------------------------------- #

# ``sign(unsigned_event, on_success, on_failure)``: hand an unsigned event
# to whatever signer the window holds. Exactly one callback must fire.
SignFn = Callable[[dict, Callable[[dict], None], Callable[[str], None]], None]


class _Unset:
    """Marks an application field the caller did not pass."""

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()


@dataclass
class _Call:
    """One API call, from the first signature to the one callback."""

    method: str
    path: str
    body: Optional[bytes]
    signed: bool
    parse: Callable[[Any], Any]
    on_success: Callable[[Any], None]
    on_failure: Callable[[ApiError], None]
    generation: int
    resigned: bool = False
    done: bool = False


def _json_body(payload: Dict[str, Any]) -> bytes:
    """The one serialisation of a body: these bytes are hashed AND sent."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _reply_header(reply, name: str) -> bytes:
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


def _year_segment(year: int) -> str:
    if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9999:
        raise ValueError("year must be a four-digit integer")
    return str(year)


class MembershipApi(QObject):
    """Callback-style client for the membership API's main surface.

    Every method takes ``on_success`` and ``on_failure`` and exactly one
    of them fires, ``on_failure`` always with an :class:`ApiError`. A
    failure that needs no network (no service, a field that would be
    refused) is reported before the method returns and before anything
    is signed, so the user is never asked to approve a request that
    cannot succeed.

    Seams: ``sign`` (see :data:`SignFn`), ``service_url`` (None reads
    :func:`service_url`), ``base_url`` (None reads :func:`upstream_url`),
    ``nam`` and ``clock`` (unix seconds), so no test touches the network,
    a signer or the wall clock.

    ``base_url`` is the association's own address: what the signatures
    name. ``service_url`` is where the requests travel.
    """

    def __init__(
        self,
        sign: SignFn,
        *,
        service_url: Optional[str] = None,
        base_url: Optional[str] = None,
        nam: Optional[QNetworkAccessManager] = None,
        clock: Optional[Callable[[], float]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        base = str(base_url).rstrip("/") if base_url is not None else upstream_url()
        if not url_safety.origin_of(base):
            raise ValueError("base_url must be an absolute http(s) URL")
        self._sign = sign
        self._service_url = (_usable_service(service_url) if service_url is not None
                             else _resolve_service_url())
        self._base_url = base
        self._nam = nam or QNetworkAccessManager(self)
        self._clock = clock or time.time
        self._generation = 0
        self._inflight: Dict[int, Any] = {}

    # -- public surface ----------------------------------------------------

    @property
    def configured(self) -> bool:
        """This build names a membership service."""
        return bool(self._service_url)

    def check_service(self, on_done: Callable[[bool], None]) -> None:
        """Ask the service whether joining is possible; ``on_done(bool)`` once.

        A service with no key, an unreachable one, or no service at all
        answers False, and the window then offers the association's
        website instead of a flow that would end in an error.
        """
        if not self._service_url:
            QTimer.singleShot(0, lambda: on_done(False))
            return
        request = QNetworkRequest(QUrl(f"{self._service_url}/status"))
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setTransferTimeout(STATUS_TIMEOUT_MS)
        reply = self._nam.get(request)

        def finished() -> None:
            ok = False
            try:
                if reply.error() == QNetworkReply.NetworkError.NoError:
                    data = json.loads(bytes(reply.readAll())[:65536].decode("utf-8"))
                    ok = isinstance(data, dict) and data.get("membership") is True
            except (ValueError, UnicodeDecodeError):
                ok = False
            finally:
                reply.deleteLater()
            on_done(ok)

        reply.finished.connect(finished)

    def url_for(self, path: str) -> str:
        """The association's own URL for ``path``: what the NIP-98 ``u`` tag
        names. One place builds it, so signature and request always agree."""
        return f"{self._base_url}{API_PREFIX}{path}"

    def request_url_for(self, path: str) -> str:
        """Where the request for ``path`` actually travels: the service."""
        return f"{self._service_url}{API_PREFIX}{path}"

    def config(self, on_success: Callable[[MembershipConfig], None],
               on_failure: Callable[[ApiError], None]) -> None:
        """The fee, the current fee year and the statutes. Key only, no
        signature: the fee has to be visible before anybody signs."""
        self._start("GET", "/config", None, False, parse_config, on_success, on_failure)

    def me(self, on_success: Callable[[MembershipStatus], None],
           on_failure: Callable[[ApiError], None]) -> None:
        """The signing user's own membership."""
        self._start("GET", "/me", None, True, parse_membership, on_success, on_failure)

    def apply(
        self,
        on_success: Callable[[MembershipStatus], None],
        on_failure: Callable[[ApiError], None],
        *,
        statutes_accepted: Any = True,
        application_text: Any = UNSET,
        email: Any = UNSET,
        no_email: Any = UNSET,
        nip05_handle: Any = UNSET,
    ) -> None:
        """File the application, or update the contact data of one.

        Only the fields passed are sent. A field left out is left alone
        on the server; a field passed as None is sent as null, which
        CLEARS the stored value. The body is signed as a whole, so the
        difference is real: sending a key the user never touched would
        overwrite what they have on file.

        ``statutes_accepted`` is True (the consent, required the first
        time) or UNSET (a repeat application that only updates contact
        data). The server refuses an explicit false, so it is not
        accepted here either. ``no_email`` is a bool or UNSET; the server
        has no null for it. Misuse raises TypeError: it is a bug in the
        caller, not a condition to report to the user.
        """
        payload: Dict[str, Any] = {}
        if statutes_accepted is True:
            payload["statutes_accepted"] = True
        elif statutes_accepted is not UNSET:
            raise TypeError("statutes_accepted must be True or UNSET")
        for name, value in (
            ("application_text", application_text),
            ("email", email),
            ("nip05_handle", nip05_handle),
        ):
            if value is UNSET:
                continue
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string, None or UNSET")
            payload[name] = value
        if no_email is not UNSET:
            if not isinstance(no_email, bool):
                raise TypeError("no_email must be a bool or UNSET")
            payload["no_email"] = no_email

        problems: Dict[str, List[str]] = {}
        for name, check in (
            ("nip05_handle", nip05_handle_problem),
            ("email", email_problem),
            ("application_text", application_text_problem),
        ):
            value = payload.get(name)
            if isinstance(value, str):
                problem = check(value)
                if problem:
                    problems[name] = [problem]
        call = self._new_call("POST", "/applications", _json_body(payload), True,
                              parse_membership, on_success, on_failure)
        # A missing service outranks a field problem: without one nothing
        # works, and fixing the field would only lead to the next refusal.
        if problems and self._service_url:
            self._fail(call, ApiError(ErrorCode.VALIDATION, field_errors=problems))
            return
        self._run(call)

    def create_invoice(
        self,
        year: int,
        on_success: Callable[[Invoice], None],
        on_failure: Callable[[ApiError], None],
        *,
        return_url: Optional[str] = None,
    ) -> None:
        """A checkout for ``year``, which must be the current fee year.

        Idempotent on the server: a second call hands back the existing
        invoice with ``created`` False. ``return_url`` must be on the
        association's allowlist or the call is refused; without one the
        body is left out entirely, as the spec allows. Raises ValueError
        for a year that is not four digits.
        """
        body = _json_body({"return_url": return_url}) if return_url else None
        self._start("POST", f"/payments/{_year_segment(year)}/invoice", body, True,
                    parse_invoice, on_success, on_failure)

    def refresh_payment(
        self,
        year: int,
        on_success: Callable[[Invoice], None],
        on_failure: Callable[[ApiError], None],
    ) -> None:
        """Re-read the invoice for ``year`` from the payment processor.

        This is also the repair path for a lost payment notification:
        a settled invoice found here grants the membership exactly as
        the notification would have. Creates nothing.
        """
        self._start("POST", f"/payments/{_year_segment(year)}/refresh", None, True,
                    parse_invoice, on_success, on_failure)

    def payments(self, on_success: Callable[[Tuple[FeeEntry, ...]], None],
                 on_failure: Callable[[ApiError], None]) -> None:
        """Every fee on record, newest year first."""
        self._start("GET", "/payments", None, True, parse_payments, on_success, on_failure)

    def export_data(self, on_success: Callable[[MembershipExport], None],
                    on_failure: Callable[[ApiError], None]) -> None:
        """Everything the association stores about the signing user."""
        self._start("GET", "/export", None, True, parse_export, on_success, on_failure)

    def erase(self, on_success: Callable[[Erasure], None],
              on_failure: Callable[[ApiError], None]) -> None:
        """Erase the signing user's personal data. Not a ban, and paid
        fees stay behind as anonymised bookkeeping."""
        self._start("DELETE", "/me", None, True, parse_erasure, on_success, on_failure)

    def cancel(self) -> None:
        """Drop every call in progress. No callback fires for them.

        A signer prompt already on the user's phone cannot be withdrawn;
        its answer is simply ignored.
        """
        self._generation += 1
        inflight, self._inflight = self._inflight, {}
        for reply in inflight.values():
            try:
                reply.abort()
            except RuntimeError:  # already deleted on the C++ side
                pass

    # -- the pipeline ------------------------------------------------------

    def _new_call(self, method, path, body, signed, parse, on_success, on_failure) -> _Call:
        return _Call(method, path, body, signed, parse, on_success, on_failure,
                     self._generation)

    def _start(self, method, path, body, signed, parse, on_success, on_failure) -> None:
        self._run(self._new_call(method, path, body, signed, parse, on_success, on_failure))

    def _run(self, call: _Call) -> None:
        if not self._service_url:
            self._fail(call, ApiError(ErrorCode.UNAVAILABLE))
            return
        if call.signed:
            self._sign_and_send(call)
        else:
            self._send(call, None)

    def _live(self, call: _Call) -> bool:
        return not call.done and call.generation == self._generation

    def _sign_and_send(self, call: _Call) -> None:
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
                self._fail(call, ApiError(ErrorCode.SIGNER_DECLINED, message=problem))
                return
            self._send(call, event)

        def _refused(reason: str) -> None:
            if answered["done"]:
                return
            answered["done"] = True
            if self._live(call):
                self._fail(call, _signer_error(reason))

        try:
            # A copy, so a signer that edits what it is handed cannot
            # change what its answer is checked against.
            self._sign(copy.deepcopy(unsigned), _signed, _refused)
        except Exception as exc:  # noqa: BLE001, a broken adapter must not reach Qt
            if not answered["done"]:
                answered["done"] = True
                if self._live(call):
                    self._fail(call, ApiError(
                        ErrorCode.SIGNER_UNREACHABLE,
                        message=_clean_text(f"signer unavailable: {exc}"),
                    ))

    def _send(self, call: _Call, signed: Optional[dict]) -> None:
        request = QNetworkRequest(QUrl(self.request_url_for(call.path)))
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setTransferTimeout(NETWORK_TIMEOUT_MS)
        # A redirect re-sends the headers, the signature included, so it
        # may only stay on the service's own origin.
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy,
        )
        request.setMaximumRedirectsAllowed(_MAX_REDIRECTS)

        stale = False
        if signed is not None:
            request.setRawHeader(b"Authorization", nip98.authorization_header_value(signed))
            stale = (self._clock() - signed["created_at"]) > STALE_SIGNATURE_SECONDS

        if nip98.has_body(call.body):
            # Exactly this value: anything else is refused with 415.
            request.setRawHeader(b"Content-Type", nip98.JSON_CONTENT_TYPE.encode("ascii"))
            reply = self._nam.post(request, QByteArray(call.body))
        elif call.method == "GET":
            reply = self._nam.get(request)
        elif call.method == "DELETE":
            reply = self._nam.deleteResource(request)
        else:
            # No body means no Content-Type and no payload tag. Qt adds no
            # Content-Type of its own to an empty POST.
            reply = self._nam.post(request, QByteArray())

        key = id(reply)
        self._inflight[key] = reply
        flags = {"oversize": False}

        def _guard(received: int, total: int) -> None:
            if flags["oversize"]:
                return
            if received > MAX_RESPONSE_BYTES or total > MAX_RESPONSE_BYTES:
                flags["oversize"] = True
                reply.abort()

        reply.downloadProgress.connect(_guard)
        reply.finished.connect(lambda: self._on_finished(call, reply, key, flags, stale))

    def _on_finished(self, call: _Call, reply, key: int, flags: dict, stale: bool) -> None:
        self._inflight.pop(key, None)
        try:
            if not self._live(call):
                return
            outcome = self._read(call, reply, flags)
        finally:
            reply.deleteLater()

        if isinstance(outcome, ApiError):
            # A remote signer that took most of the 60 second window can
            # hand back an event that expires on the way. That is the one
            # 401 a fresh signature can cure, so it gets one, and only one.
            if (
                outcome.code == ErrorCode.UNAUTHORIZED
                and call.signed
                and stale
                and not call.resigned
            ):
                call.resigned = True
                self._sign_and_send(call)
                return
            self._fail(call, outcome)
            return
        self._succeed(call, outcome[0])

    def _read(self, call: _Call, reply, flags: dict):
        """``(value,)`` on success, else the :class:`ApiError`."""
        status = int(reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0)
        if flags["oversize"]:
            return ApiError(ErrorCode.BAD_RESPONSE, status=status or None,
                            message="the answer was larger than allowed")
        if 300 <= status < 400:
            return ApiError(ErrorCode.BAD_RESPONSE, status=status,
                            message="the server redirected the request")
        if status == 0:
            return self._transport_error(reply.error())

        raw = bytes(reply.readAll())
        if len(raw) > MAX_RESPONSE_BYTES:
            return ApiError(ErrorCode.BAD_RESPONSE, status=status,
                            message="the answer was larger than allowed")
        if not 200 <= status < 300:
            return self._http_error(status, raw, reply)
        try:
            envelope = json.loads(raw.decode("utf-8"))
            if not isinstance(envelope, dict) or "data" not in envelope:
                raise ValueError("the answer has no data")
            return (call.parse(envelope["data"]),)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            return ApiError(ErrorCode.BAD_RESPONSE, status=status,
                            message=_clean_text(str(exc)))

    @staticmethod
    def _transport_error(error) -> ApiError:
        if error in (QNetworkReply.NetworkError.TimeoutError,
                     QNetworkReply.NetworkError.OperationCanceledError):
            # Qt reports its own transfer timeout as a cancelled operation.
            # Our deliberate aborts never get here: an oversize answer is
            # caught above and a cancelled call is no longer live.
            return ApiError(ErrorCode.TIMEOUT)
        if error in (QNetworkReply.NetworkError.InsecureRedirectError,
                     QNetworkReply.NetworkError.TooManyRedirectsError):
            return ApiError(ErrorCode.BAD_RESPONSE, message="the server redirected the request")
        if error == QNetworkReply.NetworkError.NoError:
            return ApiError(ErrorCode.BAD_RESPONSE, message="the answer carried no status")
        return ApiError(ErrorCode.OFFLINE)

    def _http_error(self, status: int, raw: bytes, reply) -> ApiError:
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        message = _clean_text(body.get("message"))

        retry_after = None
        if status in (429, 503):
            retry_after = parse_retry_after(
                _reply_header(reply, "Retry-After"), now=self._clock(),
            )

        service_code = body.get("code")
        if status == 503 and service_code in ("not_configured", "upstream_refused"):
            # The service runs but holds no key, or the association refused
            # it: joining in the app isn't available, whatever the user does.
            code = ErrorCode.UNAVAILABLE
        elif status in (401, 403):
            code = ErrorCode.UNAUTHORIZED
        elif status == 404 and service_code == "not_forwarded":
            # The service's own 404: it does not forward this request at
            # all, which only a bug or a changed service explains. Not the
            # association's "nothing on record".
            code = ErrorCode.BAD_RESPONSE
        elif status == 404:
            code = ErrorCode.NOT_FOUND
        elif status == 409:
            code = ErrorCode.CONFLICT
        elif status == 422:
            return ApiError(
                ErrorCode.VALIDATION, status=status, message=message,
                field_errors=_clean_field_errors(body.get("errors")),
            )
        elif status == 429:
            code = ErrorCode.RATE_LIMITED
        elif 500 <= status < 600:
            code = ErrorCode.SERVER
        else:
            # 400, 405, 413, 415 and the rest: the request itself was
            # wrong, which only a bug or a changed server explains. There
            # is nothing the user could fix.
            code = ErrorCode.BAD_RESPONSE
        return ApiError(code, status=status, message=message, retry_after=retry_after)

    # -- delivery ----------------------------------------------------------

    def _succeed(self, call: _Call, value: Any) -> None:
        if not self._live(call):
            return
        call.done = True
        try:
            call.on_success(value)
        except Exception:  # noqa: BLE001, a caller's bug must not unwind into Qt
            traceback.print_exc()

    def _fail(self, call: _Call, error: ApiError) -> None:
        if not self._live(call):
            return
        call.done = True
        try:
            call.on_failure(error)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


# Inside MembershipApi.__init__, ``service_url`` is the argument of that
# name, which shadows the module function.
_resolve_service_url = service_url


# --------------------------------------------------------------------------- #
# Signer adapter                                                               #
# --------------------------------------------------------------------------- #

def session_signer(pool, profile, *, timeout_ms: int = SIGN_TIMEOUT_MS) -> SignFn:
    """A :data:`SignFn` over a ``BunkerSessionPool`` for ``profile``.

    Typed loosely on purpose so this module never imports the pool: it
    only needs ``pool.get(profile, on_ready, on_error)`` and the client's
    ``sign_event(unsigned, on_success, on_failure, timeout_ms=...)``.
    """

    def sign(unsigned: dict, on_success, on_failure) -> None:
        def _ready(client) -> None:
            client.sign_event(unsigned, on_success, on_failure, timeout_ms=timeout_ms)

        pool.get(profile, _ready, on_failure)

    return sign
