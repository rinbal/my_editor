# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Put one blob on one server: sign a token, send it, verify the answer.

Two callers used to do this, in two different shapes. The importer's
version was the better one, injected transport, pure orchestration,
explicit callbacks, sequential so the signer is asked once at a time,
and the library store's version fanned out in parallel and threw its
failures away. This module is that better shape, extracted, so both
callers share it and the store moves toward the importer rather than the
other way round.

Two functions, because there are two ways a blob gets onto a server:

- :func:`upload_to_server` sends the bytes. Used when this process holds
  them, which after the importer stopped asking servers to fetch
  arbitrary third-party URLs is every case except one.
- :func:`mirror_to_server` asks the server to copy the blob from another
  server that already has it (BUD-04). Used only for a blob this app
  just uploaded, where the hash is known and BUD-04's own example flow
  applies.

Both sign a ``t=upload`` token carrying the blob hash as ``x`` and the
target's bare domain as ``server`` (BUD-11), one signature per server.
BUD-04's example reuses a single token across servers; that is declined
here, because BUD-11's Security Considerations call an unscoped token
replayable for its whole validity window and a scoped one is invalid at
the next server anyway.

Failures are reported, never swallowed. A refused source URL and a
descriptor naming a different blob both reach ``on_failure`` with a
stable code, and a refusal that can be decided locally is decided before
the signer is asked, so it costs no prompt and no request.

Home: ``client.py`` must not import the bunker, which would drag the
signer into the transport layer; ``auth.py`` is pure event building; and
having ``nostr.blossom`` import ``nostr.imports`` would invert the
layering. A small module of its own is the only arrangement that lets
both callers share the code without a cycle.
"""

from __future__ import annotations

import hashlib
from typing import Callable

import url_safety

from ..bunker import BunkerSessionPool
from ..profiles import Profile
from .auth import build_blossom_auth_event
from .client import BlossomClient, BlossomError, UploadResult, server_origin
from .errors import ERROR_CODES


# Kept byte-identical to what the store and the importer emitted before
# the extraction: the asset layer still classifies a signer failure by
# matching the start of this sentence.
_SIGNER_PREFIX = "signer rejected the Blossom auth event: "

_UNSAFE_SOURCE = "mirror source URL was not allowed"


def upload_to_server(
    *,
    session_pool: BunkerSessionPool,
    profile: Profile,
    client: BlossomClient,
    server: str,
    body: bytes,
    mime: str,
    sha256: str = "",
    on_success: Callable[[UploadResult], None],
    on_failure: Callable[[BlossomError], None],
) -> None:
    """Sign one token and ``PUT /upload`` ``body`` to ``server``.

    ``sha256`` is the hash of ``body``; it is computed here when the
    caller does not already hold it, so the ``x`` tag, the ``X-SHA-256``
    header and the bytes on the wire can never disagree. The descriptor
    the server returns is checked against it before ``on_success``.
    """
    sha = (sha256 or hashlib.sha256(body).hexdigest()).lower()
    origin = server_origin(server)

    def _send(signed: dict) -> None:
        client.upload(
            origin,
            body,
            mime or "application/octet-stream",
            signed,
            on_success=lambda result: _verify(result, sha, on_success, on_failure),
            on_failure=on_failure,
            sha256=sha,
        )

    _sign_upload_token(
        session_pool=session_pool,
        profile=profile,
        origin=origin,
        sha256=sha,
        on_signed=_send,
        on_failure=on_failure,
    )


def mirror_to_server(
    *,
    session_pool: BunkerSessionPool,
    profile: Profile,
    client: BlossomClient,
    server: str,
    source_url: str,
    sha256: str,
    on_success: Callable[[UploadResult], None],
    on_failure: Callable[[BlossomError], None],
) -> None:
    """Sign one token and ask ``server`` to copy the blob at ``source_url``.

    ``sha256`` is the hash of the blob being copied. BUD-11's endpoint
    table makes it REQUIRED as an ``x`` tag on ``PUT /mirror``, with
    "SHA-256 of the mirrored blob" as the implied hash, and BUD-04 step 5
    has the destination server verify the bytes it downloads against it.
    A caller that has never seen the bytes cannot produce that value and
    therefore cannot use this function; it should fetch and upload
    instead.

    ``source_url`` is refused before anything is signed, so an address
    that must never be handed to a server costs neither a signer prompt
    nor a request.
    """
    sha = (sha256 or "").lower()
    if not url_safety.is_safe_mirror_source(source_url):
        on_failure(BlossomError(_UNSAFE_SOURCE, code=ERROR_CODES.UNSAFE_URL))
        return
    origin = server_origin(server)

    def _send(signed: dict) -> None:
        client.mirror(
            origin,
            source_url,
            signed,
            on_success=lambda result: _verify(result, sha, on_success, on_failure),
            on_failure=on_failure,
            sha256=sha,
        )

    _sign_upload_token(
        session_pool=session_pool,
        profile=profile,
        origin=origin,
        sha256=sha,
        on_signed=_send,
        on_failure=on_failure,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _sign_upload_token(
    *,
    session_pool: BunkerSessionPool,
    profile: Profile,
    origin: str,
    sha256: str,
    on_signed: Callable[[dict], None],
    on_failure: Callable[[BlossomError], None],
) -> None:
    """Resolve the signer and sign one ``t=upload`` token for ``origin``."""
    unsigned = build_blossom_auth_event(
        "upload",
        file_hash=sha256,
        server=origin,
        pubkey_hex=profile.user_pubkey,
    )

    def _on_ready(bunker_client) -> None:
        bunker_client.sign_event(
            unsigned,
            on_success=on_signed,
            on_failure=lambda reason: on_failure(_signer_error(reason, prefixed=True)),
        )

    session_pool.get(
        profile,
        on_ready=_on_ready,
        on_error=lambda reason: on_failure(_signer_error(reason)),
    )


def _signer_error(reason, *, prefixed: bool = False) -> BlossomError:
    text = f"{_SIGNER_PREFIX}{reason}" if prefixed else str(reason)
    return BlossomError(text, code=ERROR_CODES.SIGNER_REJECTED)


def _verify(
    result: UploadResult,
    sha256: str,
    on_success: Callable[[UploadResult], None],
    on_failure: Callable[[BlossomError], None],
) -> None:
    """Route a descriptor that names another blob to ``on_failure``.

    The client already refuses one, so this is the second net rather than
    the first. It is here because the primitive is what callers trust:
    handing it a client that skipped the check must not turn somebody
    else's blob into a success.
    """
    if str(result.get("hash") or "").lower() != sha256:
        on_failure(
            BlossomError(
                "server described a different blob than the one sent",
                code=ERROR_CODES.HASH_MISMATCH,
            )
        )
        return
    on_success(result)
