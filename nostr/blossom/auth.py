# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kind 24242 Blossom auth event helpers (BUD-11).

A Blossom server requires every privileged request (upload, list,
delete, mirror) to carry an ``Authorization: Nostr <base64url>`` header
where the payload is a signed kind 24242 event. The event tags declare
which action the bearer is authorising; the server rejects the request
if the tags don't match the URL it received.

These helpers stay pure: they build the unsigned event dict and the
header string. Actual signing is handed off to ``BunkerClient.sign_event``
(NIP-46) by the caller.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional

import url_safety


BLOSSOM_AUTH_KIND = 24242

# Default lifetime for an auth event, in seconds. Five minutes is the
# STANDUP default; the same value works here because Blossom servers
# check expiration against the request reception time, not the event
# created_at.
DEFAULT_AUTH_TTL_SECONDS = 300

# BUD-11 validation rule 2: "The created_at timestamp MUST be in the
# past." A server whose clock runs a second behind ours reads an
# exactly-now timestamp as the future and answers 401, which is the
# clock skew ``store.handle_list_error`` already blames for its
# retry-without-auth fallback. Backdating costs nothing: expiration is
# computed from now, so the token keeps its full TTL.
_CREATED_AT_BACKDATE_SECONDS = 60


def auth_server_domain(server: str) -> str:
    """Bare lowercase domain for a BUD-11 ``server`` tag.

    BUD-11 scopes an authorization token by DOMAIN NAME: "The value MUST
    be a lowercase domain name only (e.g., ``cdn.example.com``), not a
    full URL." This is NOT the same tag as BUD-03's kind 10063
    ``server``, which carries the full URL including the scheme. See ADR
    AD-11: making the two agree is a defect, not a cleanup.

    Accepts a full origin or an already-bare host and is idempotent.
    Returns ``""`` when no host can be read, which omits the tag.

    The port is dropped because the spec says domain name only. An IP
    literal is emitted verbatim, lowercase and unbracketed, rather than
    omitting the tag: BUD-11's Security Considerations make an unscoped
    token the worse failure, and loopback dev servers must keep working.
    """
    host = url_safety.host_of(server)
    if host:
        return host
    value = str(server or "").strip().lower()
    if not value or any(ch.isspace() for ch in value):
        return ""
    # No scheme, so url_safety cannot parse it. Trim anything that is
    # not the host so the tag can never carry a path or a port.
    value = value.split("/", 1)[0]
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def build_blossom_auth_event(
    action: str,
    *,
    file_hash: Optional[str] = None,
    server: Optional[str] = None,
    expiration: Optional[int] = None,
    pubkey_hex: Optional[str] = None,
) -> dict:
    """Return an *unsigned* kind 24242 auth event for ``action``.

    ``action`` is the value of the ``t`` tag: typically one of
    ``"upload"``, ``"list"``, ``"delete"``. (Mirror requests also use
    ``"upload"``: BUD-11's endpoint table gives ``PUT /mirror`` the
    ``upload`` verb.)

    ``file_hash`` is the lowercase hex sha256 of the blob the auth event
    is targeting. BUD-11's endpoint table marks an ``x`` tag REQUIRED
    for ``PUT /upload``, ``PUT /mirror`` and ``DELETE /<sha256>``, and
    not applicable for ``GET /list/<pubkey>``. For a mirror the implied
    hash is "SHA-256 of the mirrored blob", so the caller must supply it
    even though the request body is a URL.

    ``server`` is any spelling of the target server, full origin or bare
    host; the tag itself is always the bare lowercase domain BUD-11
    mandates. Scoping a token stops it being replayed on another server
    for the rest of its validity window.

    ``expiration`` is a unix timestamp in seconds; defaults to now + 5 min.

    ``pubkey_hex``, when supplied, is set on the unsigned event so the
    bunker pipeline can validate it matches the active profile before
    signing. The remote signer overwrites this with its own value as
    part of signing, so leaving it None is also fine. Some NIP-46
    signers also set ``created_at`` themselves, which makes the
    backdating below best effort.
    """
    now = int(time.time())
    expires_at = expiration if expiration is not None else now + DEFAULT_AUTH_TTL_SECONDS

    tags: list[list[str]] = [
        ["t", action],
        ["expiration", str(int(expires_at))],
    ]
    if file_hash:
        tags.append(["x", file_hash.lower()])
    if server:
        domain = auth_server_domain(server)
        if domain:
            tags.append(["server", domain])

    event: dict = {
        "kind": BLOSSOM_AUTH_KIND,
        "created_at": now - _CREATED_AT_BACKDATE_SECONDS,
        "tags": tags,
        "content": f"Authorize {action}",
    }
    if pubkey_hex:
        event["pubkey"] = pubkey_hex.lower()
    return event


def to_auth_header(signed_event: dict) -> str:
    """Encode a signed kind 24242 event into an Authorization header value.

    Wire format: ``Nostr <base64url-nopad(json(signed_event))>``. BUD-11:
    "the authorization token MUST be encoded as Base64 URL-safe without
    padding (Base64url, as used by JWTs) and use the Authorization
    scheme Nostr".

    The JSON is the most compact form (no whitespace,
    ensure_ascii=False) because that's what every Blossom server we
    target expects, and it keeps the header length predictable for
    size-limited transports. Non-ASCII survives: the payload is utf-8
    encoded before base64.
    """
    payload = json.dumps(signed_event, separators=(",", ":"), ensure_ascii=False)
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"Nostr {encoded.rstrip('=')}"
