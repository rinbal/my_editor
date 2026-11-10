# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-46 (Nostr Connect) client over the RelayPool.

Spec: https://github.com/nostr-protocol/nips/blob/master/46.md

Wire transport:
  - Both directions ride kind 24133 events
  - Content is the JSON-RPC payload encrypted with NIP-44 v2
  - "p" tag identifies the recipient (which way the message is going)

Flow for the bunker:// pairing this module handles:
  1. Editor parses the URI → (bunker_pubkey, relays, secret?)
  2. Editor mints an ephemeral local keypair just for this NIP-46 channel.
  3. Editor subscribes to the bunker's relays, filtered to events authored
     by the bunker pubkey and addressed to the local pubkey.
  4. Editor sends a "connect" request with the secret + requested perms.
  5. Bunker validates the secret and responds with "ack" (or echoes the
     secret back per spec). Either is success.
  6. Editor sends "get_public_key" to learn the user's actual Nostr pubkey
     (distinct from the bunker pubkey, which is only a relay-facing
     identity).
  7. ``connected`` signal fires with the user pubkey.

Subsequent ``sign_event`` calls follow the same JSON-RPC dance, caller
gets a callback when the signed event arrives (or a failure callback on
timeout/error).
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import _

from . import CLIENT_NAME, CLIENT_URL, crypto, events, failures, refs
from .relay import KeySignIn, RelayPool, Subscription
from .answer_size import OWN_REQUEST_ID_BYTES, request_json
from .signer_limits import (
    bound_known, connection_answer_limit, connection_message_limit,
    connection_plaintext_limit, sign_answer_size,
)


# --------------------------------------------------------------------------- #
# Defaults                                                                     #
# --------------------------------------------------------------------------- #

# Bunker URI pairing usually requires the user to approve on their phone,
# so the connect-call timeout is generous.
DEFAULT_CONNECT_TIMEOUT_MS = 90_000

# Subsequent get_public_key / sign_event calls should be fast, the channel
# is already up, the signer just has to compute.
DEFAULT_RPC_TIMEOUT_MS = 30_000

# ``ping`` is the one call no signer asks a human about, so the only thing
# it waits on is a relay round-trip. Amber answers it (since 4.0.3, and
# grants it on connect since 6.2.0), as do Primal, Amethyst, Aegis and
# Bunker46 (research/signer-pairing.md, section 1). What stays silent is a
# signer that is asleep (an Android app the system stopped, Primal on an
# iPhone without its background sound), an Amber older than 4.0.3, or one
# that drops a method it has no handler for, as nsec.app does with any it
# does not know (whether it answers ping was not verified). Giving ping the
# full RPC budget meant such a signer burned 30 seconds before
# ``reattach`` even tried the fallback that would have worked. This is a
# liveness probe: if it has not come back in a few seconds, the answer we
# need is somewhere else.
PING_TIMEOUT_MS = 6_000

# A signer that answered anything this recently is taken to be there
# (``check_alive``): asking again only to find out would cost a signer that
# ignores ping six seconds, right after it proved itself.
ANSWERED_RECENTLY_MS = 30_000

# A signer may answer any request with an "auth_url" challenge instead of a
# result, meaning the user has to authenticate in a browser first. The real
# answer arrives later under the same request id, so the pending request is
# held open this long rather than failing.
AUTH_CHALLENGE_TIMEOUT_MS = 180_000

# The failure reasons that mean nobody answered, as opposed to the signer
# answering "no". Defined here, next to the code that produces them, so
# the callers that have to recognise them cannot drift from the wording.
SIGNER_SILENT_NEEDLES: Tuple[str, ...] = (
    "timed out waiting for signer",
    "could not deliver request to any relay",
)


# The failure reasons this client gives itself when its session to the
# signer app is not there (not connected yet, closed, reopened, dropped):
# the signer app was never asked, so nothing it would say is known.
SESSION_ERROR_NEEDLES: Tuple[str, ...] = (
    "not connected",
    "user pubkey not yet known",
    "closed by client",
    "reopening channel",
    "dropped from session pool",
    "application shutdown",
)


def is_signer_silent(reason: str) -> bool:
    """True if ``reason`` means the signer never answered."""
    lowered = (reason or "").lower()
    return any(needle in lowered for needle in SIGNER_SILENT_NEEDLES)


def is_signer_answer(reason: str) -> bool:
    """True if ``reason`` is the signer app's own answer (a refusal, an
    error it sent), rather than silence or this client's session not being
    there: asking again would be answered the same way."""
    lowered = (reason or "").lower()
    return not is_signer_silent(lowered) and not any(
        needle in lowered for needle in SESSION_ERROR_NEEDLES)


# What the person is told when a signer app did not do what was asked:
# one plain sentence per kind of reason, never the reason itself (lesson
# 26); the reason is for Details, the tooltip and the log.
_DECLINED = ("reject", "denied", "declin", "not allowed", "unauthori", "forbidden",
             "permission", "unknown client", "refus")


def declined_by_signer(reason: str) -> bool:
    """True if ``reason`` is the signer app saying no."""
    lowered = (reason or "").lower()
    return any(needle in lowered for needle in _DECLINED)


def signer_failure_words(reason: str) -> str:
    """The one sentence for a request the signer app did not do, its
    likely cause and what to do (lessons 36), whatever ``reason`` says:
    it never answered (or was not reached), it declined, or it answered
    with an error of its own."""
    lowered = (reason or "").lower()
    if is_signer_silent(lowered) or any(needle in lowered for needle in SESSION_ERROR_NEEDLES):
        return _(failures.SIGNER_SILENT)
    if declined_by_signer(lowered):
        return _(failures.SIGNER_DECLINED)
    return _(failures.SIGNER_FAILED)


def pairing_failure_words(reason: str) -> str:
    """The one sentence for a pairing that did not work, its likely cause
    and what to do (lessons 36), whatever ``reason`` says: the signer app
    was silent, declined, answered for another connection, or something
    else went wrong."""
    lowered = (reason or "").lower()
    if is_signer_silent(lowered):
        return _(failures.SIGNER_SILENT)
    if declined_by_signer(lowered):
        return _(failures.SIGNER_DECLINED)
    if "secret" in lowered:
        return _("The answer was meant for another connection: try again.")
    return _("The connection didn’t work: try again, or connect another way.")


