# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A signer that uses a private key kept on this computer.

It answers the same calls as the remote signer client (nostr/bunker.py):
``sign_event``, ``nip44_encrypt`` / ``nip44_decrypt`` and their ``_self``
forms, with the same callbacks. So drafts, publishing, media and
membership work the same whether the key is in a phone app or here, and
none of them needs to know which.

Answers arrive on the next turn of the event loop, never inside the call,
because every caller was written against a signer that answers later.
Answering immediately would run their callbacks before the code that
follows the call, which is a different program.

``close()`` overwrites the key in memory: a signer that was signed out of,
or replaced by a signer app, holds no key any more.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QTimer

from nostr import crypto, events

LOCAL = "local"     # Profile.signer for accounts whose key is kept here


class LocalSigner(QObject):
    """Signs and encrypts with ``secret_key``, for its own public key only."""

    def __init__(self, secret_key: bytes, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sk = bytearray(secret_key)
        self._pubkey = crypto.get_public_key(bytes(self._sk)).hex()
        self._open = True

    # -- what callers read ------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._open

    @property
    def user_pubkey(self) -> str:
        return self._pubkey

    @property
    def relays(self) -> List[str]:
        return []

    # -- signing -----------------------------------------------------------------

    def sign_event(self, unsigned_event: dict, on_success: Callable[[dict], None],
                   on_failure: Callable[[str], None], *, timeout_ms: int = 0) -> None:
        def work():
            payload = {
                "kind": unsigned_event["kind"],
                "content": unsigned_event.get("content", ""),
                "tags": unsigned_event.get("tags", []),
                "created_at": int(unsigned_event["created_at"]),
            }
            return events.sign_event(payload, bytes(self._sk))
        self._answer(work, on_success, on_failure)

    def nip44_encrypt(self, peer_pubkey_hex: str, plaintext: str,
                      on_success: Callable[[str], None], on_failure: Callable[[str], None],
                      *, timeout_ms: int = 0) -> None:
        self._answer(lambda: crypto.encrypt_to(plaintext, bytes(self._sk),
                                               bytes.fromhex(peer_pubkey_hex)),
                     on_success, on_failure)

    def nip44_decrypt(self, peer_pubkey_hex: str, ciphertext_b64: str,
                      on_success: Callable[[str], None], on_failure: Callable[[str], None],
                      *, timeout_ms: int = 0) -> None:
        self._answer(lambda: crypto.decrypt_from(ciphertext_b64, bytes(self._sk),
                                                 bytes.fromhex(peer_pubkey_hex)),
                     on_success, on_failure)

    def nip44_encrypt_self(self, plaintext: str, on_success, on_failure, *,
                           timeout_ms: int = 0) -> None:
        self.nip44_encrypt(self._pubkey, plaintext, on_success, on_failure)

    def nip44_decrypt_self(self, ciphertext_b64: str, on_success, on_failure, *,
                           timeout_ms: int = 0) -> None:
        self.nip44_decrypt(self._pubkey, ciphertext_b64, on_success, on_failure)

    def close(self, reason: str = "closed by client") -> None:
        self._open = False
        for i in range(len(self._sk)):
            self._sk[i] = 0
        self._sk = bytearray()

    # -- plumbing ----------------------------------------------------------------

    def _answer(self, work, on_success, on_failure) -> None:
        def run():
            if not self._open:
                on_failure("not connected")
                return
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001, report, never raise into Qt
                on_failure(str(exc) or "could not complete the request")
                return
            on_success(result)
        QTimer.singleShot(0, run)
