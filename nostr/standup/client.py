# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Qt client for a STANDUP submission server (editorial contract 12).

:class:`StandupApi` runs on the call pipeline every signed API of the app
shares (``nostr/signed_api.py``: a fresh NIP-98 signature right before
sending, the exact bytes hashed and sent, a reply read within a limit,
exactly one callback). What is STANDUP's own:

- **Where requests go.** One client per server: the address a verified
  key event names (``server``, contract 8.1). Only https, except plain
  http to exactly ``127.0.0.1`` when a developer allows it
  (``MYEDITOR_STANDUP_LOCAL_HTTP=1``). No address is built in: the
  association runs the instance and names it, so without one nothing is
  sent (:func:`configured_server` reads ``MYEDITOR_STANDUP_SERVER``).
- **Three ways to authorize** (12.1). A signed action is a NIP-98 request
  that always carries a ``payload`` tag (contract 10), so its body is
  never empty, ``{}`` at least. Reading what needs a person goes with a
  session token (``POST /api/session`` once, then ``Bearer``), so
  following a submission does not prompt the signer app for every read;
  an expired or unknown token is replaced once, by itself. The status
  key lookup and the public endpoints carry nothing.
- **Errors** are :class:`StandupError`, with the contract's codes (12.2)
  where the server gave one, and ``maybe_stored`` when the request may
  have arrived although no answer did, the case contract 12.4 resolves
  with a lookup ("No answer after sending").
- **Receipts** are compared with what was sent before anything else sees
  them (:func:`~.views.check_receipt`): :meth:`StandupApi.submit` answers
  Confirmed, BlobsMissing or Mismatch, never a bare "ok".

The token lives in memory only, for this client: it is never written
anywhere, and :meth:`StandupApi.stop` (sign-out, another account)
forgets it.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply

from ..einundzwanzig_api.models import _clean_text, parse_retry_after
from ..signed_api import (
    BAD_RESPONSE,
    OFFLINE,
    SIGNER_DECLINED,
    SIGNER_UNREACHABLE,
    TIMEOUT,
    Call,
    SignedJsonApi,
    SignFn,
    json_body,
    reply_header,
    signer_failure_code,
    usable_base_url,
)
from . import formats, views
from .formats import FormatError

# --------------------------------------------------------------------------- #
# Where requests go                                                            #
# --------------------------------------------------------------------------- #

# A developer's own server (a local STANDUP). Unset in production, where
# the server comes from the publication's verified key event.
ENV_SERVER: str = "MYEDITOR_STANDUP_SERVER"
# "1" allows plain http to exactly 127.0.0.1, for a local STANDUP.
ENV_LOCAL_HTTP: str = "MYEDITOR_STANDUP_LOCAL_HTTP"
_LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1"})


def local_http_allowed() -> bool:
    return os.environ.get(ENV_LOCAL_HTTP, "").strip() == "1"


def usable_server_url(value: Any, *, local_http: Optional[bool] = None) -> str:
    """``value`` as a submission server's address, without a trailing
    slash, or ``""``: https to any host; plain http only to exactly
    ``127.0.0.1``, and only with ``local_http`` (default: the developer
    setting). No user, password, query or fragment."""
    allowed = local_http_allowed() if local_http is None else local_http
    return usable_base_url(value, http_hosts=_LOCAL_HTTP_HOSTS if allowed else frozenset())


def configured_server() -> str:
    """The server a developer named, or ``""``. Never a built-in address:
    the association names its instance (the UI says "kommt vom Verein")."""
    return usable_server_url(os.environ.get(ENV_SERVER, ""))


# --------------------------------------------------------------------------- #
# Errors (12.2)                                                                #
# --------------------------------------------------------------------------- #

class Code:
    """Error codes: the contract's (12.2) and the app's own. Plain strings."""

    # The app's own.
    UNAVAILABLE = "unavailable"            # no usable server address
    OFFLINE = OFFLINE
    TIMEOUT = TIMEOUT
    BAD_RESPONSE = BAD_RESPONSE
    SIGNER_DECLINED = SIGNER_DECLINED
    SIGNER_UNREACHABLE = SIGNER_UNREACHABLE
    SERVER = "server"                      # 5xx
    REFUSED = "refused"                    # refused here, before sending
    # The contract's.
    BAD_REQUEST = "bad_request"
    AUTH_REQUIRED = "auth_required"
    SESSION_EXPIRED = "session_expired"
    AUTH_INVALID = "auth_invalid"
    FORBIDDEN = "forbidden"
    PAUSED = "paused"
    BLOCKED = "blocked"
    LIMIT_OPEN = "limit_open"
    NOT_MEMBER = "not_member"
    ROLE = "role"
    NOT_FOUND = "not_found"
    STALE_REVISION = "stale_revision"
    INVALID_TRANSITION = "invalid_transition"
    CONFLICT = "conflict"
    EPOCH = "epoch"
    TOO_LARGE = "too_large"
    INVALID = "invalid"
    KEY_EVENT_INVALID = "key_event_invalid"
    SETTINGS_INVALID = "settings_invalid"
    WRAP_INVALID = "wrap_invalid"
    RATE_LIMITED = "rate_limited"


