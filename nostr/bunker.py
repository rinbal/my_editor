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

Subsequent ``sign_event`` calls follow the same JSON-RPC dance — caller
gets a callback when the signed event arrives (or a failure callback on
timeout/error).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlsplit

from PySide6.QtCore import QObject, QTimer, Signal

from . import CLIENT_NAME, crypto, events
from .relay import RelayPool, Subscription


# --------------------------------------------------------------------------- #
# Defaults                                                                     #
# --------------------------------------------------------------------------- #

# Bunker URI pairing usually requires the user to approve on their phone,
# so the connect-call timeout is generous.
DEFAULT_CONNECT_TIMEOUT_MS = 90_000

# Subsequent get_public_key / sign_event calls should be fast — the channel
# is already up, the signer just has to compute.
DEFAULT_RPC_TIMEOUT_MS = 30_000

# Permissions we request at connect time. Comma-separated per spec.
#
#   sign_event:1      — short notes
#   sign_event:30023  — long-form articles
#   sign_event:31234  — NIP-37 draft wraps (private encrypted drafts)
#   nip44_encrypt     — encrypting draft payloads to the user's own pubkey
#   nip44_decrypt     — decrypting drafts pulled back from relays
#   get_public_key    — required for the post-connect user-pubkey lookup
#   ping              — keepalive
#
# Signers that don't recognise an entry typically ignore it silently;
# the actual capability check happens lazily when we invoke each method.
DEFAULT_PERMS = (
    "get_public_key,"
    "sign_event:1,sign_event:30023,sign_event:31234,"
    "nip44_encrypt,nip44_decrypt,"
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
        raise ValueError("expected a bunker:// URI")

    parts = urlsplit(uri)
    pubkey = (parts.netloc or parts.path.lstrip("/")).strip().lower()
    if len(pubkey) != 64 or not all(c in "0123456789abcdef" for c in pubkey):
        raise ValueError("bunker URI must contain a 32-byte hex pubkey")

    qs = parse_qs(parts.query, keep_blank_values=False)
    relays = [r.strip() for r in qs.get("relay", []) if r.strip()]
    if not relays:
        raise ValueError("bunker URI must specify at least one relay")
    for relay in relays:
        if not (relay.startswith("wss://") or relay.startswith("ws://")):
            raise ValueError(f"relay must be ws:// or wss://, got {relay!r}")

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
    url: str = "",
    image: str = "",
) -> str:
    """Build a NIP-46 ``nostrconnect://`` URI from the client's keypair.

    Format:
        nostrconnect://<client-pubkey>?relay=<wss://...>&secret=<...>&perms=<...>&name=<...>

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
      connected(user_pubkey_hex)  — after connect + get_public_key succeed
      disconnected(reason)        — channel torn down
    """

    connected = Signal(str)
    disconnected = Signal(str)

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

        # nostrconnect:// state — only used while listen_for_nostrconnect
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
        except ValueError as exc:
            on_failure(str(exc))
            return
        self._setup_channel(
            bunker_pubkey=parsed.bunker_pubkey,
            relays=parsed.relays,
            local_sk=local_sk,
        )
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

        # Filter is loose by necessity — we don't know the signer's
        # pubkey yet, so we accept any kind 24133 addressed to us.
        self._subscription = self._pool.subscribe(
            self._relays,
            filters=[{"kinds": [24133], "#p": [self._local_pk_hex]}],
        )
        self._subscription.event.connect(self._on_nostrconnect_event)

        self._nc_timer = QTimer(self)
        self._nc_timer.setSingleShot(True)
        self._nc_timer.setInterval(timeout_ms)
        self._nc_timer.timeout.connect(self._on_nostrconnect_timeout)
        self._nc_timer.start()

        return self._local_pk_hex

    def _on_nostrconnect_event(self, event: dict) -> None:
        """Look for an incoming ``connect`` request; ignore everything else
        until the channel is established."""
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
            return  # not for us, or malformed

        if not isinstance(payload, dict) or payload.get("method") != "connect":
            return

        params = payload.get("params")
        received_secret = (
            params[1] if isinstance(params, list) and len(params) >= 2 else ""
        )
        if received_secret != self._nc_secret:
            # Wrong secret — spoof attempt. Stay listening; the real
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

        # Reply with "ack" so the signer knows we accepted.
        request_id = payload.get("id", "")
        if isinstance(request_id, str) and request_id:
            self._send_response(request_id, "ack")

        # Now resolve the user's actual pubkey (distinct from the
        # signer's relay-facing pubkey we just captured).
        on_success_cb = self._nc_on_success
        on_failure_cb = self._nc_on_failure

        def _got_user_pk(pk_hex: str) -> None:
            pk_hex = pk_hex.strip().lower()
            if len(pk_hex) != 64 or not all(c in "0123456789abcdef" for c in pk_hex):
                on_failure_cb(f"signer returned malformed user pubkey: {pk_hex!r}")
                self.close(reason="bad user pubkey")
                return
            self._user_pubkey = pk_hex
            self._is_connected = True
            self.connected.emit(pk_hex)
            on_success_cb(pk_hex)

        self._send_request(
            method="get_public_key",
            params=[],
            on_success=_got_user_pk,
            on_failure=on_failure_cb,
            timeout_ms=DEFAULT_RPC_TIMEOUT_MS,
        )

    def _on_nostrconnect_timeout(self) -> None:
        if self._is_connected or self._bunker_pk_hex is not None:
            return  # signer arrived in the gap before this fired
        if self._nc_on_failure is not None:
            self._nc_on_failure("timed out waiting for a signer to scan the QR")

    def _send_response(self, request_id: str, result: str) -> None:
        """Send a NIP-46 response back to the signer (no pending state).

        Used for the ``"ack"`` reply during the nostrconnect handshake —
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
        self._pool.publish(self._relays, event)

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

        We skip the ``connect`` request and verify the channel is live by
        sending a ``ping``. If the ping comes back, we trust the saved
        user_pubkey and emit ``connected``.
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

        self._send_request(
            method="ping",
            params=[],
            on_success=_ok,
            on_failure=on_failure,
            timeout_ms=timeout_ms,
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
        ``created_at`` — but NOT ``id``, ``pubkey``, or ``sig``. The
        signer fills those in.
        """
        if not self._is_connected:
            on_failure("not connected")
            return

        payload = {
            "kind": unsigned_event["kind"],
            "content": unsigned_event.get("content", ""),
            "tags": unsigned_event.get("tags", []),
            "created_at": int(unsigned_event["created_at"]),
        }

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
    # user's own pubkey — we expose ``nip44_encrypt_self`` /
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
        ``on_failure`` receives a human-readable reason — including the
        case where the signer rejects the method as unknown (older
        signers without NIP-44 support).
        """
        if not self._is_connected:
            on_failure("not connected")
            return
        peer = peer_pubkey_hex.strip().lower()
        if len(peer) != 64 or not all(c in "0123456789abcdef" for c in peer):
            on_failure(f"invalid peer pubkey: {peer_pubkey_hex!r}")
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
            on_failure("not connected")
            return
        peer = peer_pubkey_hex.strip().lower()
        if len(peer) != 64 or not all(c in "0123456789abcdef" for c in peer):
            on_failure(f"invalid peer pubkey: {peer_pubkey_hex!r}")
            return
        if not ciphertext_b64:
            on_failure("ciphertext is empty")
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

    def nip44_encrypt_self(
        self,
        plaintext: str,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
        *,
        timeout_ms: int = DEFAULT_RPC_TIMEOUT_MS,
    ) -> None:
        """Self-encrypt — the NIP-37 case. Peer pubkey is the user's own."""
        if self._user_pubkey is None:
            on_failure("user pubkey not yet known")
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
        """Self-decrypt — the NIP-37 case. Peer pubkey is the user's own."""
        if self._user_pubkey is None:
            on_failure("user pubkey not yet known")
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
            except Exception:  # noqa: BLE001 — never let one callback break the rest
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
        )
        self._subscription.event.connect(self._on_response_event)

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
        def _got_pk(pk_hex: str) -> None:
            pk_hex = pk_hex.strip().lower()
            if len(pk_hex) != 64 or not all(c in "0123456789abcdef" for c in pk_hex):
                on_failure(f"signer returned malformed user pubkey: {pk_hex!r}")
                self.close(reason="bad user pubkey")
                return
            self._user_pubkey = pk_hex
            self._is_connected = True
            self.connected.emit(pk_hex)
            on_success(pk_hex)

        self._send_request(
            method="get_public_key",
            params=[],
            on_success=_got_pk,
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

        request_id = secrets.token_hex(8)
        inner = json.dumps(
            {"id": request_id, "method": method, "params": params},
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

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(timeout_ms)
        timer.timeout.connect(lambda rid=request_id: self._on_timeout(rid))
        self._pending[request_id] = _Pending(method, on_success, on_failure, timer)
        timer.start()

        publish_job = self._pool.publish(self._relays, event, timeout_ms=8000)
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
            return  # not for us, or malformed — ignore quietly
        if not isinstance(payload, dict):
            return

        request_id = payload.get("id")
        if not isinstance(request_id, str) or request_id not in self._pending:
            return

        error = payload.get("error")
        result = payload.get("result")
        if error:
            self._fail(request_id, str(error))
        elif isinstance(result, str):
            self._succeed(request_id, result)
        else:
            self._fail(request_id, "signer response missing both result and error")

    def _on_timeout(self, request_id: str) -> None:
        self._fail(request_id, "timed out waiting for signer")

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
# BunkerSessionPool — one connected client per profile, cached process-wide   #
# --------------------------------------------------------------------------- #

class BunkerSessionPool(QObject):
    """Lazy cache of ``BunkerClient`` instances keyed by user pubkey.

    The first ``get(profile, …)`` call for a profile creates a fresh
    client and triggers a ``reattach`` handshake (sends ``ping``). On
    success the client is memoized and reused for subsequent ``sign_event``
    calls — no extra WebSocket handshakes per publish.

    Closing the pool tears down every active client (used at app shutdown).
    """

    def __init__(self, pool: RelayPool, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._clients: Dict[str, BunkerClient] = {}
        # pubkey -> pending callbacks waiting on the first reattach to finish.
        self._inflight: Dict[str, List[tuple[Callable[[BunkerClient], None], Callable[[str], None]]]] = {}

    def get(
        self,
        profile,  # nostr.profiles.Profile — avoid circular import
        on_ready: Callable[[BunkerClient], None],
        on_error: Callable[[str], None],
    ) -> None:
        pubkey = profile.user_pubkey
        client = self._clients.get(pubkey)
        if client is not None and client.is_connected:
            on_ready(client)
            return

        # Coalesce: if a reattach is already in flight for this profile,
        # piggy-back on it instead of opening a parallel channel.
        waiters = self._inflight.get(pubkey)
        if waiters is not None:
            waiters.append((on_ready, on_error))
            return
        self._inflight[pubkey] = [(on_ready, on_error)]

        new_client = BunkerClient(self._pool, parent=self)

        def _ok() -> None:
            self._clients[pubkey] = new_client
            for ready_cb, _err_cb in self._inflight.pop(pubkey, []):
                try:
                    ready_cb(new_client)
                except Exception:  # noqa: BLE001 — never let one waiter's bug break another
                    pass

        def _err(reason: str) -> None:
            new_client.close(reason=reason)
            for _ready_cb, err_cb in self._inflight.pop(pubkey, []):
                try:
                    err_cb(reason)
                except Exception:  # noqa: BLE001
                    pass

        try:
            local_sk = bytes.fromhex(profile.local_secret_hex)
        except ValueError:
            self._inflight.pop(pubkey, None)
            on_error("saved local secret is malformed; please re-connect the signer")
            return

        new_client.reattach(
            bunker_pubkey=profile.bunker_pubkey,
            relays=list(profile.bunker_relays),
            local_sk=local_sk,
            user_pubkey=profile.user_pubkey,
            on_success=_ok,
            on_failure=_err,
        )

    def drop(self, user_pubkey: str) -> None:
        client = self._clients.pop(user_pubkey, None)
        if client is not None:
            client.close(reason="dropped from session pool")

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close(reason="application shutdown")
        self._clients.clear()
        self._inflight.clear()