# Where MyEditor and a signer app meet to pair (NIP-46 nostrconnect).
# Transport only, never anyone's home relay.
#
# The ones that state no small content cap come first, at least two of
# them (contract 12.5; their NIP-11 documents, fetched 2026-11-10):
# relay.primal.net and relay.damus.io take messages of 1,000,000 bytes,
# nos.lol and nrs.primal.net ("Primal Nostr Remote Signer Relay") 131,072;
# none states a content cap and none asks for payment. nrs.primal.net is
# one of Amber's own default relays (AmberSettings.kt at 621c445a, lines 8
# to 13: auth.nostr1.com, bucket.coracle.social, nrs.primal.net,
# relay.nip46.com); relay.primal.net is not one of them. relay.nsec.app
# comes last: nsec.app answers only there, whatever relays the link names,
# so without it nsec.app could never reach MyEditor (it publishes no
# NIP-11 document).
# theforest.nostr1.com is not one of them: its document says
# payment_required and lists fees, so a channel key that has not paid
# cannot rely on writing a kind 24133 there.
NIP46_RELAYS: tuple = (
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nrs.primal.net",
    "wss://relay.nsec.app",
)


# Permissions we request at connect time. Comma-separated per spec.
#
#   sign_event:1      short notes
#   sign_event:30023  long-form articles
#   sign_event:31234  NIP-37 draft wraps (private encrypted drafts)
#   nip44_encrypt     encrypting draft payloads to the user's own pubkey
#   nip44_decrypt     decrypting drafts pulled back from relays
#   nip04_decrypt     reading a list of sources an older app saved
#   get_public_key    required for the post-connect user-pubkey lookup
#   ping              keepalive
#   sign_event:22242  signing in to the account's relays that ask (NIP-42)
#   sign_event:13     sealing a private message (NIP-59)
#
# Signers that don't recognise an entry typically ignore it silently;
# the actual capability check happens lazily when we invoke each method.
DEFAULT_PERMS = (
    "get_public_key,"
    "sign_event:1,sign_event:30023,sign_event:31234,"
    # Proof of identity for EINUNDZWANZIG membership requests (NIP-98), and
    # adding the members' relay to the user's relay list (NIP-65).
    "sign_event:27235,sign_event:10002,"
    # Publishing the profile of an account created or edited in MyEditor.
    "sign_event:0,"
    # Media servers: proving who uploads, lists or deletes a file
    # (BUD-11), and the list of servers the account uses (BUD-03).
    "sign_event:24242,sign_event:10063,"
    # The synced feed list (NIP-78) and where drafts are kept (NIP-37).
    "sign_event:30078,sign_event:10013,"
    # Signing in to the account's own relays that ask for it (NIP-42).
    "sign_event:22242,"
    # Sealing a message for a publication's editors (NIP-59): the seal is
    # the one part of a gift wrap the sender's own key signs. The list is
    # sent only when pairing, so a signer paired before this was added
    # decides at the first seal whether to ask the person or refuse.
    "sign_event:13,"
    "nip44_encrypt,nip44_decrypt,"
    # Reading a list of sources an older app saved with NIP-04 (never
    # used to encrypt anything).
    "nip04_decrypt,"
    "ping"
)


# --------------------------------------------------------------------------- #
# URI parsing                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class BunkerURI:
    bunker_pubkey: str          # hex, 64 chars
    relays: List[str]           # one or more wss:// URLs
    secret: Optional[str]       # optional pairing token


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_user_key(value: str) -> bool:
    """Whether ``value`` is a key as a signer names one: 64 lowercase hex
    characters that are a point of the curve (a public key, BIP-340);
    about half of all 64-digit values are not."""
    return _is_hex64(value) and crypto.is_public_key(bytes.fromhex(value))


def parse_bunker_uri(uri: str) -> BunkerURI:
    """Parse a bunker:// URI per NIP-46.

    Format:
        bunker://<remote-signer-pubkey>?relay=<wss://...>&relay=<wss://...>&secret=<...>

    Raises ValueError with a human-readable message on any malformedness.
    """
    if not isinstance(uri, str):
        raise ValueError("bunker URI must be a string")
    uri = uri.strip()
    if not uri.startswith("bunker://"):
        raise ValueError(_("expected a bunker:// URI"))

    parts = urlsplit(uri)
    pubkey = (parts.netloc or parts.path.lstrip("/")).strip().lower()
    if not _is_hex64(pubkey):
        raise ValueError(_("bunker URI must contain a 32-byte hex pubkey"))
    # One typed wrong often is no point of the curve: no channel can be
    # opened to it.
    if not _is_user_key(pubkey):
        raise ValueError("the signer's key in the bunker URI is not a public key")

    qs = parse_qs(parts.query, keep_blank_values=False)
    relays = [r.strip() for r in qs.get("relay", []) if r.strip()]
    if not relays:
        raise ValueError(_("bunker URI must specify at least one relay"))
    for relay in relays:
        if not (relay.startswith("wss://") or relay.startswith("ws://")):
            raise ValueError(_("relay must be ws:// or wss://, got {relay}").format(
                relay=repr(relay)))

    secret_values = qs.get("secret", [])
    secret = secret_values[0] if secret_values else None

    return BunkerURI(bunker_pubkey=pubkey, relays=relays, secret=secret)


def build_nostrconnect_uri(
    local_pubkey_hex: str,
    relays: List[str],
    secret: str,
    *,
    name: str = CLIENT_NAME,
    perms: str = DEFAULT_PERMS,
    url: str = CLIENT_URL,
    image: str = "",
) -> str:
    """Build a NIP-46 ``nostrconnect://`` URI from the client's keypair.

    Format:
        nostrconnect://<client-pubkey>?relay=<wss://...>&secret=<...>&perms=<...>&name=<...>
            &url=<...>&image=<...>

    ``url`` is MyEditor's own page by default: Amber, Amethyst, Aegis and
    nsec.app show the app's name and page where the person approves the
    connection, and nsec.app refuses a link without any of name, url and
    image (research/signer-pairing.md, section 2). ``image`` is optional
    in NIP-46 and left out by default: no signer was seen to show it, and
    without it the code is small enough to scan at three pixels a module
    (nostr/qr.py).

    Multiple ``relay`` parameters are appended in order. URL-encoding is
    applied so signers parsing the URI with a standard query-string
    parser get back the original values.
    """
    if len(local_pubkey_hex) != 64 or not all(c in "0123456789abcdef" for c in local_pubkey_hex.lower()):
        raise ValueError("local pubkey must be 64 hex chars")
    if not relays:
        raise ValueError("at least one relay is required")
    if not secret:
        raise ValueError("a non-empty secret is required to prevent connection spoofing")

    parts: List[str] = []
    for relay in relays:
        parts.append(f"relay={quote(relay, safe='')}")
    parts.append(f"secret={quote(secret, safe='')}")
    parts.append(f"perms={quote(perms, safe=',:')}")
    if name:
        parts.append(f"name={quote(name, safe='')}")
    if url:
        parts.append(f"url={quote(url, safe='')}")
    if image:
        parts.append(f"image={quote(image, safe='')}")
    return f"nostrconnect://{local_pubkey_hex.lower()}?" + "&".join(parts)


