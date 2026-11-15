# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The STANDUP backend's checks of signed Nostr data, written again for
the fake server from the backend's rules (feat/redaktion-backend
1183930), without MyEditor's nostr/nip98.py, nostr/events.py or
nostr/bech32.py: a fake that checked with the client's own code would
agree with the client's mistakes instead of catching them.

- :func:`verified`: core/nostr_events.py ``shape_problem`` and
  ``verified`` (lowercase hex, exact types, the NIP-01 id, a BIP-340
  signature, checked with coincurve).
- :func:`check_nip98`: core/nip98.py ``verify_request_event`` and
  api/auth.py ``_verify_editorial_nip98`` with ``_check_editorial_tags``,
  in their order, with their reasons.
- :func:`decode_naddr`: the ``naddr`` of NIP-19, as core/
  editorial_submission.py ``link_source`` reads it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from typing import List, Optional
from urllib.parse import urlparse

from coincurve import PublicKeyXOnly

NIP98_KIND = 27235
# Contract 10 and the backend's config_sample.py NIP98_TIMESTAMP_WINDOW.
NIP98_WINDOW_SECONDS = 60
MAX_AUTH_HEADER = 8192                  # api/auth.py _MAX_AUTH_HEADER

FIELDS = ("id", "pubkey", "created_at", "kind", "tags", "content")
SIGNED_FIELDS = FIELDS + ("sig",)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")


class EventInvalid(ValueError):
    """The event is malformed, its id does not match, or its signature is bad."""


class Refused(Exception):
    """What the backend answers instead: ``code`` (``auth_required``,
    ``auth_invalid`` with ``reason``, or ``bad_request``)."""

    def __init__(self, code: str, reason: Optional[str] = None) -> None:
        super().__init__(code if reason is None else f"{code}: {reason}")
        self.code = code
        self.reason = reason


# -- NIP-01 (core/nostr_events.py) -------------------------------------------

def _hex64(value) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def shape_problem(event, *, signed: bool) -> Optional[str]:
    """Why ``event`` is not a well-formed NIP-01 event, or None."""
    if not isinstance(event, dict):
        return "not a JSON object"
    for name in FIELDS + (("sig",) if signed else ()):
        if name not in event:
            return f"missing {name}"
    if not _hex64(event["id"]):
        return "id"
    if not _hex64(event["pubkey"]):
        return "pubkey"
    # A JSON true is a bool, never a timestamp or a kind.
    if type(event["created_at"]) is not int or event["created_at"] < 0:
        return "created_at"
    if type(event["kind"]) is not int or not 0 <= event["kind"] <= 65535:
        return "kind"
    tags = event["tags"]
    if not isinstance(tags, list) or not all(
            isinstance(tag, list) and tag and all(isinstance(v, str) for v in tag)
            for tag in tags):
        return "tags"
    if not isinstance(event["content"], str):
        return "content"
    if signed and (not isinstance(event["sig"], str) or not _HEX128.fullmatch(event["sig"])):
        return "sig"
    return None


def event_id(event: dict) -> Optional[str]:
    """The NIP-01 id: SHA-256 of ``[0, pubkey, created_at, kind, tags,
    content]`` as compact UTF-8 JSON; None when it cannot be serialised
    (a lone surrogate)."""
    try:
        text = json.dumps([0, event["pubkey"], event["created_at"], event["kind"],
                           event["tags"], event["content"]],
                          separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (UnicodeEncodeError, TypeError, ValueError):
        return None


def _signature_valid(event: dict) -> bool:
    try:
        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(event["id"]))
    except (ValueError, TypeError):
        return False


def verified(event, kind: Optional[int] = None) -> dict:
    """The signed event reduced to its NIP-01 fields, or :class:`EventInvalid`."""
    problem = shape_problem(event, signed=True)
    if problem:
        raise EventInvalid(problem)
    if kind is not None and event["kind"] != kind:
        raise EventInvalid(f"expected kind {kind}, got {event['kind']}")
    if event_id(event) != event["id"]:
        raise EventInvalid("id does not match its content")
    if not _signature_valid(event):
        raise EventInvalid("signature is not valid")
    return {name: event[name] for name in SIGNED_FIELDS}


def tags_named(event: dict, name: str) -> List[list]:
    return [tag for tag in event["tags"] if tag[0] == name]


def first_tag(event: dict, name: str) -> Optional[list]:
    return next((tag for tag in event["tags"] if tag[0] == name), None)


# -- NIP-98 (core/nip98.py, api/auth.py) ---------------------------------------

def _first_value(event: dict, name: str) -> Optional[str]:
    """core/nip98.py _find_tag: the value of the first tag of that name
    that has one."""
    return next((tag[1] for tag in event["tags"] if len(tag) >= 2 and tag[0] == name), None)


