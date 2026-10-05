# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Joining the EINUNDZWANZIG association from inside the app.

``nostr/einundzwanzig.py`` answers "is this account on the public roster"
without telling the association who asked. This package is the other
direction: the signed membership API, through which a user applies,
pays the annual fee, reads their own record back, downloads it, or has
it erased. Every call here is made on purpose by the user, so here the
association does learn who is asking, and that is the point.

Spec: https://verein.einundzwanzig.space/docs/api.json (OpenAPI 3.1).
Only the main surface under ``/api/v1/membership`` is used. The "native
app" branch exists for clients that cannot sign, cannot read a
membership back and has no refresh; this app can sign.

Requests go through a membership service, never straight to the
association. The association names the calling application by a client
key (``X-Api-Key``) that must stay secret, so it lives only on that
service (``sidecar/`` in this repository, run 24/7 by whoever publishes
a build: rinbal's server for official builds once it is deployed, your
own if you self-host).
The app never holds the key. Which service a build talks to is
:func:`service_url`; without one, or when the service says it has no
key, joining in the app is simply not offered.

What the app does add is a NIP-98 signature (``nostr/nip98.py``) naming
the end user, on every call but :meth:`MembershipApi.config`. Its ``u``
tag is the association's own URL for the request (that is what the
association verifies), even though the request travels to the service;
the service checks it too before it lends its key. The association
accepts each event id once and only within 60 seconds of its timestamp,
so every attempt builds and signs a FRESH event immediately before it is
sent, a retry included.

Signing goes through a plain callable rather than the bunker, so tests
can fake it and the window can adapt whatever signer it holds (see
:func:`session_signer`). With a NIP-46 phone signer each signed call may
be a prompt on the user's phone, which shapes two decisions: a slow
approval gets exactly one automatic re-sign (``client``), and payment
polling asks for one signature per check, not two (``watcher``).

The server is a third party. Its answers are parsed defensively and
every failure, malformed or not, reaches the caller as an
:class:`ApiError` through ``on_failure``; nothing raises into Qt.

The package is split along its seams, and this module re-exports every
public name, so ``nostr.einundzwanzig_api`` is imported as before:

- ``models``: the error codes, :class:`ApiError`, the records the API
  returns and their parsers;
- ``messages``: the user-facing words and the input checks;
- ``bolt11``: the amount a Lightning invoice asks for;
- ``client``: :class:`MembershipApi`, the service and upstream addresses,
  and the signer adapter;
- ``watcher``: :class:`PaymentWatcher`.
"""

from .bolt11 import bolt11_amount_sats
from .client import (
    API_PREFIX,
    BASE_URL,
    ENV_SERVICE_URL,
    ENV_UPSTREAM_URL,
    MAX_RESPONSE_BYTES,
    NETWORK_TIMEOUT_MS,
    SIGN_TIMEOUT_MS,
    STALE_SIGNATURE_SECONDS,
    STATUS_TIMEOUT_MS,
    UNSET,
    MembershipApi,
    SignFn,
    service_url,
    session_signer,
    upstream_url,
)
from .messages import (
    APPLICATION_TEXT_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    NIP05_HANDLE_MAX_LENGTH,
    application_text_problem,
    email_problem,
    field_message,
    handle_field_message,
    humanize,
    nip05_handle_problem,
)
from .models import (
    ERROR_CODES,
    MEMBERSHIP_STATUSES,
    STATUS_AWAITING_PAYMENT,
    STATUS_LAPSED,
    STATUS_MEMBER,
    STATUS_NONE,
    ApiError,
    CurrentYear,
    Erasure,
    ErrorCode,
    FeeEntry,
    Invoice,
    MembershipConfig,
    MembershipExport,
    MembershipStatus,
    parse_config,
    parse_erasure,
    parse_export,
    parse_fee_entry,
    parse_invoice,
    parse_membership,
    parse_payments,
    parse_retry_after,
)
from .watcher import POLL_ATTEMPTS, POLL_INTERVAL_MS, PaymentWatcher

__all__ = [
    "API_PREFIX", "APPLICATION_TEXT_MAX_LENGTH", "BASE_URL", "EMAIL_MAX_LENGTH",
    "ENV_SERVICE_URL", "ENV_UPSTREAM_URL", "ERROR_CODES", "MAX_RESPONSE_BYTES",
    "MEMBERSHIP_STATUSES", "NETWORK_TIMEOUT_MS", "NIP05_HANDLE_MAX_LENGTH",
    "POLL_ATTEMPTS", "POLL_INTERVAL_MS", "SIGN_TIMEOUT_MS", "STALE_SIGNATURE_SECONDS",
    "STATUS_AWAITING_PAYMENT", "STATUS_LAPSED", "STATUS_MEMBER", "STATUS_NONE",
    "STATUS_TIMEOUT_MS", "UNSET",
    "ApiError", "CurrentYear", "Erasure", "ErrorCode", "FeeEntry", "Invoice",
    "MembershipApi", "MembershipConfig", "MembershipExport", "MembershipStatus",
    "PaymentWatcher", "SignFn",
    "application_text_problem", "bolt11_amount_sats", "email_problem", "field_message",
    "handle_field_message", "humanize", "nip05_handle_problem", "parse_config",
    "parse_erasure", "parse_export", "parse_fee_entry", "parse_invoice",
    "parse_membership", "parse_payments", "parse_retry_after", "service_url",
    "session_signer", "upstream_url",
]
