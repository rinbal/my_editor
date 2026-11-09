# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What went wrong with a STANDUP request (editorial contract 12.2,
feat/redaktion 5c11475).

:class:`StandupError` is the one failure value of the client: the
contract's code where the server gave one, the app's own otherwise, and
``maybe_stored`` when the request may have arrived although no answer
did (12.4, "No answer after sending"). :func:`error_from_answer` reads a
refusal's envelope ``{"ok": false, "error": {"code", "message",
"detail"}}``. Pure: no Qt, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from ..http_answers import clean_text
from ..signed_api import BAD_RESPONSE, OFFLINE, SIGNER_DECLINED, SIGNER_UNREACHABLE, TIMEOUT
from . import formats
from .formats import FormatError


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
    STOPPED = "stopped"                    # the account was let go: nothing more goes out
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
    METHOD_NOT_ALLOWED = "method_not_allowed"   # only a bug or a changed server
    STALE_REVISION = "stale_revision"
    INVALID_TRANSITION = "invalid_transition"
    CONFLICT = "conflict"
    EPOCH = "epoch"
    NO_DECIDER = "no_decider"   # nobody outside the writer and contributors could decide
    TOO_LARGE = "too_large"
    INVALID = "invalid"
    KEY_EVENT_INVALID = "key_event_invalid"
    SETTINGS_INVALID = "settings_invalid"
    WRAP_INVALID = "wrap_invalid"
    RATE_LIMITED = "rate_limited"


# 12.2: the codes each status may carry; the first is the fallback for a
# code this app does not know. 500 carries "internal", which says no more
# than the status.
_CODES_BY_STATUS = {
    400: (Code.BAD_REQUEST,),
    401: (Code.AUTH_INVALID, Code.AUTH_REQUIRED, Code.SESSION_EXPIRED),
    403: (Code.FORBIDDEN, Code.PAUSED, Code.BLOCKED, Code.LIMIT_OPEN, Code.NOT_MEMBER,
          Code.ROLE),
    404: (Code.NOT_FOUND,),
    405: (Code.METHOD_NOT_ALLOWED,),
    409: (Code.CONFLICT, Code.STALE_REVISION, Code.INVALID_TRANSITION, Code.EPOCH,
          Code.NO_DECIDER),
    413: (Code.TOO_LARGE,),
    422: (Code.INVALID, Code.KEY_EVENT_INVALID, Code.SETTINGS_INVALID, Code.WRAP_INVALID),
    429: (Code.RATE_LIMITED,),
}

_MAX_DETAIL_KEYS = 20
_MAX_DETAIL_WORDS = 64


@dataclass(frozen=True)
class StandupError:
    """One failure. ``message`` is the server's own text (German) or a
    short reason of the app's, never shown raw; ``detail`` is the
    server's detail, cleaned; ``maybe_stored`` says the request may have
    reached the server although no answer came back, which for a
    submission means: look it up (12.4), do not tell the person it
    failed. ``own`` says the server's own envelope named ``code``: a
    proxy's page with the same status only has the status, so its 404 is
    no ``not_found`` of the server (contract 2, bde1274)."""

    code: str
    status: Optional[int] = None
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    retry_after: Optional[int] = None
    maybe_stored: bool = False
    own: bool = False

    @property
    def reason(self) -> str:
        """``detail.reason`` of an ``auth_invalid`` (12.2), or ``""``."""
        value = self.detail.get("reason")
        return value if isinstance(value, str) else ""


def refused(message: str) -> StandupError:
    """A request this app does not send, and why."""
    return StandupError(Code.REFUSED, message=message)


def error_from_answer(status: int, raw: bytes, *,
                      retry_after: Optional[int] = None) -> StandupError:
    """The error a non-2xx answer with body ``raw`` says. A body that is
    not the contract's envelope still has its status: the code is then
    the status's first one (a proxy's 502 says ``server``)."""
    try:
        body = formats.strict_json(raw, limit=len(raw)) if raw else {}
    except FormatError:
        body = {}       # a proxy's page: the status is all there is
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {}
    server_code = error.get("code")
    allowed = _CODES_BY_STATUS.get(status)
    if allowed is not None:
        code = server_code if server_code in allowed else allowed[0]
    elif 500 <= status < 600:
        code = Code.SERVER
    else:
        # 415 and the rest: only a bug or a changed server.
        code = Code.BAD_RESPONSE
    own = (isinstance(body, dict) and body.get("ok") is False and allowed is not None
           and server_code in allowed)
    return StandupError(code, status=status, message=clean_text(error.get("message")),
                        detail=_clean_detail(error.get("detail")),
                        retry_after=retry_after, maybe_stored=status >= 500, own=own)


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
            cleaned[clean_text(key)] = clean_text(item)
        elif item is None or isinstance(item, bool) or formats.is_int(item, low=-(2 ** 53)):
            cleaned[clean_text(key)] = item
        elif isinstance(item, list) and all(isinstance(x, str) for x in item):
            cleaned[clean_text(key)] = tuple(clean_text(x) for x in item[:_MAX_DETAIL_WORDS])
    return cleaned


__all__ = ["Code", "StandupError", "error_from_answer", "refused"]
