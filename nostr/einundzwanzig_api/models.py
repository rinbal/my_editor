# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the membership API answers, read defensively.

The errors every call can end in (:class:`ErrorCode`, :class:`ApiError`),
the records the API returns, and the parsers that turn its JSON into
them. The server is a third party: a parser raises ValueError for
anything malformed, and the client turns that into an ApiError, so
nothing a server sends can raise into Qt.

Pure: no Qt, no network, no clock.
"""

from __future__ import annotations

import copy
import email.utils
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import url_safety

from .bolt11 import bolt11_amount_sats


# --------------------------------------------------------------------------- #
# Limits                                                                       #
# --------------------------------------------------------------------------- #

# A Retry-After further out than this is treated as this. Invoice creation
# has a daily quota, so a day is the longest wait that means anything.
_MAX_RETRY_AFTER_SECONDS: int = 24 * 60 * 60

_MAX_MESSAGE_CHARS: int = 300
_MAX_FIELD_ERRORS: int = 20
_MAX_MESSAGES_PER_FIELD: int = 5

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #

class ErrorCode:
    """The stable error codes. Plain strings, so they survive a signal."""

    UNAVAILABLE = "unavailable"                # joining in the app isn't available right now
    OFFLINE = "offline"                        # no connection to the server
    TIMEOUT = "timeout"                        # the server stopped answering
    UNAUTHORIZED = "unauthorized"              # 401: key or signature refused
    VALIDATION = "validation"                  # 422, or refused before sending
    RATE_LIMITED = "rate_limited"              # 429, see ``retry_after``
    NOT_FOUND = "not_found"                    # 404: nothing on record
    CONFLICT = "conflict"                      # 409
    SERVER = "server"                          # 5xx: the server's own trouble
    SIGNER_DECLINED = "signer_declined"        # the signer said no, or answered nonsense
    SIGNER_UNREACHABLE = "signer_unreachable"  # the signer never answered
    BAD_RESPONSE = "bad_response"              # an answer this client cannot use


ERROR_CODES = frozenset(
    value for name, value in vars(ErrorCode).items() if name.isupper()
)


@dataclass(frozen=True)
class ApiError:
    """Why a call failed.

    ``code`` is one of :data:`ERROR_CODES` and is what callers branch on.
    ``status`` is the HTTP status when the server answered. ``message``
    is the server's own summary (or a diagnostic for the non-HTTP
    codes); it is for logs, not for the user, who gets :func:`humanize`.
    ``field_errors`` maps a request field to the reasons it was refused.
    ``retry_after`` is in seconds, when the server said how long to wait.
    """

    code: str
    status: Optional[int] = None
    message: str = ""
    field_errors: Dict[str, List[str]] = field(default_factory=dict)
    retry_after: Optional[int] = None


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_text(value: Any) -> str:
    """Server or signer text, made safe to keep in an error: control
    characters go, whitespace collapses, and the result is capped."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_CONTROL_CHARS.sub(" ", value).split())
    return text[:_MAX_MESSAGE_CHARS]


def _clean_field_errors(value: Any) -> Dict[str, List[str]]:
    """``{"field": ["reason", ...]}`` from a 422 body, whatever arrived."""
    if not isinstance(value, dict):
        return {}
    cleaned: Dict[str, List[str]] = {}
    for name, reasons in list(value.items())[:_MAX_FIELD_ERRORS]:
        if not isinstance(name, str) or not name:
            continue
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list):
            continue
        texts = [_clean_text(r) for r in reasons[:_MAX_MESSAGES_PER_FIELD]]
        texts = [t for t in texts if t]
        cleaned[_clean_text(name)] = texts
    return cleaned


