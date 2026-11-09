# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading one field of a STANDUP answer: the value when it has the type
and range the contract gives it, else :class:`~.formats.FormatError`
naming the field. Shared by the views (``views.py``) and the other
answers (``answers.py``), which parse every answer once, here at the
boundary. Pure."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from . import formats
from .formats import is_hex64, is_int, need

# Text the contract does not bound otherwise.
MAX_FIELD_CHARS = 2_000


def read_object(value: Any, what: str) -> Mapping[str, Any]:
    need(isinstance(value, dict), f"{what} must be an object")
    return value


def read_list(value: Any, what: str, *, limit: int = 1_000) -> list:
    need(isinstance(value, list) and len(value) <= limit, f"{what} must be a list")
    return value


def read_hex64(value: Any, what: str) -> str:
    need(is_hex64(value), f"{what} must be 64 hex digits")
    return value


def read_optional_hex64(value: Any, what: str) -> Optional[str]:
    return None if value is None else read_hex64(value, what)


def read_pubkey(value: Any, what: str) -> str:
    need(formats.is_pubkey(value), f"{what} must be a public key")
    return value


def read_int(value: Any, what: str, *, low: int = 0) -> int:
    need(is_int(value, low=low), f"{what} must be a whole number of at least {low}")
    return value


def read_optional_int(value: Any, what: str, *, low: int = 0) -> Optional[int]:
    return None if value is None else read_int(value, what, low=low)


def read_bool(value: Any, what: str) -> bool:
    need(isinstance(value, bool), f"{what} must be true or false")
    return value


def read_text(value: Any, what: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    need(isinstance(value, str) and len(value) <= limit, f"{what} must be text")
    return value


def read_optional_text(value: Any, what: str, *, limit: int = MAX_FIELD_CHARS) -> Optional[str]:
    return None if value is None else read_text(value, what, limit=limit)


def read_one_of(value: Any, allowed: Tuple[str, ...], what: str) -> str:
    need(value in allowed, f"{what} is not one of {', '.join(allowed)}")
    return value


def read_hex64_list(value: Any, what: str, *, limit: int = 1_000) -> Tuple[str, ...]:
    return tuple(read_hex64(item, what) for item in read_list(value, what, limit=limit))


def read_address(value: Any) -> str:
    formats.parse_address(value)
    return value


def read_wrap(value: Any, what: str) -> Mapping[str, str]:
    need(formats.is_wrap(value), f"{what} must be a key wrap")
    return value


def read_optional_wrap(value: Any, what: str) -> Optional[Mapping[str, str]]:
    return None if value is None else read_wrap(value, what)
