# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How large a signer app's answer is, at its largest (STANDUP's editorial
contract 12.5 "Answer size", feat/redaktion 9c3447a; vector
``signer_answer``). Pure: no Qt, no network.

A decryption or a signature comes back inside the signer app's JSON
answer, escaped, and the whole answer must fit one NIP-44 message
(65,535 bytes). Encoders differ: Go's ``encoding/json`` writes ``<``,
``>``, ``&``, U+2028 and U+2029 as six-character ``\\uXXXX`` escapes, nak
(json-iterator) also writes backspace and form feed as ``\\u0008`` and
``\\u000c``. So every answer is measured with the worst-case escaper:
``"``, ``\\``, newline, carriage return and tab two characters; every
other character below U+0020 and ``<``, ``>``, ``&``, U+2028 and U+2029
six; everything else its UTF-8 bytes.

- ``sign_event``: the result is the signed event as a JSON string, so the
  event is escaped twice:
  ``escaped({"id": <32-char id>, "result": escaped(<event>)})``.
- ``nip44_decrypt``: the plaintext is the result string, escaped once:
  ``escaped({"id": <32-char id>, "result": plaintext})``.

Either must be at most :data:`SIGNER_ANSWER_MAX`: 65,535 less 64 bytes
for fields a signer may add (an empty ``error``).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "REQUEST_ID", "SIGNER_ANSWER_MAX", "decrypt_answer_size", "escaped", "escaped_text_size",
    "sign_answer_size",
]

# 65,535 (one NIP-44 message) less 64 bytes a signer may add.
SIGNER_ANSWER_MAX = 65_471
# The longest request id an app sends (32 characters), echoed in the answer.
REQUEST_ID = "0" * 32

# What a sign_event answer adds to the event asked for, as placeholders of
# their length: the id, the pubkey and the signature.
_SIGNED = {"id": "0" * 64, "pubkey": "0" * 64, "sig": "0" * 128}

_TWO = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_SIX = frozenset({"<", ">", "&", " ", " "})


def _escape_char(ch: str) -> str:
    short = _TWO.get(ch)
    if short is not None:
        return short
    if ord(ch) < 0x20 or ch in _SIX:
        return f"\\u{ord(ch):04x}"
    return ch


def _string(text: str) -> str:
    return '"' + "".join(_escape_char(ch) for ch in text) + '"'


def escaped(value: Any) -> str:
    """``value`` as JSON without whitespace, every string (keys too)
    escaped at its longest. Numbers, true, false and null as JSON writes
    them."""
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(escaped(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(_string(str(key)) + ":" + escaped(item)
                              for key, item in value.items()) + "}"
    return json.dumps(value)


def escaped_text_size(text: str) -> int:
    """UTF-8 bytes of ``text`` escaped at its longest, without the quotes.
    Additive: the escape of a text is the escapes of its characters, so
    the size of a page is the sum of the sizes of its parts."""
    return len(_string(text).encode("utf-8")) - 2


_DECRYPT_OVERHEAD = len(escaped({"id": REQUEST_ID, "result": ""}).encode("utf-8"))


def decrypt_answer_size(plaintext: str) -> int:
    """Bytes of the answer to ``nip44_decrypt`` that hands ``plaintext``
    back."""
    return _DECRYPT_OVERHEAD + escaped_text_size(plaintext)


def sign_answer_size(unsigned: dict) -> int:
    """Bytes of the answer to ``sign_event`` for ``unsigned``: the signed
    event (placeholders for what the signer adds), escaped twice."""
    event = {"kind": unsigned["kind"], "content": unsigned.get("content", ""),
             "tags": unsigned.get("tags", []), "created_at": int(unsigned["created_at"])}
    return decrypt_answer_size(escaped(dict(event, **_SIGNED)))