def parse_retry_after(value: Any, *, now: float) -> Optional[int]:
    """Seconds to wait from a Retry-After header, or None.

    Either form the HTTP spec allows: a number of seconds or a date.
    Clamped to a day, and never negative.
    """
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("latin-1", errors="replace")
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        seconds = int(text)
    else:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            from datetime import timezone
            when = when.replace(tzinfo=timezone.utc)
        seconds = math.ceil(when.timestamp() - now)
    return max(0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


# --------------------------------------------------------------------------- #
# Responses                                                                    #
# --------------------------------------------------------------------------- #

STATUS_NONE = "none"
STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_MEMBER = "member"
STATUS_LAPSED = "lapsed"
MEMBERSHIP_STATUSES = frozenset(
    {STATUS_NONE, STATUS_AWAITING_PAYMENT, STATUS_MEMBER, STATUS_LAPSED}
)


@dataclass(frozen=True)
class MembershipConfig:
    """What joining costs and what an application carries.

    ``fee`` is an integer in ``currency``. The spec calls it "the
    smallest unit" while its example reads 21 CHF and the association's
    own client renders it as whole units; show it as the server sends it
    and do not divide.
    """

    fee: int
    currency: str
    year: int
    statutes_url: str
    statutes_version: str
    statutes_adopted_at: str
    required_fields: Tuple[str, ...]
    optional_fields: Tuple[str, ...]
    application_text_max_length: int


@dataclass(frozen=True)
class CurrentYear:
    """The fee year being collected, and whether this person paid it."""

    year: int
    fee: int
    currency: str
    paid: bool
    receipt_url: Optional[str]


@dataclass(frozen=True)
class MembershipStatus:
    """The signing user's own record, as ``GET /me`` reports it.

    Render ``membership_status``. ``association_status`` is the category
    the board assigned and does not lapse, so on its own it calls an
    unpaid member active; the spec is emphatic about this.
    """

    pubkey: str
    membership_status: str
    association_status: str
    statutes_accepted_at: Optional[str]
    applied_at: Optional[str]
    current_year: CurrentYear

    @property
    def is_member(self) -> bool:
        return self.membership_status == STATUS_MEMBER

    @property
    def needs_application(self) -> bool:
        return self.membership_status == STATUS_NONE

    @property
    def needs_payment(self) -> bool:
        return self.membership_status in (STATUS_AWAITING_PAYMENT, STATUS_LAPSED)


@dataclass(frozen=True)
class FeeEntry:
    """One annual fee on record."""

    year: int
    amount: int
    currency: str
    paid: bool
    receipt_url: Optional[str]


@dataclass(frozen=True)
class Invoice:
    """A checkout for one fee year.

    ``bolt11`` is optional and additive: None means "use the checkout
    page", never "something went wrong" and never "expired". On a refresh
    a None ``checkout_url`` means the invoice expired or was invalidated
    and the year is free for a new checkout; see :attr:`expired`.
    """

    checkout_url: Optional[str]
    bolt11: Optional[str]
    created: bool
    payment: FeeEntry

    @property
    def expired(self) -> bool:
        return self.checkout_url is None and not self.payment.paid

    @property
    def amount_sats(self) -> Optional[int]:
        return bolt11_amount_sats(self.bolt11) if self.bolt11 else None


@dataclass(frozen=True)
class Erasure:
    """The answer to an erasure request.

    ``retained_payments`` counts the fees kept as anonymised bookkeeping
    and is None on every call after the first: the link needed to count
    them is what the erasure destroyed.
    """

    erased: bool
    retained_payments: Optional[int]


@dataclass(frozen=True)
class MembershipExport:
    """The data-subject access export.

    ``document`` is the server's ``data`` object as decoded, kept whole
    so it can be saved for the user unchanged. Only the two fields a
    caller needs to label it are lifted out.
    """

    pubkey: str
    membership_status: str
    document: Dict[str, Any]


def _require_dict(value: Any, what: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} is not an object")
    return value


def _require_int(obj: Dict[str, Any], key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} is not an integer")
    return value


def _require_str(obj: Dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is not a string")
    return value


def _require_bool(obj: Dict[str, Any], key: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} is not a boolean")
    return value


def _optional_str(obj: Dict[str, Any], key: str) -> Optional[str]:
    """A nullable string. Absent reads as null: the reader is tolerant
    about the fields it can do without and strict about the rest."""
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} is not a string or null")
    return value


def _optional_url(obj: Dict[str, Any], key: str) -> Optional[str]:
    """A nullable link the user may be sent to.

    A link the browser gate would refuse (``javascript:``, ``file:``)
    makes the whole answer unusable rather than being dropped, so a
    hostile or broken server cannot hand the UI something to open.
    """
    value = _optional_str(obj, key)
    if value is None:
        return None
    if not url_safety.is_safe_external_url(value):
        raise ValueError(f"{key} is not a usable web address")
    return value


def _require_url(obj: Dict[str, Any], key: str) -> str:
    value = _optional_url(obj, key)
    if value is None:
        raise ValueError(f"{key} is missing")
    return value


def _str_tuple(value: Any, what: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{what} is not a list")
    return tuple(v for v in value if isinstance(v, str))


def parse_config(data: Any) -> MembershipConfig:
    """``MembershipConfigResource``. Raises ValueError when malformed."""
    obj = _require_dict(data, "config")
    statutes = _require_dict(obj.get("statutes"), "statutes")
    application = _require_dict(obj.get("application"), "application")
    return MembershipConfig(
        fee=_require_int(obj, "fee"),
        currency=_require_str(obj, "currency"),
        year=_require_int(obj, "year"),
        statutes_url=_require_url(statutes, "url"),
        statutes_version=_require_str(statutes, "version"),
        statutes_adopted_at=_require_str(statutes, "adopted_at"),
        required_fields=_str_tuple(application.get("required_fields"), "required_fields"),
        optional_fields=_str_tuple(application.get("optional_fields"), "optional_fields"),
        application_text_max_length=_require_int(application, "application_text_max_length"),
    )


def _parse_current_year(data: Any) -> CurrentYear:
    obj = _require_dict(data, "current_year")
    return CurrentYear(
        year=_require_int(obj, "year"),
        fee=_require_int(obj, "fee"),
        currency=_require_str(obj, "currency"),
        paid=_require_bool(obj, "paid"),
        receipt_url=_optional_url(obj, "receipt_url"),
    )


def _membership_status(obj: Dict[str, Any]) -> str:
    value = obj.get("membership_status")
    if value not in MEMBERSHIP_STATUSES:
        raise ValueError("membership_status is not one of the documented values")
    return value


def parse_membership(data: Any) -> MembershipStatus:
    """``MembershipResource``. Raises ValueError when malformed."""
    obj = _require_dict(data, "membership")
    pubkey = obj.get("pubkey")
    if not isinstance(pubkey, str) or not _HEX64.match(pubkey):
        raise ValueError("pubkey is not 64 lowercase hex characters")
    return MembershipStatus(
        pubkey=pubkey,
        membership_status=_membership_status(obj),
        association_status=_require_str(obj, "association_status"),
        statutes_accepted_at=_optional_str(obj, "statutes_accepted_at"),
        applied_at=_optional_str(obj, "applied_at"),
        current_year=_parse_current_year(obj.get("current_year")),
    )


def parse_fee_entry(data: Any) -> FeeEntry:
    """``PaymentEventResource``. Raises ValueError when malformed."""
    obj = _require_dict(data, "payment")
    return FeeEntry(
        year=_require_int(obj, "year"),
        amount=_require_int(obj, "amount"),
        currency=_require_str(obj, "currency"),
        paid=_require_bool(obj, "paid"),
        receipt_url=_optional_url(obj, "receipt_url"),
    )


def parse_payments(data: Any) -> Tuple[FeeEntry, ...]:
    """A list of ``PaymentEventResource``, newest year first."""
    if not isinstance(data, list):
        raise ValueError("payments is not a list")
    return tuple(parse_fee_entry(entry) for entry in data)


def parse_invoice(data: Any) -> Invoice:
    """``InvoiceResource``. Raises ValueError when malformed."""
    obj = _require_dict(data, "invoice")
    created = obj.get("created", False)
    if not isinstance(created, bool):
        raise ValueError("created is not a boolean")
    return Invoice(
        checkout_url=_optional_url(obj, "checkout_url"),
        bolt11=_optional_str(obj, "bolt11"),
        created=created,
        payment=parse_fee_entry(obj.get("payment")),
    )


def parse_erasure(data: Any) -> Erasure:
    """The ``DELETE /me`` answer. Raises ValueError when malformed."""
    obj = _require_dict(data, "erasure")
    retained = obj.get("retained_payments")
    if retained is not None and (
        not isinstance(retained, int) or isinstance(retained, bool) or retained < 0
    ):
        raise ValueError("retained_payments is not a count")
    return Erasure(erased=_require_bool(obj, "erased"), retained_payments=retained)


def parse_export(data: Any) -> MembershipExport:
    """``MembershipExportResource``. Raises ValueError when malformed."""
    obj = _require_dict(data, "export")
    subject = _require_dict(obj.get("subject"), "subject")
    pubkey = subject.get("pubkey")
    if not isinstance(pubkey, str) or not _HEX64.match(pubkey):
        raise ValueError("subject.pubkey is not 64 lowercase hex characters")
    for key in ("payments", "membership_grants"):
        if not isinstance(obj.get(key, []), list):
            raise ValueError(f"{key} is not a list")
    for key in ("member", "nostr_profile"):
        if obj.get(key) is not None and not isinstance(obj.get(key), dict):
            raise ValueError(f"{key} is not an object or null")
    return MembershipExport(
        pubkey=pubkey,
        membership_status=_membership_status(obj),
        document=copy.deepcopy(obj),
    )
