"""Kind 24242 Blossom auth event helpers (BUD-02).

A Blossom server requires every privileged request (upload, list,
delete, mirror) to carry an ``Authorization: Nostr <base64>`` header
where the base64 payload is a signed kind 24242 event. The event tags
declare which action the bearer is authorising; the server rejects the
request if the tags don't match the URL it received.

These helpers stay pure: they build the unsigned event dict and the
header string. Actual signing is handed off to ``BunkerClient.sign_event``
(NIP-46) by the caller.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional


BLOSSOM_AUTH_KIND = 24242

# Default lifetime for an auth event, in seconds. Five minutes is the
# STANDUP default; the same value works here because Blossom servers
# check expiration against the request reception time, not the event
# created_at.
DEFAULT_AUTH_TTL_SECONDS = 300


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
    ``"upload"`` because the server treats /mirror as an upload by URL.)

    ``file_hash`` is the lowercase hex sha256 of the file the auth event
    is targeting; required for ``upload`` (of the actual file) and
    ``delete``, omitted for ``list`` and ``upload`` of a /mirror request
    where the server picks the hash from the source URL.

    ``server`` is the origin (e.g. ``"https://blossom.band"``) the auth
    event is scoped to. Including it lets servers detect cross-server
    replays.

    ``expiration`` is a unix timestamp in seconds; defaults to now + 5 min.

    ``pubkey_hex``, when supplied, is set on the unsigned event so the
    bunker pipeline can validate it matches the active profile before
    signing. The remote signer overwrites this with its own value as
    part of signing, so leaving it None is also fine.
    """
    expires_at = expiration if expiration is not None else int(time.time()) + DEFAULT_AUTH_TTL_SECONDS

    tags: list[list[str]] = [
        ["t", action],
        ["expiration", str(int(expires_at))],
    ]
    if file_hash:
        tags.append(["x", file_hash.lower()])
    if server:
        tags.append(["server", server])

    event: dict = {
        "kind": BLOSSOM_AUTH_KIND,
        "created_at": int(time.time()),
        "tags": tags,
        "content": f"Authorize {action}",
    }
    if pubkey_hex:
        event["pubkey"] = pubkey_hex.lower()
    return event


def to_auth_header(signed_event: dict) -> str:
    """Encode a signed kind 24242 event into an Authorization header value.

    Wire format: ``Nostr <base64(json(signed_event))>``. The JSON is the
    most compact form (no whitespace, ensure_ascii=False) because that's
    what every Blossom server we target expects, and it keeps the header
    length predictable for size-limited transports.
    """
    payload = json.dumps(signed_event, separators=(",", ":"), ensure_ascii=False)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"Nostr {encoded}"
