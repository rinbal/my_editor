# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A JSON value as the other apps of the editorial contract read and
write it: they are JavaScript (STANDUP, the contract's reference,
docs/editorial-fixtures/crosscheck.reference.mjs), so a list's records
are ranked and named here as JavaScript ranks and names them, or two
apps would keep different records of the same list and write it back
and forth (13 at 0456312, RG-FMd-a).

- :func:`record_text`: a record's canonical JSON, as the reference
  writes it before hashing.
- :func:`js_truthy`: whether JavaScript takes a value for true
  (``deleted``).
- :func:`js_string`: a value as ``String()`` writes it (a tombstone's
  key).

Pure: no Qt.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

# The integers every app reads exactly (JavaScript's safe integers).
MAX_SAFE = 2 ** 53 - 1


def record_text(value: Any) -> str:
    """The canonical JSON of a record as the merge hashes it, the text
    the contract's reference writes (crosscheck.reference.mjs): keys
    sorted as JavaScript sorts them (by UTF-16 code unit), no whitespace,
    every value as ``JSON.stringify`` writes it. For what canonical JSON
    holds (integers, ASCII keys) it is :func:`formats.canonical_json`,
    the text of ``json.dumps(sort_keys=True, separators=(",", ":"),
    ensure_ascii=False)``; a number canonical JSON cannot hold is written
    as JavaScript reads and writes it."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_js_text(key) + ":" + record_text(value[key])
                              for key in keys) + "}"
    if isinstance(value, list):
        return "[" + ",".join(record_text(item) for item in value) + "]"
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return _js_number(value)
    return _js_text(str(value))


def _js_text(text: str) -> str:
    """A string as ``JSON.stringify`` writes it: a lone surrogate (Python
    reads one from ``\ud800``) escaped, as JavaScript does."""
    return re.sub("[\ud800-\udfff]", lambda found: "\\u%04x" % ord(found.group()),
                  json.dumps(text, ensure_ascii=False))


def _js_number(number: Any) -> str:
    """A JSON number as JavaScript reads and writes it (Number::toString):
    a number is a double there, so an integer beyond 2^53 is rounded, 1.0
    is "1", and the exponent form starts at 1e21 and below 1e-6."""
    if isinstance(number, int) and abs(number) <= MAX_SAFE:
        return str(number)
    value = float(number)
    if value != value or value in (float("inf"), float("-inf")):
        return "null"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # The shortest digits that read back as the same double (repr, as
    # JavaScript picks them): value = digits * 10^(n - k).
    _sign, places, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, places)).rstrip("0")
    exponent += len(places) - len(digits)
    n = len(digits) + exponent
    k = len(digits)
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + digits
    else:
        power = n - 1
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + \
            ("+" if power >= 0 else "-") + str(abs(power))
    return sign + text


def js_truthy(value: Any) -> bool:
    """Whether JavaScript takes the JSON ``value`` for true: all but
    null, false, 0 and the empty string (an empty list or object too)."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and value == value
    return True


def js_string(value: Any) -> str:
    """A JSON value as JavaScript's ``String()`` writes it."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    return "[object Object]"


__all__ = ["MAX_SAFE", "js_string", "js_truthy", "record_text"]
