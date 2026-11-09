# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fakes for the STANDUP submission tests.

:class:`KeySigner` is a signer with the calls of LocalSigner and
BunkerClient (``sign_event``, ``nip44_encrypt``/``nip44_decrypt`` and
their ``_self`` forms) that answers at once, with a real key, so what it
signs verifies and what it encrypts decrypts.
"""

from __future__ import annotations

import json
from typing import Callable, List, Optional

from nostr import crypto, events


class KeySigner:
    """Signs and encrypts with ``secret`` and answers inside the call.

    ``refuse`` refuses every request with that reason; ``tamper`` edits a
    signed event before it is handed back. ``signed`` and ``asked`` record
    what was requested."""

    def __init__(self, secret: bytes, *, refuse: Optional[str] = None,
                 tamper: Optional[Callable[[dict], dict]] = None) -> None:
        self.secret = bytes(secret)
        self.user_pubkey = crypto.get_public_key(self.secret).hex()
        self.refuse = refuse
        self.tamper = tamper
        self.signed: List[dict] = []
        self.asked: List[str] = []
        self.is_connected = True

    def sign_event(self, unsigned: dict, on_success, on_failure, *, timeout_ms: int = 0) -> None:
        self.asked.append(f"sign_event:{unsigned.get('kind')}")
        if self.refuse is not None:
            on_failure(self.refuse)
            return
        event = events.sign_event(json.loads(json.dumps(unsigned)), self.secret)
        if self.tamper is not None:
            event = self.tamper(event)
        self.signed.append(event)
        on_success(event)

    def nip44_encrypt(self, peer: str, plaintext: str, on_success, on_failure, *,
                      timeout_ms: int = 0) -> None:
        self.asked.append("nip44_encrypt")
        if self.refuse is not None:
            on_failure(self.refuse)
            return
        on_success(crypto.encrypt_to(plaintext, self.secret, bytes.fromhex(peer)))

    def nip44_decrypt(self, peer: str, ciphertext: str, on_success, on_failure, *,
                      timeout_ms: int = 0) -> None:
        self.asked.append("nip44_decrypt")
        if self.refuse is not None:
            on_failure(self.refuse)
            return
        try:
            text = crypto.decrypt_from(ciphertext, self.secret, bytes.fromhex(peer))
        except ValueError as exc:
            on_failure(str(exc))
            return
        on_success(text)

    def nip44_encrypt_self(self, plaintext: str, on_success, on_failure, *,
                           timeout_ms: int = 0) -> None:
        self.nip44_encrypt(self.user_pubkey, plaintext, on_success, on_failure)

    def nip44_decrypt_self(self, ciphertext: str, on_success, on_failure, *,
                           timeout_ms: int = 0) -> None:
        self.nip44_decrypt(self.user_pubkey, ciphertext, on_success, on_failure)