# --------------------------------------------------------------------------- #
# Pending-request bookkeeping                                                  #
# --------------------------------------------------------------------------- #

def connect_handshake_secret(payload: dict) -> Optional[str]:
    """Pull the pairing secret out of a signer's ``connect`` handshake.

    NIP-46 has the remote signer answer a ``nostrconnect://`` QR with a
    connect *response* whose ``result`` is the secret. Signers predating
    that wording instead send a connect *request* carrying the secret in
    ``params``. Both shapes are read here so either signer can pair.

    Returns None when the payload is not a connect handshake at all, which
    is different from a handshake carrying the wrong secret.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("method") == "connect":
        params = payload.get("params")
        if isinstance(params, list) and len(params) >= 2 and isinstance(params[1], str):
            return params[1]
        return ""  # a connect request with no secret, which cannot match
    result = payload.get("result")
    # "auth_url" is a challenge, not an answer, and "ack" carries no proof
    # of who sent it. Neither may stand in for the secret.
    if isinstance(result, str) and result and result not in ("auth_url", "ack"):
        return result
    return None


@dataclass
class _Pending:
    method: str
    on_success: Callable[[str], None]   # receives the raw result string
    on_failure: Callable[[str], None]   # receives a human-readable reason
    timer: QTimer


# --------------------------------------------------------------------------- #
# BunkerClient                                                                #
# --------------------------------------------------------------------------- #

class BunkerClient(QObject):
    """One NIP-46 channel to one remote signer.

    Lifecycle:
      ``connect_to_bunker`` (paste flow) OR ``reattach`` (silent reconnect
      on app launch using a saved profile).
      Then ``sign_event`` for each event we want signed.
      Finally ``close`` to release the relay subscription.

    Signals:
      connected(user_pubkey_hex)  after connect + get_public_key succeed
      disconnected(reason)        channel torn down
    """

    connected = Signal(str)
    disconnected = Signal(str)
    # A signer wants the user to authenticate at this URL before it answers.
    auth_challenge = Signal(str)
    # A signer replied on our channel but we could not read the message.
    # Surfaced so the UI can say so instead of showing a bare timeout.
    unreadable_reply = Signal(str)

    def __init__(self, pool: RelayPool, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool

        # Set during connect_to_bunker / reattach.
        self._local_sk: Optional[bytes] = None
        self._local_pk_hex: Optional[str] = None
        self._bunker_pk_hex: Optional[str] = None
        self._relays: List[str] = []
        self._conv_key: Optional[bytes] = None
        self._user_pubkey: Optional[str] = None

        self._subscription: Optional[Subscription] = None
        self._pending: Dict[str, _Pending] = {}
        self._is_connected = False
        self._warned_unreadable = False
        # When the signer last answered anything (time.monotonic()).
        self._answered_at: Optional[float] = None

        # nostrconnect:// state, only used while listen_for_nostrconnect
        # is the active flow.
        self._nc_secret: Optional[str] = None
        self._nc_on_success: Optional[Callable[[str], None]] = None
        self._nc_on_failure: Optional[Callable[[str], None]] = None
        self._nc_timer: Optional[QTimer] = None

    # -- public properties --------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def user_pubkey(self) -> Optional[str]:
        return self._user_pubkey

    @property
    def bunker_pubkey(self) -> Optional[str]:
        return self._bunker_pk_hex

    @property
    def relays(self) -> List[str]:
        return list(self._relays)

    @property
    def local_secret_hex(self) -> Optional[str]:
        return self._local_sk.hex() if self._local_sk else None

    # -- high-level: pair via bunker:// URI --------------------------------

    def connect_to_bunker(
        self,
        uri: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
        local_sk: Optional[bytes] = None,
    ) -> None:
        """Pair with a remote signer by parsing the URI, opening relays,
        sending connect, then resolving the user's pubkey.

        ``local_sk`` is for testing or replay; production callers should
        omit it and let the client mint a fresh ephemeral keypair.
        """
        try:
            parsed = parse_bunker_uri(uri)
            self._setup_channel(
                bunker_pubkey=parsed.bunker_pubkey,
                relays=parsed.relays,
                local_sk=local_sk,
            )
        except ValueError as exc:
            # A key that is no point of the curve, or another address no
            # channel can be opened to: refused, never a wait for nothing.
            self._refuse(on_failure, str(exc))
            return
        self._send_request(
            method="connect",
            params=[parsed.bunker_pubkey, parsed.secret or "", DEFAULT_PERMS],
            on_success=lambda result: self._on_connect_acked(result, parsed.secret, on_success, on_failure),
            on_failure=on_failure,
            timeout_ms=timeout_ms,
        )

    # -- high-level: passive nostrconnect:// listener ----------------------

    def listen_for_nostrconnect(
        self,
        relays: List[str],
        secret: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
        local_sk: Optional[bytes] = None,
    ) -> str:
        """Open a channel waiting for a signer to scan a nostrconnect:// QR.

        Generates an ephemeral keypair (or uses ``local_sk`` for tests),
        subscribes to ``relays`` for kind 24133 events addressed to us,
        and waits for an incoming ``connect`` request from any signer.
        On a successful handshake (correct secret + valid ``get_public_key``
        response), the channel transitions into normal signing mode
        identical to the bunker:// path.

        Returns the local pubkey (hex) so the caller can build the QR.
        """
        self._local_sk = local_sk or crypto.generate_secret_key()
        self._local_pk_hex = crypto.get_public_key(self._local_sk).hex()
        self._relays = list(relays)
        # The bunker pubkey + conv key aren't known until the signer
        # actually contacts us.
        self._bunker_pk_hex = None
        self._conv_key = None
        self._user_pubkey = None
        self._nc_secret = secret
        self._nc_on_success = on_success
        self._nc_on_failure = on_failure

        if self._subscription is not None:
            self._subscription.close()

        # Filter is loose by necessity, we don't know the signer's
        # pubkey yet, so we accept any kind 24133 addressed to us.
        self._subscription = self._pool.subscribe(
            self._relays,
            filters=[{"kinds": [24133], "#p": [self._local_pk_hex]}],
            sign_in=self._channel_sign_in(),
        )
        self._subscription.event.connect(self._on_nostrconnect_event)

        self._nc_timer = QTimer(self)
        self._nc_timer.setSingleShot(True)
        self._nc_timer.setInterval(timeout_ms)
        self._nc_timer.timeout.connect(self._on_nostrconnect_timeout)
        self._nc_timer.start()

        return self._local_pk_hex

    def _on_nostrconnect_event(self, event: dict) -> None:
        """Wait for the signer's ``connect`` handshake, ignore everything
        else until the channel is established."""
        signer_pk_hex = event.get("pubkey", "")
        if not isinstance(signer_pk_hex, str) or len(signer_pk_hex) != 64:
            return
        try:
            conv_key = crypto.conversation_key(
                self._local_sk, bytes.fromhex(signer_pk_hex)
            )
            plaintext = crypto.decrypt(event.get("content", ""), conv_key)
            payload = json.loads(plaintext)
        except (ValueError, json.JSONDecodeError):
            # Addressed to us but unreadable, so a signer is very likely
            # talking a dialect we do not speak. Silence here is what makes
            # the failure look like "nothing happened".
            self._note_unreadable_reply(signer_pk_hex)
            return

        received_secret = connect_handshake_secret(payload)
        if received_secret is None:
            return
        if received_secret != self._nc_secret:
            # Wrong secret, so a spoof attempt. Stay listening, the real
            # signer may still scan the QR. Don't fail the caller.
            return

        # Channel established. Lock the signer's pubkey + conv key in
        # place and switch the subscription's routing to the normal
        # response handler so future events (e.g. sign_event replies)
        # land in _on_response_event.
        self._bunker_pk_hex = signer_pk_hex
        self._conv_key = conv_key
        try:
            self._subscription.event.disconnect(self._on_nostrconnect_event)
        except (RuntimeError, TypeError):
            pass
        self._subscription.event.connect(self._on_response_event)
        self._nc_timer.stop()

        # Only a connect *request* is owed an answer. When the signer sent a
        # response instead, acking it would be replying to a reply.
        request_id = payload.get("id", "")
        if payload.get("method") == "connect" and isinstance(request_id, str) and request_id:
            self._send_response(request_id, "ack")

        # Now resolve the user's actual pubkey (distinct from the
        # signer's relay-facing pubkey we just captured).
        on_success_cb = self._nc_on_success
        on_failure_cb = self._nc_on_failure

        self._send_request(
            method="get_public_key",
            params=[],
            on_success=lambda pk_hex: self._got_user_key(pk_hex, on_success_cb, on_failure_cb),
            on_failure=on_failure_cb,
            timeout_ms=DEFAULT_RPC_TIMEOUT_MS,
        )

    def _got_user_key(self, pk_hex: str, on_success: Callable[[str], None],
                      on_failure: Callable[[str], None]) -> None:
        """The signer's answer to ``get_public_key``: the account's key,
        64 hex characters that are a point of the curve (a public key,
        BIP-340), or the pairing fails and the channel closes."""
        pk_hex = pk_hex.strip().lower()
        if not _is_user_key(pk_hex):
            on_failure(f"signer returned malformed user pubkey: {pk_hex!r}")
            self.close(reason="bad user pubkey")
            return
        self._user_pubkey = pk_hex
        self._is_connected = True
        self.connected.emit(pk_hex)
        on_success(pk_hex)

    def _on_nostrconnect_timeout(self) -> None:
        if self._is_connected or self._bunker_pk_hex is not None:
            return  # signer arrived in the gap before this fired
        if self._nc_on_failure is not None:
            self._nc_on_failure("timed out waiting for a signer to scan the QR")

    def _send_response(self, request_id: str, result: str) -> None:
        """Send a NIP-46 response back to the signer (no pending state).

        Used for the ``"ack"`` reply during the nostrconnect handshake,
        the signer sent us the connect request, so we owe them an answer.
        Fire-and-forget; publish acknowledgement isn't surfaced.
        """
        if self._conv_key is None or self._bunker_pk_hex is None:
            return
        inner = json.dumps(
            {"id": request_id, "result": result},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        ciphertext = crypto.encrypt(inner, self._conv_key)
        event = events.build_event(
            kind=24133,
            content=ciphertext,
            tags=[["p", self._bunker_pk_hex]],
            sk=self._local_sk,
        )
        self._pool.publish(self._relays, event, sign_in=self._channel_sign_in())

    # -- high-level: silently re-attach from a saved profile ---------------

    def reattach(
        self,
        bunker_pubkey: str,
        relays: List[str],
        local_sk: bytes,
        user_pubkey: str,
        on_success: Callable[[], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Re-open the channel for an already-paired profile.

        The ``connect`` request is skipped and the channel is proven live
        with a ``ping``. Not every signer answers one, so a failed ping
        falls back to ``get_public_key``, which every signer implements and
        which we already hold permission for. That fallback also re-checks
        the identity: if the signer has since switched accounts, saying so
        beats publishing under a pubkey the user no longer means to use.
        """
        self._setup_channel(
            bunker_pubkey=bunker_pubkey,
            relays=relays,
            local_sk=local_sk,
        )
        self._user_pubkey = user_pubkey

        def _ok(_result: str) -> None:
            self._is_connected = True
            self.connected.emit(user_pubkey)
            on_success()

        def _confirm_identity(pk_hex: str) -> None:
            if pk_hex.strip().lower() != user_pubkey.strip().lower():
                on_failure(
                    "the signer is now holding a different account than this "
                    "profile was paired with"
                )
                self.close(reason="identity changed")
                return
            _ok("pong")

        self._probe(_ok, _confirm_identity, on_failure, timeout_ms)

    def check_alive(
        self,
        on_answer: Callable[[], None],
        on_silence: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Find out whether the signer app is there before asking it for
        something that waits on a person or on a 30-second timeout.

        Asked the way ``reattach`` proves the channel, with questions no
        signer asks a person about. Any reply counts, a refusal too: someone
        is there to give it. ``on_silence`` gets the reason when nothing came
        back. A signer that answered within ``ANSWERED_RECENTLY_MS`` is not
        asked at all.
        """
        recently = self._answered_at is not None and (
            time.monotonic() - self._answered_at) * 1000 < ANSWERED_RECENTLY_MS
        if recently:
            # On a later turn, as every answer arrives.
            QTimer.singleShot(0, self, on_answer)
            return

        def _failed(reason: str) -> None:
            if is_signer_answer(reason):
                on_answer()
            else:
                on_silence(reason)

        self._probe(lambda _pong: on_answer(), lambda _pk: on_answer(), _failed, timeout_ms)

    def _probe(
        self,
        on_ping: Callable[[str], None],
        on_public_key: Callable[[str], None],
        on_failure: Callable[[str], None],
        timeout_ms: int,
    ) -> None:
        """Prove the channel: ``ping``, then ``get_public_key`` if the ping
        fails. Not every signer answers ping (one asleep, an Amber before
        4.0.3, one that drops methods it has no handler for: see
        PING_TIMEOUT_MS); every signer implements ``get_public_key``, and we
        already hold permission for it."""

        def _ping_failed(_reason: str) -> None:
            self._send_request(
                method="get_public_key",
                params=[],
                on_success=on_public_key,
                on_failure=on_failure,
                timeout_ms=timeout_ms,
            )

        self._send_request(
            method="ping",
            params=[],
            on_success=on_ping,
            on_failure=_ping_failed,
            # The probe gets a short leash so the fallback that actually
            # works is reached quickly. ``min`` so a caller asking for an
            # even tighter budget still gets it.
            timeout_ms=min(PING_TIMEOUT_MS, timeout_ms),
        )

    # -- high-level: sign an event ------------------------------------------

    def sign_event(
        self,
        unsigned_event: dict,
        on_success: Callable[[dict], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Hand an unsigned event to the remote signer.

        The unsigned event needs ``kind``, ``content``, ``tags``,
        ``created_at``, but NOT ``id``, ``pubkey``, or ``sig``. The
        signer fills those in.
        """
        if not self._is_connected:
            self._refuse(on_failure, "not connected")
            return
        refusal = refs.signing_refusal(unsigned_event)
        if refusal:
            self._refuse(on_failure, refusal)
            return

        payload = {
            "kind": unsigned_event["kind"],
            "content": unsigned_event.get("content", ""),
            "tags": unsigned_event.get("tags", []),
            "created_at": int(unsigned_event["created_at"]),
        }
        # The answer is the signed event, escaped as a string: our request
        # may fit while the answer, with the id, pubkey and signature the
        # signer adds, does not, and we would wait out the timeout. Over
        # relays that all cap what they carry, less fits (contract 12.5).
        # What the person is told comes earlier (signer_limits.size_problem).
        answer, limit = sign_answer_size(payload), connection_answer_limit(self._relays)
        if answer > limit:
            self._refuse(on_failure,
                         f"too large for a signer app: sign_event would answer with up to "
                         f"{answer} bytes, an answer over these relays may carry at most "
                         f"{limit}")
            return

        def _ok(result: str) -> None:
            try:
                signed = json.loads(result)
            except json.JSONDecodeError:
                on_failure("signer returned non-JSON result")
                return
            if not isinstance(signed, dict):
                on_failure("signer returned non-event result")
                return
            if not events.verify_event(signed):
                on_failure("signer returned an event with an invalid signature")
                return
            on_success(signed)

        self._send_request(
            method="sign_event",
            params=[json.dumps(payload, separators=(",", ":"), ensure_ascii=False)],
            on_success=_ok,
            on_failure=on_failure,
            timeout_ms=timeout_ms,
        )

    # -- high-level: NIP-44 encrypt / decrypt via the signer ---------------
    #
    # NIP-46 §"Methods" defines ``nip44_encrypt`` and ``nip44_decrypt``:
    #   params: [third_party_pubkey_hex, plaintext_or_ciphertext]
    #   result: the encrypted (base64) or decrypted (UTF-8) string
    #
    # For NIP-37 self-encrypted drafts the third-party pubkey is the
    # user's own pubkey, we expose ``nip44_encrypt_self`` /
    # ``nip44_decrypt_self`` as convenience wrappers to make that
    # intent explicit at the call site.

    def nip44_encrypt(
        self,
        peer_pubkey_hex: str,
        plaintext: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Ask the signer to NIP-44 encrypt ``plaintext`` to ``peer_pubkey_hex``.

        ``on_success`` receives the base64-encoded NIP-44 payload.
        ``on_failure`` receives a human-readable reason, including the
        case where the signer rejects the method as unknown (older
        signers without NIP-44 support).
        """
        if not self._is_connected:
            self._refuse(on_failure, "not connected")
            return
        peer = peer_pubkey_hex.strip().lower()
        if len(peer) != 64 or not all(c in "0123456789abcdef" for c in peer):
            self._refuse(on_failure, f"invalid peer pubkey: {peer_pubkey_hex!r}")
            return
        size = len(plaintext.encode("utf-8"))
        limit = connection_plaintext_limit(self._relays)
        if size > limit:
            # The request itself may still fit, but the answer cannot: the
            # signer would encrypt and then have no message to send it in,
            # and we would wait out the whole timeout for nothing. Over
            # relays that all cap what they carry, less fits (contract 12.5).
            self._refuse(
                on_failure,
                f"too large to encrypt through a signer app: {size} bytes, "
                f"at most {limit} over these relays",
            )
            return

        def _ok(result: str) -> None:
            if not result:
                on_failure("signer returned empty ciphertext")
                return
            on_success(result)

        self._send_request(
            method="nip44_encrypt",
            params=[peer, plaintext],
            on_success=_ok,
            on_failure=on_failure,
            timeout_ms=timeout_ms,
        )

    def nip44_decrypt(
        self,
        peer_pubkey_hex: str,
        ciphertext_b64: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Ask the signer to NIP-44 decrypt ``ciphertext_b64`` from ``peer_pubkey_hex``."""
        if not self._is_connected:
            self._refuse(on_failure, "not connected")
            return
        peer = peer_pubkey_hex.strip().lower()
        if len(peer) != 64 or not all(c in "0123456789abcdef" for c in peer):
            self._refuse(on_failure, f"invalid peer pubkey: {peer_pubkey_hex!r}")
            return
        if not ciphertext_b64:
            self._refuse(on_failure, "ciphertext is empty")
            return

        def _ok(result: str) -> None:
            # Some signers return "" on permission denial instead of a
            # proper error frame. Symmetrise with ``nip44_encrypt`` which
            # rejects empty ciphertext, and give the caller a clear reason
            # rather than letting downstream JSON parsing fail with a
            # misleading "not valid JSON".
            if not result:
                on_failure("signer returned empty plaintext")
                return
            on_success(result)

        self._send_request(
            method="nip44_decrypt",
            params=[peer, ciphertext_b64],
            on_success=_ok,
            on_failure=on_failure,
            timeout_ms=timeout_ms,
        )

    def nip04_decrypt(
        self,
        peer_pubkey_hex: str,
        content: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Ask the signer to read a NIP-04 message from ``peer_pubkey_hex``
        (NIP-46 ``nip04_decrypt``): what an older app saved. Nothing is ever
        encrypted with NIP-04 here."""
        if not self._is_connected:
            self._refuse(on_failure, "not connected")
            return
        peer = peer_pubkey_hex.strip().lower()
        if len(peer) != 64 or not all(c in "0123456789abcdef" for c in peer):
            self._refuse(on_failure, f"invalid peer pubkey: {peer_pubkey_hex!r}")
            return
        if not content:
            self._refuse(on_failure, "ciphertext is empty")
            return

        def _ok(result: str) -> None:
            if not result:
                on_failure("signer returned empty plaintext")
                return
            on_success(result)

        self._send_request(
            method="nip04_decrypt",
            params=[peer, content],
            on_success=_ok,
            on_failure=on_failure,
            timeout_ms=timeout_ms,
        )

    def nip04_decrypt_self(
        self,
        content: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Read a NIP-04 message the account sent to itself."""
        if self._user_pubkey is None:
            self._refuse(on_failure, "user pubkey not yet known")
            return
        self.nip04_decrypt(self._user_pubkey, content, on_success, on_failure,
                           timeout_ms=timeout_ms)

    def nip44_encrypt_self(
        self,
        plaintext: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Self-encrypt, the NIP-37 case. Peer pubkey is the user's own."""
        if self._user_pubkey is None:
            self._refuse(on_failure, "user pubkey not yet known")
            return
        self.nip44_encrypt(
            self._user_pubkey,
            plaintext,
            on_success,
            on_failure,
            timeout_ms=timeout_ms,
        )

    def nip44_decrypt_self(
        self,
        ciphertext_b64: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Self-decrypt, the NIP-37 case. Peer pubkey is the user's own."""
        if self._user_pubkey is None:
            self._refuse(on_failure, "user pubkey not yet known")
            return
        self.nip44_decrypt(
            self._user_pubkey,
            ciphertext_b64,
            on_success,
            on_failure,
            timeout_ms=timeout_ms,
        )

    # -- low-level: lifecycle ----------------------------------------------

    def close(self, reason: str = "closed by client") -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if self._nc_timer is not None:
            self._nc_timer.stop()
            self._nc_timer = None
        # Snapshot AND clear pending before invoking callbacks. A callback
        # that re-enters by sending a fresh request would otherwise either
        # have its new entry wiped by the trailing ``.clear()`` (silently
        # dropping the new request) or be skipped by a subsequent
        # iteration over a mutated dict.
        pending_snapshot = list(self._pending.values())
        self._pending.clear()
        for pending in pending_snapshot:
            pending.timer.stop()
            try:
                pending.on_failure(reason)
            except Exception:  # noqa: BLE001, never let one callback break the rest
                pass
        was_connected = self._is_connected
        self._is_connected = False
        # Clear the user pubkey too: a closed channel has no identity
        # context, and stale ``_user_pubkey`` would let ``nip44_*_self``
        # silently target the wrong key on a future reopen.
        self._user_pubkey = None
        if was_connected:
            self.disconnected.emit(reason)

    # -- internals ---------------------------------------------------------

    def _setup_channel(
        self,
        bunker_pubkey: str,
        relays: List[str],
        local_sk: Optional[bytes],
    ) -> None:
        """Generate (or accept) the local keypair, derive conv key,
        open the response subscription. Idempotent within one client."""
        if self._subscription is not None:
            self.close(reason="reopening channel")

        self._local_sk = local_sk or crypto.generate_secret_key()
        self._local_pk_hex = crypto.get_public_key(self._local_sk).hex()
        self._bunker_pk_hex = bunker_pubkey.lower()
        self._relays = list(relays)
        self._conv_key = crypto.conversation_key(
            self._local_sk, bytes.fromhex(self._bunker_pk_hex)
        )

        # Subscribe to responses BEFORE publishing the connect request,
        # otherwise a fast signer could reply before we're listening.
        self._subscription = self._pool.subscribe(
            self._relays,
            filters=[{
                "kinds": [24133],
                "authors": [self._bunker_pk_hex],
                "#p": [self._local_pk_hex],
            }],
            sign_in=self._channel_sign_in(),
        )
        self._subscription.event.connect(self._on_response_event)

    def _channel_sign_in(self) -> KeySignIn:
        """A relay of the channel that asks for a sign-in gets the
        channel's own key: the one it is asked to deliver messages to,
        which says nothing about the account."""
        assert self._local_sk is not None
        return KeySignIn(self._local_sk)

    def _on_connect_acked(
        self,
        result: str,
        expected_secret: Optional[str],
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        # Per spec: result is "ack" or the echoed secret. If we sent a secret,
        # we MUST validate it. We accept "ack" either way for compatibility
        # with signers that respond uniformly.
        if expected_secret and result != "ack" and result != expected_secret:
            on_failure(
                "signer responded but the secret did not match (possible spoof attempt)"
            )
            self.close(reason="secret mismatch")
            return

        # Now learn the user's actual pubkey.
        self._send_request(
            method="get_public_key",
            params=[],
            on_success=lambda pk_hex: self._got_user_key(pk_hex, on_success, on_failure),
            on_failure=on_failure,
            timeout_ms=DEFAULT_RPC_TIMEOUT_MS,
        )

    def _send_request(
        self,
        method: str,
        params: List[str],
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        timeout_ms: int,
    ) -> str:
        assert self._local_sk is not None and self._conv_key is not None
        assert self._bunker_pk_hex is not None

        request_id = secrets.token_hex(OWN_REQUEST_ID_BYTES)
        inner = request_json(request_id, method, params)
        size = len(inner.encode("utf-8"))
        limit = connection_message_limit(self._relays)
        if size > limit:
            # A request is one NIP-44 message, so a larger one cannot be
            # sent at all. ``crypto.encrypt`` would raise, and the caller,
            # written for an answer that comes later, would never hear of
            # it: a draft or a list that never finishes saving. Over
            # relays that all cap what they carry, one that is too long
            # for them would never arrive (contract 12.5).
            self._refuse(
                on_failure,
                f"request too large for a signer app: {method} needs "
                f"{size} bytes, a NIP-46 message over these relays carries at most "
                f"{limit}",
            )
            return request_id
        ciphertext = crypto.encrypt(inner, self._conv_key)
        event = events.build_event(
            kind=24133,
            content=ciphertext,
            tags=[["p", self._bunker_pk_hex]],
            sk=self._local_sk,
        )

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(timeout_ms)
        timer.timeout.connect(lambda rid=request_id: self._on_timeout(rid))
        self._pending[request_id] = _Pending(method, on_success, on_failure, timer)
        timer.start()

        publish_job = self._pool.publish(self._relays, event, timeout_ms=8000,
                                         sign_in=self._channel_sign_in())
        publish_job.all_done.connect(
            lambda results, rid=request_id: self._on_publish_done(rid, results)
        )
        return request_id

    def _on_publish_done(self, request_id: str, results: list) -> None:
        if request_id not in self._pending:
            return  # already responded or timed out
        if not any(ok for _, ok, _ in results):
            reasons = "; ".join(f"{url}: {msg}" for url, _, msg in results) or "no relays available"
            self._fail(request_id, f"could not deliver request to any relay ({reasons})")

    def _on_response_event(self, event: dict) -> None:
        if self._conv_key is None:
            return
        try:
            plaintext = crypto.decrypt(event.get("content", ""), self._conv_key)
            payload = json.loads(plaintext)
        except (ValueError, json.JSONDecodeError):
            self._note_unreadable_reply(event.get("pubkey", ""))
            return
        if not isinstance(payload, dict):
            return

        request_id = payload.get("id")
        if not isinstance(request_id, str) or request_id not in self._pending:
            return
        self._answered_at = time.monotonic()

        error = payload.get("error")
        result = payload.get("result")

        if result == "auth_url":
            # Not an answer but a challenge: the user has to authenticate in
            # a browser, after which the real answer arrives under this same
            # id. Hold the request open rather than failing it.
            url = str(error or "").strip()
            if url:
                pending = self._pending.get(request_id)
                if pending is not None:
                    pending.timer.start(AUTH_CHALLENGE_TIMEOUT_MS)
                self.auth_challenge.emit(url)
                return
            self._fail(request_id, "signer asked for authentication but sent no address")
            return

        if error:
            self._fail(request_id, str(error))
        elif isinstance(result, str):
            self._succeed(request_id, result)
        else:
            self._fail(request_id, "signer response missing both result and error")

    def _note_unreadable_reply(self, signer_pk_hex: str) -> None:
        """Report, once per channel, that a reply arrived we could not read.

        Payloads are NIP-44 encrypted per NIP-46. A signer using the older
        NIP-04 encoding lands here, and without this the only symptom is a
        timeout that looks identical to the signer never answering at all.
        """
        if self._warned_unreadable:
            return
        self._warned_unreadable = True
        self.unreadable_reply.emit(signer_pk_hex)

    def _on_timeout(self, request_id: str) -> None:
        self._fail(request_id, "timed out waiting for signer")

    @staticmethod
    def _refuse(on_failure: Callable[[str], None], reason: str) -> None:
        """Refuse a request that is never sent: every such refusal, on the
        next turn of the event loop, as a signer's own refusal would
        arrive. A caller that sets up its waiting state after the call
        (local_signer.py says why that is the shape every caller has)
        then sees the failure after that state, not before it. Not bound
        to this client (as local_signer.py's answers are not): a client
        closed and deleted in between must not take the refusal with
        it, or its caller waits for good."""
        QTimer.singleShot(0, lambda: on_failure(reason))

    def _succeed(self, request_id: str, result: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        pending.timer.stop()
        pending.on_success(result)

    def _fail(self, request_id: str, reason: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        pending.timer.stop()
        pending.on_failure(reason)


# --------------------------------------------------------------------------- #
# BunkerSessionPool: one signer per profile, cached process-wide             #
# --------------------------------------------------------------------------- #

class BunkerSessionPool(QObject):
    """Lazy cache of signers keyed by user pubkey.

    A profile that signs with a signer app gets a ``BunkerClient``: the
    first ``get(profile, …)`` creates one and triggers a ``reattach``
    handshake, and on success the client is memoized and reused for every
    later call, with no extra WebSocket handshakes per publish. A profile
    whose key is kept on this computer gets a ``LocalSigner``.

    The cached signer is for the way the profile signed when it was made.
    When that changes (an account paired with a signer app is restored
    with its key, or a local account is paired with a signer app), the
    old signer is dropped instead of being handed out, so nothing keeps
    signing the old way.

    Closing the pool tears down every signer (used at app shutdown).

    ``relay_caps`` (nostr/relay_caps.RelayCapsReader) reads what the
    relays of a connection carry while it reattaches, and the client is
    handed out once both are done, so its first request is bounded by
    them (contract 12.5). Without one nothing is read: the bounds are
    those of relays that state no cap, and of the ones the contract names.
    """

    def __init__(self, pool: RelayPool, parent: Optional[QObject] = None, *,
                 vault=None, relay_caps=None) -> None:
        super().__init__(parent)
        self._pool = pool
        # Keys kept on this computer, for profiles that sign locally.
        self._vault = vault
        self._relay_caps = relay_caps
        # pubkey -> BunkerClient or LocalSigner, and how that one signs.
        self._clients: Dict[str, QObject] = {}
        self._signers: Dict[str, str] = {}
        # pubkey -> the client whose reattach is in flight, and the
        # callbacks waiting on it.
        self._attaching: Dict[str, BunkerClient] = {}
        self._inflight: Dict[str, List[Tuple[Callable[[QObject], None], Callable[[str], None]]]] = {}

    def read_bounds(self, profile, done: Callable[[], None]) -> None:
        """``done()`` on a later turn once what ``profile``'s signer
        connection carries is known (signer_limits.bound_known): its
        relays' documents read, nothing else waited for, not the signer
        reattaching (RG-M2r2b 13). Without a reader the bounds are those
        named and stated, known at once."""
        if self._relay_caps is None or bound_known(profile):
            QTimer.singleShot(0, done)
            return
        self._relay_caps.read(list(profile.bunker_relays or ()), done)

    def key_here(self, user_pubkey: str) -> bool:
        """Whether the account's private key is kept on this computer
        (also when it signs through a signer app): what the words about a
        signer app's limit offer (``signer_limits.size_problem``)."""
        return self._key_vault().has(user_pubkey)

    def _key_vault(self):
        from .key_vault import KeyVault
        return self._vault if self._vault is not None else KeyVault()

    def get(
        self,
        profile,  # nostr.profiles.Profile, not imported to avoid a cycle
        on_ready: Callable[[QObject], None],
        on_error: Callable[[str], None],
    ) -> None:
        from .local_signer import LOCAL
        pubkey = profile.user_pubkey
        signer = LOCAL if getattr(profile, "signer", "remote") == LOCAL else "remote"
        if pubkey in self._clients and self._signers.get(pubkey) != signer:
            self.drop(pubkey)
        client = self._clients.get(pubkey)
        if client is not None and client.is_connected:
            on_ready(client)
            return

        if signer == LOCAL:
            self._get_local(pubkey, on_ready, on_error)
            return

        # Coalesce: if a reattach is already in flight for this profile,
        # piggy-back on it instead of opening a parallel channel.
        waiters = self._inflight.get(pubkey)
        if waiters is not None:
            waiters.append((on_ready, on_error))
            return

        try:
            local_sk = bytes.fromhex(profile.local_secret_hex)
        except ValueError:
            on_error("saved local secret is malformed; please re-connect the signer")
            return

        self._inflight[pubkey] = [(on_ready, on_error)]
        new_client = BunkerClient(self._pool, parent=self)
        self._attaching[pubkey] = new_client

        def current() -> bool:
            # A reattach that was given up on (the account signs locally
            # now, or was dropped) must not come back as the cached client.
            # It is left to finish rather than closed mid-handshake, and
            # closed here when it does.
            if self._attaching.get(pubkey) is new_client:
                del self._attaching[pubkey]
                return True
            new_client.close(reason="no longer wanted")
            new_client.deleteLater()
            return False

        # Two things before the client is handed out: the reattach, and
        # the bound of its relays (when they are read; reading always
        # ends, at the latest when it times out). Whichever comes second
        # hands it out; a failed reattach ends it whatever the reading.
        waiting = {"attached": False, "bounded": self._relay_caps is None, "out": False}

        def _bounded() -> None:
            waiting["bounded"] = True
            if waiting["attached"]:
                _ready()

        def _ok() -> None:
            waiting["attached"] = True
            if waiting["bounded"]:
                _ready()

        def _ready() -> None:
            if waiting["out"]:
                return
            waiting["out"] = True
            if not current():
                return
            self._clients[pubkey] = new_client
            self._signers[pubkey] = "remote"
            for ready_cb, _err_cb in self._inflight.pop(pubkey, []):
                try:
                    ready_cb(new_client)
                except Exception:  # noqa: BLE001, never let one waiter's bug break another
                    pass

        def _err(reason: str) -> None:
            if not current():
                return
            # Gone for good: drafts that wait for a signer app ask again
            # every minute, each time with a client of its own.
            new_client.close(reason=reason)
            new_client.deleteLater()
            for _ready_cb, err_cb in self._inflight.pop(pubkey, []):
                try:
                    err_cb(reason)
                except Exception:  # noqa: BLE001
                    pass

        if self._relay_caps is not None:
            self._relay_caps.read(list(profile.bunker_relays), _bounded)
        try:
            new_client.reattach(
                bunker_pubkey=profile.bunker_pubkey,
                relays=list(profile.bunker_relays),
                local_sk=local_sk,
                user_pubkey=profile.user_pubkey,
                on_success=_ok,
                on_failure=_err,
            )
        except Exception as exc:  # noqa: BLE001
            # A saved pairing that cannot even start (a malformed key) must
            # fail every waiter now, not leave them waiting for good.
            _err(f"the saved signer pairing could not be opened: {exc}")

    def _get_local(self, pubkey: str, on_ready, on_error) -> None:
        """A key kept on this computer: no channel to open, no phone to
        ask. The same calls are answered by a LocalSigner."""
        from .local_signer import LOCAL, LocalSigner
        # A reattach started while the account still signed with an app
        # is given up; whoever waited on it gets the local signer instead.
        waiters = [(on_ready, on_error)] + self._inflight.pop(pubkey, [])
        self._attaching.pop(pubkey, None)
        secret = self._key_vault().load(pubkey)
        if secret is None:
            for _ready_cb, err_cb in waiters:
                err_cb("the private key for this account is not on this computer")
            return
        local = LocalSigner(secret, parent=self)
        self._clients[pubkey] = local
        self._signers[pubkey] = LOCAL
        for ready_cb, _err_cb in waiters:
            ready_cb(local)

    def drop(self, user_pubkey: str) -> None:
        """Forget the profile's signer, and any reattach still in flight."""
        client = self._clients.pop(user_pubkey, None)
        self._signers.pop(user_pubkey, None)
        if client is not None:
            client.close(reason="dropped from session pool")
            client.deleteLater()
        self._attaching.pop(user_pubkey, None)
        for _ready_cb, err_cb in self._inflight.pop(user_pubkey, []):
            try:
                err_cb("dropped from session pool")
            except Exception:  # noqa: BLE001
                pass

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close(reason="application shutdown")
            client.deleteLater()
        self._clients.clear()
        self._signers.clear()
        self._attaching.clear()
        self._inflight.clear()
