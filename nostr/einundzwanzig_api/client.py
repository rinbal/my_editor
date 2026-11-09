# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Qt client for the membership API, and where its requests go.

:class:`MembershipApi` signs (through a plain callable, see
:func:`session_signer`) and sends every call to the membership service
(:func:`service_url`), naming the association (:func:`upstream_url`) in
each signature. The package docstring has the reasoning. The call
pipeline itself (sign, send, read, one callback) is shared with the app's
other signed APIs: ``nostr/signed_api.py``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

import url_safety

from ..signed_api import Call as _Call
from ..http_answers import clean_text, parse_retry_after
from ..signed_api import SignedJsonApi, SignFn, json_body, reply_header, usable_base_url
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
    parse_config,
    parse_erasure,
    parse_export,
    parse_invoice,
    parse_membership,
    parse_payments,
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


def _usable_service(value: Any) -> str:
    """``value`` as an address this app may send signed requests to,
    without a trailing slash, or ``""``: https to any host, plain http
    only to exactly ``localhost``, ``127.0.0.1`` or ``[::1]``, for
    development (``signed_api.usable_base_url`` has the rules)."""
    return usable_base_url(value, http_hosts=_LOOPBACK_HOSTS)


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
# The client                                                                   #
# --------------------------------------------------------------------------- #

class _Unset:
    """Marks an application field the caller did not pass."""

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()


def _year_segment(year: int) -> str:
    if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9999:
        raise ValueError("year must be a four-digit integer")
    return str(year)


class MembershipApi(SignedJsonApi):
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

    network_timeout_ms = NETWORK_TIMEOUT_MS
    stale_signature_seconds = STALE_SIGNATURE_SECONDS
    max_response_bytes = MAX_RESPONSE_BYTES
    max_redirects = _MAX_REDIRECTS
    user_agent = _USER_AGENT

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
        base = str(base_url).rstrip("/") if base_url is not None else upstream_url()
        if not url_safety.origin_of(base):
            raise ValueError("base_url must be an absolute http(s) URL")
        super().__init__(sign, nam=nam, clock=clock, parent=parent)
        self._service_url = (_usable_service(service_url) if service_url is not None
                             else _resolve_service_url())
        self._base_url = base

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
        call = self._new_call("POST", "/applications", json_body(payload), True,
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
        body = json_body({"return_url": return_url}) if return_url else None
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

    # -- what the shared pipeline asks (nostr/signed_api.py) -------------

    def _error(self, code: str, **fields) -> ApiError:
        return ApiError(code, **fields)

    def _unavailable(self, call: _Call) -> Optional[ApiError]:
        return None if self._service_url else ApiError(ErrorCode.UNAVAILABLE)

    def _retry(self, call: _Call, error: ApiError, stale: bool) -> bool:
        # A remote signer that took most of the 60 second window can
        # hand back an event that expires on the way. That is the one
        # 401 a fresh signature can cure, so it gets one, and only one.
        if (error.code == ErrorCode.UNAUTHORIZED and call.signed and stale
                and not call.resigned):
            call.resigned = True
            self._sign_and_send(call)
            return True
        return False

    def _parse_success(self, call: _Call, status: int, raw: bytes):
        try:
            envelope = json.loads(raw.decode("utf-8"))
            if not isinstance(envelope, dict) or "data" not in envelope:
                raise ValueError("the answer has no data")
            return (call.parse(envelope["data"]),)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            return ApiError(ErrorCode.BAD_RESPONSE, status=status,
                            message=clean_text(str(exc)))

    def _http_error(self, status: int, raw: bytes, reply) -> ApiError:
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        message = clean_text(body.get("message"))

        retry_after = None
        if status in (429, 503):
            retry_after = parse_retry_after(
                reply_header(reply, "Retry-After"), now=self._clock(),
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