# 12.2: the codes each status may carry; the first is the fallback for a
# code this app does not know.
_CODES_BY_STATUS = {
    400: ("bad_request",),
    401: ("auth_invalid", "auth_required", "session_expired"),
    403: ("forbidden", "paused", "blocked", "limit_open", "not_member", "role"),
    404: ("not_found",),
    409: ("conflict", "stale_revision", "invalid_transition", "epoch"),
    413: ("too_large",),
    422: ("invalid", "key_event_invalid", "settings_invalid", "wrap_invalid"),
    429: ("rate_limited",),
}

# Transport failures that happen before a request leaves this computer:
# whatever it was, the server never saw it.
_NOT_SENT = frozenset({
    QNetworkReply.NetworkError.ConnectionRefusedError,
    QNetworkReply.NetworkError.HostNotFoundError,
    QNetworkReply.NetworkError.SslHandshakeFailedError,
    QNetworkReply.NetworkError.NetworkSessionFailedError,
    QNetworkReply.NetworkError.TemporaryNetworkFailureError,
    QNetworkReply.NetworkError.ProxyConnectionRefusedError,
    QNetworkReply.NetworkError.ProxyNotFoundError,
    QNetworkReply.NetworkError.ProxyAuthenticationRequiredError,
    QNetworkReply.NetworkError.UnknownProxyError,
    QNetworkReply.NetworkError.ProtocolUnknownError,
})

_MAX_DETAIL_KEYS = 20


@dataclass(frozen=True)
class StandupError:
    """One failure. ``message`` is the server's own text (German) or a
    short reason of the app's, never shown raw; ``detail`` is the
    server's detail, cleaned; ``maybe_stored`` says the request may have
    reached the server although no answer came back, which for a
    submission means: look it up (12.4), do not tell the person it
    failed."""

    code: str
    status: Optional[int] = None
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    retry_after: Optional[int] = None
    maybe_stored: bool = False

    @property
    def reason(self) -> str:
        value = self.detail.get("reason")
        return value if isinstance(value, str) else ""


