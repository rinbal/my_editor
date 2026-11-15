# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading what a server or a signer says into an error the app keeps.

Shared by the signed API clients (``nostr/signed_api.py``, the
EINUNDZWANZIG membership client and the STANDUP client): the text of a
refusal, cleaned before it is kept, and how long a ``Retry-After`` asks
to wait. Pure: no Qt, no network.
"""

from __future__ import annotations

import email.utils
import math
import re
from datetime import timezone
from typing import Any, Optional

# A Retry-After further out than this is treated as this. Invoice creation
# has a daily quota, so a day is the longest wait that means anything.
MAX_RETRY_AFTER_SECONDS: int = 24 * 60 * 60

# Text kept from an answer is cut to this many characters.
MAX_TEXT_CHARS: int = 300

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def clean_text(value: Any) -> str:
    """Server or signer text, made safe to keep in an error: control
    characters go, whitespace collapses, and the result is capped. Not
    text at all gives ``""``."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_CONTROL_CHARS.sub(" ", value).split())
    return text[:MAX_TEXT_CHARS]


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
            when = when.replace(tzinfo=timezone.utc)
        seconds = math.ceil(when.timestamp() - now)
    return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))