def _binding(url: str) -> str:
    """core/nip98.py _normalise_url_for_binding: the path and query a
    signature binds (scheme and host change behind proxies)."""
    if not url:
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    if not parts.path:
        return url
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _decode(header: str) -> dict:
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "nostr":
        raise Refused("auth_invalid", "scheme")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise Refused("auth_invalid", "malformed") from None
    try:
        return json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Refused("auth_invalid", "malformed") from None


def check_nip98(header: str, *, method: str, url: str, body: bytes, now: int,
                require_payload: bool) -> dict:
    """The verified kind 27235 event that authorizes this request, or
    :class:`Refused`. ``header`` is the Authorization value as the server
    reads it, ``body`` the exact bytes (``b""`` for none).
    ``require_payload``: a signed action (contract 10); without it, a
    request with no body needs no payload tag, and one with a body still
    does (core/nip98.py). The caller spends the id once this passed."""
    if not header.strip():
        raise Refused("auth_required")
    if len(header) > MAX_AUTH_HEADER:
        raise Refused("auth_invalid", "malformed")
    event = _decode(header)
    if shape_problem(event, signed=True):
        raise Refused("auth_invalid", "malformed")
    try:
        event = verified(event)
    except EventInvalid:
        raise Refused("auth_invalid", "signature") from None
    if event["kind"] != NIP98_KIND:
        raise Refused("auth_invalid", "kind")
    if abs(int(now) - event["created_at"]) > NIP98_WINDOW_SECONDS:
        raise Refused("auth_invalid", "expired")
    if _binding(_first_value(event, "u") or "") != _binding(url):
        raise Refused("auth_invalid", "url")
    tag_method = _first_value(event, "method")
    if tag_method is None or tag_method.upper() != method.upper():
        raise Refused("auth_invalid", "method")
    tag_payload = _first_value(event, "payload")
    if tag_payload is not None:
        if tag_payload.lower() != hashlib.sha256(body).hexdigest():
            raise Refused("auth_invalid", "payload")
    elif body:
        # A header signed for no body is never good for one.
        raise Refused("auth_invalid", "payload")
    # api/auth.py _check_editorial_tags: one u, one method, one payload
    # spelled one way.
    if len(tags_named(event, "u")) != 1 or len(tags_named(event, "method")) != 1:
        raise Refused("auth_invalid", "malformed")
    payloads = tags_named(event, "payload")
    if not payloads:
        if require_payload:
            raise Refused("auth_invalid", "payload")
    elif len(payloads) != 1 or len(payloads[0]) < 2 \
            or not _HEX64.fullmatch(payloads[0][1]) \
            or not hmac.compare_digest(payloads[0][1], hashlib.sha256(body).hexdigest()):
        raise Refused("auth_invalid", "payload")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("bad_request") from None
    return event


# -- NIP-19 naddr ------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: List[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            check ^= generator[i] if (top >> i) & 1 else 0
    return check


def _bech32_bytes(text: str, hrp: str) -> Optional[bytes]:
    if text.lower() != text and text.upper() != text:
        return None
    text = text.lower()
    cut = text.rfind("1")
    if text[:cut] != hrp or cut + 7 > len(text):
        return None
    try:
        data = [_CHARSET.index(c) for c in text[cut + 1:]]
    except ValueError:
        return None
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    if _polymod(expanded + data) != 1:
        return None
    acc, bits, out = 0, 0, bytearray()
    for value in data[:-6]:
        acc = (acc << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or (acc << (8 - bits)) & 0xFF:     # padding left over, or not zero
        return None
    return bytes(out)


def decode_naddr(text) -> Optional[dict]:
    """``{"identifier", "pubkey", "kind", "relays"}`` of an ``naddr``, or
    None when it is not one."""
    if not isinstance(text, str):
        return None
    raw = _bech32_bytes(text, "naddr")
    if raw is None:
        return None
    found: dict = {"relays": []}
    at = 0
    while at + 2 <= len(raw):
        kind, size = raw[at], raw[at + 1]
        value = raw[at + 2:at + 2 + size]
        if len(value) != size:
            return None
        at += 2 + size
        try:
            if kind == 0:
                found.setdefault("identifier", value.decode("utf-8"))
            elif kind == 1:
                found["relays"].append(value.decode("utf-8"))
            elif kind == 2 and size == 32:
                found.setdefault("pubkey", value.hex())
            elif kind == 3 and size == 4:
                found.setdefault("kind", int.from_bytes(value, "big"))
        except UnicodeDecodeError:
            return None
    if at != len(raw) or not {"identifier", "pubkey", "kind"} <= set(found):
        return None
    return found