def _clean_detail(value: Any) -> Dict[str, Any]:
    """The server's ``detail``, flat: words, numbers, true/false, null,
    and lists of words, each cleaned and capped."""
    if not isinstance(value, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key, item in list(value.items())[:_MAX_DETAIL_KEYS]:
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            cleaned[_clean_text(key)] = _clean_text(item)
        elif item is None or isinstance(item, bool) or formats.is_int(item, low=-(2 ** 53)):
            cleaned[_clean_text(key)] = item
        elif isinstance(item, list) and all(isinstance(x, str) for x in item):
            cleaned[_clean_text(key)] = tuple(_clean_text(x) for x in item[:64])
    return cleaned


# --------------------------------------------------------------------------- #
# What some answers are                                                        #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Session:
    token: str = field(repr=False)
    pubkey: str
    expires_at: int


@dataclass(frozen=True)
class Instance:
    """``GET /api/editorial/instance``. ``server`` is None when the
    operator has not set the address explicitly (14): never guessed."""

    operator: str
    server: Optional[str]
    web_app_by: str
    bote: Optional[Mapping[str, Any]]
    inbox_relays: Tuple[str, ...]


@dataclass(frozen=True)
class SubmissionDetail:
    """``GET /api/submissions/<sid>``."""

    submission: views.SubmissionView
    my_role: str
    revisions: Tuple[views.RevisionRecord, ...]
    timeline: Tuple[views.TimelineItem, ...]
    members: Tuple[Mapping[str, Any], ...]


# Answers larger than this are refused before they are read whole.
MAX_ANSWER_BYTES = 1024 * 1024
# A lookup answers up to 50 submissions with their new conversation
# items, each up to 96 KiB.
MAX_LOOKUP_ANSWER_BYTES = 16 * 1024 * 1024
# A submission's detail holds every revision's envelope (4 MiB each).
MAX_DETAIL_ANSWER_BYTES = 64 * 1024 * 1024
MAX_DESTINATIONS_ANSWER_BYTES = 4 * 1024 * 1024
# A session is replaced this long before the server would refuse it.
SESSION_MARGIN_SECONDS = 60

# How a call is authorized.
NONE = "none"
SIGNED = "signed"
SESSION = "session"


def _refused(message: str) -> StandupError:
    return StandupError(Code.REFUSED, message=message)


def _object(data: Mapping[str, Any]) -> Dict[str, Any]:
    """An answer passed on as it came, without the ``ok``."""
    return {key: value for key, value in data.items() if key != "ok"}


# --------------------------------------------------------------------------- #
# The client                                                                   #
# --------------------------------------------------------------------------- #

class StandupApi(SignedJsonApi):
    """One STANDUP server, for one account (the ``sign`` callable signs
    as that account). Every method takes ``on_success`` and
    ``on_failure``; exactly one fires, never inside a Qt slot's raise.

    Seams: ``sign``, ``nam``, ``clock``; ``local_http`` overrides the
    developer setting (tests).
    """

    max_response_bytes = MAX_ANSWER_BYTES

    def __init__(self, sign: SignFn, *, server: str, nam: Optional[QNetworkAccessManager] = None,
                 clock: Optional[Callable[[], float]] = None, local_http: Optional[bool] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(sign, nam=nam, clock=clock, parent=parent)
        self._server = usable_server_url(server, local_http=local_http)
        self._session: Optional[Session] = None
        self._session_call: Optional[Call] = None
        self._waiting: List[Call] = []

    @property
    def server(self) -> str:
        return self._server

    def configured(self) -> bool:
        return bool(self._server)

    def url_for(self, path: str) -> str:
        return self._server + path

    # -- account scope -------------------------------------------------------

    def cancel(self) -> None:
        super().cancel()
        self._waiting.clear()
        self._session_call = None

    def stop(self) -> None:
        """Sign-out or another account: drop every call, forget the token."""
        self.cancel()
        self._session = None

    @property
    def has_session(self) -> bool:
        return self._session_usable()

    # -- the pipeline's questions -------------------------------------------

    def _error(self, code: str, **fields) -> StandupError:
        return StandupError(code, **fields)

    def _signer_error(self, reason: str) -> StandupError:
        return StandupError(signer_failure_code(reason), message=_clean_text(reason))

    def _unavailable(self, call: Call) -> Optional[StandupError]:
        return None if self._server else StandupError(Code.UNAVAILABLE)

    def _prepare_request(self, call: Call, request) -> None:
        if call.extra.get("auth") != SESSION:
            return
        token = call.extra.get("bearer") or (self._session.token if self._session else "")
        if token:
            request.setRawHeader(b"Authorization", b"Bearer " + token.encode("ascii"))

    def _transport_error(self, error) -> StandupError:
        base = super()._transport_error(error)
        return StandupError(base.code, status=base.status, message=base.message,
                            maybe_stored=error not in _NOT_SENT)

    def _read(self, call: Call, reply, flags: dict, limit: int):
        outcome = super()._read(call, reply, flags, limit)
        if isinstance(outcome, StandupError) and not outcome.maybe_stored:
            status = outcome.status or 0
            # An answer that came but could not be used (2xx unreadable,
            # too large, a 5xx from a proxy behind which the server may
            # have stored it): the request arrived.
            if (200 <= status < 300 or status >= 500
                    or (outcome.code == Code.BAD_RESPONSE and status == 0)):
                outcome = StandupError(outcome.code, status=outcome.status,
                                       message=outcome.message, detail=outcome.detail,
                                       retry_after=outcome.retry_after, maybe_stored=True)
        return outcome

    def _parse_success(self, call: Call, status: int, raw: bytes):
        if call.extra.get("nip05"):
            return self._parse_nip05(call, status, raw)
        try:
            data = formats.strict_json(raw, limit=len(raw))
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise FormatError("the answer is not a success")
            call.extra["status"] = status
            return (call.parse(data),)
        except (FormatError, ValueError, TypeError, KeyError, AttributeError,
                RecursionError) as exc:
            return StandupError(Code.BAD_RESPONSE, status=status,
                                message=_clean_text(str(exc)), maybe_stored=True)

    def _http_error(self, status: int, raw: bytes, reply) -> StandupError:
        try:
            body = formats.strict_json(raw, limit=len(raw)) if raw else {}
        except FormatError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            error = {}
        retry_after = None
        if status in (429, 503):
            retry_after = parse_retry_after(reply_header(reply, "Retry-After"),
                                            now=self._clock())
        server_code = error.get("code")
        allowed = _CODES_BY_STATUS.get(status)
        if allowed is not None:
            code = server_code if server_code in allowed else allowed[0]
        elif 500 <= status < 600:
            code = Code.SERVER
        else:
            # 405, 415 and the rest: only a bug or a changed server.
            code = Code.BAD_RESPONSE
        return StandupError(code, status=status, message=_clean_text(error.get("message")),
                            detail=_clean_detail(error.get("detail")),
                            retry_after=retry_after, maybe_stored=status >= 500)

    def _retry(self, call: Call, error: StandupError, stale: bool) -> bool:
        auth = call.extra.get("auth")
        if auth == SESSION and not call.extra.get("session_retried") and (
                error.code == Code.SESSION_EXPIRED
                or (error.code == Code.AUTH_INVALID and error.reason == "session")):
            # The token ran out or the server no longer knows it: one new
            # session, then the same call again.
            call.extra["session_retried"] = True
            self._session = None
            self._with_session(call)
            return True
        if (auth == SIGNED and stale and not call.resigned
                and error.code == Code.AUTH_INVALID):
            # A signature that took most of its 60 seconds on the phone
            # and expired on the way: signed once more, once.
            call.resigned = True
            self._sign_and_send(call)
            return True
        return False

    # -- calls ---------------------------------------------------------------

    def _call(self, method: str, path: str, payload: Optional[Mapping[str, Any]], auth: str,
              parse: Callable[[Mapping[str, Any]], Any], on_success, on_failure, *,
              limit: Optional[int] = None) -> Call:
        if auth == SIGNED and payload is None:
            payload = {}   # contract 10: a signed action always has a payload tag
        body = None if payload is None else json_body(payload)
        call = self._new_call(method, path, body, auth == SIGNED, parse, on_success, on_failure)
        call.extra["auth"] = auth
        if limit is not None:
            call.extra["max_response_bytes"] = limit
        if auth == SESSION:
            problem = self._unavailable(call)
            if problem is not None:
                self._fail(call, problem)
            elif self._session_usable():
                self._send(call, None)
            else:
                self._with_session(call)
        else:
            self._run(call)
        return call

    def _session_usable(self) -> bool:
        return (self._session is not None
                and self._session.expires_at - SESSION_MARGIN_SECONDS > self._clock())

    def _with_session(self, call: Call) -> None:
        """Send ``call`` once a session is open; one opens at a time."""
        self._waiting.append(call)
        if self._session_call is not None and self._live(self._session_call):
            return

        def opened(session: Session) -> None:
            self._session_call = None
            waiting, self._waiting = self._waiting, []
            for item in waiting:
                if self._live(item):
                    self._send(item, None)

        def failed(error: StandupError) -> None:
            self._session_call = None
            waiting, self._waiting = self._waiting, []
            for item in waiting:
                self._fail(item, error)

        self._session_call = self.open_session(opened, failed)

    def open_session(self, on_success: Callable[[Session], None],
                     on_failure: Callable[[StandupError], None]) -> Call:
        """``POST /api/session``: one signature, a 12-hour token (12.1)."""
        holder: Dict[str, Call] = {}

        def parse(data: Mapping[str, Any]) -> Session:
            token = data.get("token")
            pubkey = data.get("pubkey")
            expires_at = data.get("expires_at")
            if not (isinstance(token, str) and formats.is_hex64(token)):
                raise FormatError("the session token is malformed")
            signed_by = holder["call"].extra.get("signed_by")
            if not (formats.is_hex64(pubkey) and isinstance(signed_by, str)
                    and hmac.compare_digest(pubkey, signed_by)):
                # A token for someone else would act as someone else.
                raise FormatError("the session is for another person")
            if not formats.is_int(expires_at) or expires_at <= self._clock():
                raise FormatError("the session has expired already")
            session = Session(token=token, pubkey=pubkey, expires_at=expires_at)
            self._session = session
            return session

        holder["call"] = self._call("POST", "/api/session", {}, SIGNED, parse,
                                    on_success, on_failure)
        return holder["call"]

    def close_session(self, on_done: Callable[[], None]) -> None:
        """``DELETE /api/session``: the token is forgotten here at once,
        and the server is asked to revoke it; ``on_done`` either way."""
        session, self._session = self._session, None
        if session is None or not self._server:
            on_done()
            return
        call = self._new_call("DELETE", "/api/session", None, False, lambda d: None,
                              lambda _v: on_done(), lambda _e: on_done())
        call.extra["auth"] = SESSION
        call.extra["bearer"] = session.token
        self._send(call, None)

    # -- instance and publications (12.3) ------------------------------------

    def instance(self, on_success: Callable[[Instance], None], on_failure) -> Call:
        def parse(data):
            bote = data.get("bote")
            if bote is not None:
                if not (isinstance(bote, dict) and formats.is_pubkey(bote.get("pubkey"))):
                    raise FormatError("the notice account is malformed")
                bote = {"pubkey": bote["pubkey"],
                        "npub": _clean_text(bote.get("npub")),
                        "nip05": _clean_text(bote.get("nip05")) or None}
            relays = data.get("inbox_relays") or []
            if not isinstance(relays, list):
                raise FormatError("the inbox relays must be a list")
            server = data.get("server")
            return Instance(operator=_clean_text(data.get("operator")),
                            server=usable_server_url(server) or None,
                            web_app_by=_clean_text(data.get("web_app_by")),
                            bote=bote,
                            inbox_relays=tuple(r for r in relays if isinstance(r, str))[:20])

        return self._call("GET", "/api/editorial/instance", None, NONE, parse,
                          on_success, on_failure)

    def destinations(self, on_success: Callable[[List[Mapping[str, Any]]], None],
                     on_failure) -> Call:
        """``GET /api/submissions/destinations``: the open publications as
        the server lists them. Nothing here is trusted yet: each key event
        is verified by ``trust``, and the rules that count are the ones in
        it, signed by the publication."""
        def parse(data):
            items = data.get("publications")
            if not isinstance(items, list):
                raise FormatError("the publications must be a list")
            found = []
            for item in items[:500]:
                if not isinstance(item, dict) or not isinstance(item.get("key_event"), dict):
                    continue
                try:
                    formats.parse_address(item.get("address"))
                except FormatError:
                    continue
                picture = item.get("picture")
                found.append({"address": item["address"],
                              "name": _clean_text(item.get("name")),
                              "picture": picture if isinstance(picture, str)
                              and picture.startswith("https://") else None,
                              "key_event": item["key_event"]})
            return found

        return self._call("GET", "/api/submissions/destinations", None, NONE, parse,
                          on_success, on_failure, limit=MAX_DESTINATIONS_ANSWER_BYTES)

    @staticmethod
    def _site(address: str) -> str:
        pubkey, site_d = formats.parse_address(address)
        return "/api/sites/" + formats.route_key(pubkey, site_d)

    def site_settings(self, address: str, on_success, on_failure) -> Call:
        """``GET .../settings``: ``{"key_event", "settings_event", "members_count"}``."""
        def parse(data):
            if not isinstance(data.get("key_event"), dict):
                raise FormatError("no key event")
            return _object(data)

        return self._call("GET", self._site(address) + "/settings", None, NONE, parse,
                          on_success, on_failure)

    def put_site_settings(self, address: str, *, key_event: Optional[dict],
                          settings_event: Optional[dict], on_success, on_failure) -> Call:
        return self._call("PUT", self._site(address) + "/settings",
                          {"key_event": key_event, "settings_event": settings_event},
                          SIGNED, _object, on_success, on_failure)

    def members(self, address: str, on_success, on_failure) -> Call:
        def parse(data):
            if not isinstance(data.get("members"), list):
                raise FormatError("the members must be a list")
            return [m for m in data["members"] if isinstance(m, dict)]

        return self._call("GET", self._site(address) + "/members", None, SESSION, parse,
                          on_success, on_failure)

    def invite_member(self, address: str, *, pubkey: str, role: str, sees: str,
                      wraps: Sequence[Mapping[str, Any]], history: Optional[Mapping[str, Any]],
                      on_success, on_failure) -> Call:
        return self._call("POST", self._site(address) + "/members",
                          {"pubkey": pubkey, "role": role, "sees": sees, "wraps": list(wraps),
                           "history": history}, SIGNED, _object, on_success, on_failure)

    def accept_membership(self, address: str, on_success, on_failure) -> Call:
        return self._call("POST", self._site(address) + "/members/accept", {}, SIGNED,
                          _object, on_success, on_failure)

    def remove_member(self, address: str, *, pubkey: str, epoch: Mapping[str, Any],
                      on_success, on_failure) -> Call:
        return self._call("POST", self._site(address) + "/members/remove",
                          {"pubkey": pubkey, "epoch": epoch}, SIGNED, _object,
                          on_success, on_failure)

    def update_my_membership(self, address: str, changes: Mapping[str, Any],
                             on_success, on_failure) -> Call:
        allowed = {"away_until", "hand_back", "notices"}
        return self._call("PATCH", self._site(address) + "/members/me",
                          {k: v for k, v in changes.items() if k in allowed}, SIGNED, _object,
                          on_success, on_failure)

    def new_epoch(self, address: str, epoch: Mapping[str, Any], on_success, on_failure) -> Call:
        return self._call("POST", self._site(address) + "/epochs", dict(epoch), SIGNED,
                          _object, on_success, on_failure)

    def keys(self, address: str, on_success, on_failure) -> Call:
        return self._call("GET", self._site(address) + "/keys", None, SESSION, _object,
                          on_success, on_failure)

    def keyring(self, on_success, on_failure) -> Call:
        return self._call("GET", "/api/me/keyring", None, SESSION, _object,
                          on_success, on_failure, limit=MAX_LOOKUP_ANSWER_BYTES)

    def put_keyring(self, *, expected_version: int, pages: Sequence[str],
                    on_success, on_failure) -> Call:
        return self._call("PUT", "/api/me/keyring",
                          {"expected_version": expected_version, "pages": list(pages)},
                          SIGNED, _object, on_success, on_failure)

    def bote_pubkey(self, on_success: Callable[[str], None], on_failure) -> Call:
        """``/.well-known/nostr.json?name=bote``: the notice account's key."""
        call = self._new_call("GET", "/.well-known/nostr.json?name=bote", None, False,
                              None, on_success, on_failure)
        call.extra["auth"] = NONE
        call.extra["nip05"] = True
        self._run(call)
        return call

    # -- submissions (12.4) --------------------------------------------------

    def submit(self, body: Mapping[str, Any], on_success: Callable[[Any], None],
               on_failure: Callable[[StandupError], None]) -> Call:
        """``POST /api/submissions`` with ``body`` (see :func:`submission_body`).

        ``on_success`` gets :class:`~.views.Confirmed`, :class:`~.views.BlobsMissing`
        or :class:`~.views.Mismatch`. Sending the same body again (after
        uploading missing files, or after no answer) is safe: the server
        answers the stored receipt (criterion 5).
        """
        problem = submission_body_problem(body)
        if problem:
            call = self._new_call("POST", "/api/submissions", None, True, lambda d: None,
                                  on_success, on_failure)
            self._fail(call, _refused(problem))
            return call
        holder: Dict[str, Call] = {}

        def parse(data):
            status = holder["call"].extra.get("status")
            return views.check_receipt(body, data.get("receipt"), now=self._clock(),
                                       repeat=status == 200)

        holder["call"] = self._call("POST", "/api/submissions", dict(body), SIGNED, parse,
                                    on_success, on_failure)
        return holder["call"]

    def lookup(self, items: Sequence[Tuple[str, int]],
               on_success: Callable[[Dict[str, views.LookupItem]], None],
               on_failure) -> Call:
        """``POST /api/submissions/lookup`` with ``(status_key, since)``
        pairs, at most 50, and no signature: ``on_success`` gets the
        answers by status key (a key the server does not know is simply
        not there). Internal notes are dropped whatever the server sends."""
        items = list(items)
        problem = None
        if not items or len(items) > formats.MAX_LOOKUP_ITEMS:
            problem = f"a lookup asks for 1 to {formats.MAX_LOOKUP_ITEMS} submissions"
        elif not all(formats.is_hex64(key) and formats.is_int(since) for key, since in items):
            problem = "a status key is malformed"
        if problem:
            call = self._new_call("POST", "/api/submissions/lookup", None, False,
                                  lambda d: None, on_success, on_failure)
            self._fail(call, _refused(problem))
            return call
        by_hash = {formats.status_key_hash(key): key for key, _since in items}

        def parse(data):
            answers = data.get("items")
            if not isinstance(answers, list):
                raise FormatError("the lookup answer must be a list")
            found: Dict[str, views.LookupItem] = {}
            for raw in answers:
                try:
                    item = views.parse_lookup_item(raw)
                except FormatError:
                    continue      # one broken item does not hide the others
                key = next((k for h, k in by_hash.items()
                            if hmac.compare_digest(h, item.status_key_hash)), None)
                if key is not None:
                    found[key] = item
            return found

        return self._call("POST", "/api/submissions/lookup",
                          {"items": [{"status_key": k, "since": s} for k, s in items]},
                          NONE, parse, on_success, on_failure, limit=MAX_LOOKUP_ANSWER_BYTES)

    def submission(self, submission_id: str, on_success: Callable[[SubmissionDetail], None],
                   on_failure, *, since: Optional[int] = None) -> Call:
        """``GET /api/submissions/<sid>?since=seq`` with the session."""
        path = f"/api/submissions/{_sid(submission_id)}"
        if since is not None:
            path += f"?since={int(since)}"

        def parse(data):
            role = data.get("my_role")
            if role not in views.MEMBER_ROLES + views.PARTICIPANT_ROLES:
                raise FormatError("the answer names no role")
            revisions = data.get("revisions") or []
            members = data.get("members") or []
            if not isinstance(revisions, list) or not isinstance(members, list):
                raise FormatError("revisions and members must be lists")
            return SubmissionDetail(
                submission=views.parse_view(data.get("submission")),
                my_role=role,
                revisions=tuple(views.parse_revision_record(r) for r in revisions),
                timeline=views.parse_timeline(data.get("timeline") or [],
                                              internal=role in views.MEMBER_ROLES),
                members=tuple(m for m in members if isinstance(m, dict)))

        return self._call("GET", path, None, SESSION, parse, on_success, on_failure,
                          limit=MAX_DETAIL_ANSWER_BYTES)

    def send_message(self, submission_id: str, wrap: Mapping[str, Any], *, kind_hint: str,
                     on_success: Callable[[int], None], on_failure,
                     channel: str = "conversation") -> Call:
        """``POST .../messages`` with the session: the gift wrap and what
        kind of item it is (``kind_hint``, so a reaction sends no notice).
        ``on_success(seq)``: only a ``seq`` means the server has it."""
        path = f"/api/submissions/{_sid(submission_id)}/messages"
        problem = None
        if kind_hint not in views.KIND_HINTS:
            problem = "unknown kind of message"
        elif channel not in ("conversation", "internal"):
            problem = "unknown channel"
        elif not formats.is_signed_event(wrap, kind=1059):
            problem = "not a gift wrap"
        elif len(formats.canonical_bytes(wrap)) > formats.MAX_WRAP_EVENT_BYTES:
            problem = "the wrapped message is larger than the server takes"
        if problem:
            call = self._new_call("POST", path, None, False, lambda d: None,
                                  on_success, on_failure)
            self._fail(call, _refused(problem))
            return call
        return self._call("POST", path, {"channel": channel, "wrap": dict(wrap),
                                         "kind_hint": kind_hint},
                          SESSION, _seq, on_success, on_failure)

    def _action(self, submission_id: str, name: str, payload: Mapping[str, Any], parse,
                on_success, on_failure) -> Call:
        sid = _sid(submission_id)
        body = {"submission_id": sid}
        body.update(payload)
        return self._call("POST", f"/api/submissions/{sid}/{name}", body, SIGNED, parse,
                          on_success, on_failure)

    def decision(self, submission_id: str, *, outcome: str, expected_revision: int,
                 message_wrap: Optional[dict] = None, split_wrap: Optional[dict] = None,
                 split_rumor_id: Optional[str] = None, section: Optional[str] = None,
                 on_success, on_failure) -> Call:
        return self._action(submission_id, "decision", {
            "outcome": outcome, "expected_revision": expected_revision,
            "message_wrap": message_wrap, "split_wrap": split_wrap,
            "split_rumor_id": split_rumor_id, "section": section,
        }, _view_and_seq, on_success, on_failure)

    def confirm(self, submission_id: str, *, revision_hash: str, confirms: Sequence[str],
                split_rumor_id: Optional[str] = None, on_success, on_failure) -> Call:
        return self._action(submission_id, "confirmation", {
            "revision_hash": revision_hash, "split_rumor_id": split_rumor_id,
            "confirms": list(confirms)}, _view, on_success, on_failure)

    def responsible(self, submission_id: str, *, action: str, to: Optional[str] = None,
                    on_success, on_failure) -> Call:
        return self._action(submission_id, "responsible", {"action": action, "to": to},
                            _view, on_success, on_failure)

    def withdraw(self, submission_id: str, on_success, on_failure) -> Call:
        return self._action(submission_id, "withdraw", {}, _view, on_success, on_failure)

    def published(self, submission_id: str, *, naddr: str, event: dict,
                  on_success, on_failure) -> Call:
        return self._action(submission_id, "published", {"naddr": naddr, "event": event},
                            _view, on_success, on_failure)

    def placed(self, submission_id: str, *, site_event: dict, section: Optional[str] = None,
               on_success, on_failure) -> Call:
        return self._action(submission_id, "placed",
                            {"site_event": site_event, "section": section},
                            _view, on_success, on_failure)

    def correction(self, submission_id: str, revision: Mapping[str, Any],
                   on_success, on_failure) -> Call:
        keys = ("revision", "revision_hash", "envelope", "envelope_sha256", "wraps", "blobs")
        return self._action(submission_id, "corrections",
                            {k: revision[k] for k in keys if k in revision},
                            _view_and_seq, on_success, on_failure)

    def remind(self, submission_id: str, *, to: str, on_success, on_failure) -> Call:
        return self._action(submission_id, "remind", {"to": to}, _object,
                            on_success, on_failure)

    def request_reupload(self, submission_id: str, *, sha256: str,
                         on_success, on_failure) -> Call:
        return self._action(submission_id, "reupload", {"sha256": sha256}, _object,
                            on_success, on_failure)

    def propose(self, address: str, *, naddr: str, event: dict, p_sub: str,
                wraps: Mapping[str, Any], title: str, on_success, on_failure) -> Call:
        return self._call("POST", self._site(address) + "/proposals",
                          {"naddr": naddr, "event": event, "p_sub": p_sub,
                           "wraps": dict(wraps), "title": title},
                          SIGNED, _view, on_success, on_failure)

    def site_submissions(self, address: str, *, view: str = "mine",
                         cursor: Optional[str] = None, q: Optional[str] = None,
                         on_success, on_failure) -> Call:
        from urllib.parse import urlencode
        query = {"view": view}
        if cursor:
            query["cursor"] = cursor
        if q:
            query["q"] = q

        def parse(data):
            items = data.get("items")
            if not isinstance(items, list):
                raise FormatError("the items must be a list")
            return {"items": [views.parse_view(i) for i in items], "next": data.get("next")}

        return self._call("GET", self._site(address) + "/submissions?" + urlencode(query),
                          None, SESSION, parse, on_success, on_failure,
                          limit=MAX_DESTINATIONS_ANSWER_BYTES)

    def my_editorial(self, on_success, on_failure) -> Call:
        def parse(data):
            turns = data.get("turns")
            if not isinstance(turns, list):
                raise FormatError("the turns must be a list")
            return {"turns": [views.parse_view(i) for i in turns],
                    "counts": data.get("counts") if isinstance(data.get("counts"), dict) else {},
                    "publications": [p for p in data.get("publications") or []
                                     if isinstance(p, dict)]}

        return self._call("GET", "/api/me/editorial", None, SESSION, parse,
                          on_success, on_failure, limit=MAX_DESTINATIONS_ANSWER_BYTES)

    def my_submissions(self, on_success: Callable[[List[views.SubmissionView]], None],
                       on_failure) -> Call:
        def parse(data):
            items = data.get("items")
            if not isinstance(items, list):
                raise FormatError("the items must be a list")
            return [views.parse_view(i) for i in items]

        return self._call("GET", "/api/me/submissions", None, SESSION, parse,
                          on_success, on_failure, limit=MAX_DESTINATIONS_ANSWER_BYTES)

    def _parse_nip05(self, call: Call, status: int, raw: bytes):
        """The well-known answer is not an editorial one (no ``ok``)."""
        try:
            data = formats.strict_json(raw, limit=len(raw))
            pubkey = data["names"]["bote"]
            if not formats.is_pubkey(pubkey):
                raise FormatError("the notice account's key is malformed")
            return (pubkey,)
        except (FormatError, KeyError, TypeError) as exc:
            return StandupError(Code.BAD_RESPONSE, status=status, message=_clean_text(str(exc)))


def _sid(value: str) -> str:
    if not formats.is_hex64(value):
        raise ValueError("a submission id is 64 hex digits")
    return value


def _seq(data: Mapping[str, Any]) -> int:
    seq = data.get("seq")
    if not formats.is_int(seq, low=1):
        raise FormatError("the answer has no seq")
    return seq


def _view(data: Mapping[str, Any]) -> views.SubmissionView:
    return views.parse_view(data.get("submission"))


def _view_and_seq(data: Mapping[str, Any]) -> Tuple[views.SubmissionView, int]:
    return views.parse_view(data.get("submission")), _seq(data)


# --------------------------------------------------------------------------- #
# The submission body (12.4)                                                   #
# --------------------------------------------------------------------------- #

def submission_body(*, writer: str, publication: str, key_event_id: str, epoch: int,
                    d: str, round_: int, revision: int, revision_hash: str, kind: str,
                    title: str, signer: str, participants: Sequence[str], p_sub: str,
                    envelope: str, envelope_sha256: str, wraps: Mapping[str, Mapping[str, str]],
                    blobs: Sequence[Mapping[str, Any]], status_key: str,
                    client: str) -> Dict[str, Any]:
    """The body of ``POST /api/submissions``, its ids derived here
    (``coordinate_hash``, ``submission_id``, ``status_key_hash``), so a
    caller cannot send one that the server would compute differently."""
    coordinate = formats.coordinate_hash(writer, d)
    body = {
        "v": formats.CONTRACT_VERSION,
        "submission_id": formats.submission_id(writer, publication, coordinate, round_),
        "round": round_,
        "revision": revision,
        "revision_hash": revision_hash,
        "publication": publication,
        "key_event_id": key_event_id,
        "epoch": epoch,
        "kind": kind,
        "title": title,
        "coordinate_hash": coordinate,
        "signer": signer,
        "participants": list(participants),
        "p_sub": p_sub,
        "envelope": envelope,
        "envelope_sha256": envelope_sha256,
        "wraps": {name: dict(wrap) for name, wrap in wraps.items()},
        "blobs": [dict(blob) for blob in blobs],
        "status_key_hash": formats.status_key_hash(status_key),
        "client": client,
    }
    problem = submission_body_problem(body)
    if problem:
        raise FormatError(problem)
    return body


def submission_body_problem(body: Any) -> Optional[str]:
    """Why ``body`` must not be sent, or None. The server checks all of
    this too; checked here, a mistake costs no signature and no upload."""
    if not isinstance(body, dict):
        return "the body must be an object"
    try:
        formats.need(body.get("v") == formats.CONTRACT_VERSION, "the body's version")
        formats.need(formats.is_hex64(body.get("submission_id")), "the submission id")
        formats.need(formats.is_int(body.get("round"), low=1), "the round")
        formats.need(formats.is_int(body.get("revision"), low=1), "the revision")
        formats.need(formats.is_hex64(body.get("revision_hash")), "the revision hash")
        formats.parse_address(body.get("publication"))
        formats.need(formats.is_hex64(body.get("key_event_id")), "the key event")
        formats.need(formats.is_int(body.get("epoch")), "the epoch")
        formats.need(body.get("kind") in formats.KINDS, "the kind")
        title = body.get("title")
        formats.need(isinstance(title, str) and 0 < len(title) <= formats.MAX_TITLE_CHARS,
                     f"the title has 1 to {formats.MAX_TITLE_CHARS} characters")
        formats.refuse_secret_keys([title])
        formats.need(body.get("signer") in formats.SIGNERS, "who signs")
        participants = body.get("participants")
        formats.need(isinstance(participants, list)
                     and len(participants) <= formats.MAX_PARTICIPANTS
                     and all(formats.is_pubkey(p) for p in participants)
                     and len(set(participants)) == len(participants),
                     f"at most {formats.MAX_PARTICIPANTS} people take part")
        formats.need(formats.is_pubkey(body.get("p_sub")), "the submission key")
        wraps = body.get("wraps")
        formats.need(isinstance(wraps, dict) and {"redaktion", "writer"} <= set(wraps)
                     and all(formats.is_wrap(w) for w in wraps.values()),
                     "the wraps for the editorial team and the writer")
        formats.need(set(wraps) - {"redaktion", "writer"} <= set(participants),
                     "a wrap for someone who does not take part")
        blobs = body.get("blobs")
        formats.need(isinstance(blobs, list) and all(
            isinstance(b, dict) and formats.is_hex64(b.get("sha256")) for b in blobs),
            "the files")
        formats.need(len({b["sha256"] for b in blobs}) == len(blobs), "a file listed twice")
        formats.need(formats.is_hex64(body.get("status_key_hash")), "the status key hash")
        if "source" in body:
            formats.need(isinstance(body["source"], dict), "the source")
        else:
            envelope = formats.b64decode(body.get("envelope"),
                                         limit=formats.MAX_ENVELOPE_BYTES)
            formats.need(formats.sha256_hex(envelope) == body.get("envelope_sha256"),
                         "the envelope hash")
    except FormatError as exc:
        return str(exc)
    return None


__all__ = [
    "Code", "ENV_LOCAL_HTTP", "ENV_SERVER", "Instance", "MAX_ANSWER_BYTES", "Session",
    "StandupApi", "StandupError", "SubmissionDetail", "configured_server",
    "local_http_allowed", "submission_body", "submission_body_problem", "usable_server_url",
]
