# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How much fits through a signer app, and what happens when it does not.

Every NIP-46 request and answer is one NIP-44 message of at most 65,535
bytes, so a request that is too large cannot be sent, and a plaintext the
signer would encrypt into an answer too large to send gets no answer.
Both used to end badly: the first raised out of ``crypto.encrypt`` into a
caller waiting for an answer, the second waited out the whole timeout.
They now fail through the request's own ``on_failure``.
"""

from __future__ import annotations

import json
import secrets

from PySide6.QtCore import QCoreApplication

from nostr import bunker, crypto
from nostr.bunker import SIGNER_NIP44_MAX_PLAINTEXT
from tests.test_bunker_handshake import FakePool, FakeSigner

_CONV = bytes(range(32))


def _frame(payload: dict) -> int:
    """Bytes of a NIP-46 message as it is encrypted (compact, UTF-8)."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _ciphertext(size: int) -> str:
    return crypto.encrypt("x" * size, _CONV)


# -- the bound, measured with the real encryption ------------------------------

def test_the_largest_answer_to_nip44_encrypt_still_fits_one_message():
    answer = {"id": secrets.token_hex(8), "result": _ciphertext(SIGNER_NIP44_MAX_PLAINTEXT)}
    assert _frame(answer) <= crypto.NIP44_MAX_PLAINTEXT_LEN


def test_one_byte_more_makes_a_ciphertext_no_message_can_carry():
    # Padding jumps from 40,960 to 49,152 bytes: the ciphertext alone is
    # larger than a whole message, whatever is around it.
    assert len(_ciphertext(SIGNER_NIP44_MAX_PLAINTEXT + 1)) > crypto.NIP44_MAX_PLAINTEXT_LEN


def test_the_same_bound_holds_for_a_nip44_decrypt_request():
    def request(size: int) -> dict:
        return {"id": secrets.token_hex(8), "method": "nip44_decrypt",
                "params": ["ab" * 32, _ciphertext(size)]}

    assert _frame(request(SIGNER_NIP44_MAX_PLAINTEXT)) <= crypto.NIP44_MAX_PLAINTEXT_LEN
    assert _frame(request(SIGNER_NIP44_MAX_PLAINTEXT + 1)) > crypto.NIP44_MAX_PLAINTEXT_LEN


# -- a signer that encrypts, and answers in NIP-44 as every signer must --------

class EncryptingSigner(FakeSigner):
    """Answers ``nip44_encrypt`` with a real ciphertext. An answer too large
    for one NIP-44 message is never sent (nostr-tools throws), as with a
    real signer app."""

    def __init__(self, pool):
        super().__init__(pool)
        self.user_sk = crypto.generate_secret_key()
        self.user_pk = crypto.get_public_key(self.user_sk).hex()

    def _on_event(self, event):
        if event.get("pubkey") == self.pk:
            return
        try:
            payload = json.loads(crypto.decrypt(event["content"], self.conv))
        except (ValueError, json.JSONDecodeError, KeyError):
            payload = {}
        if payload.get("method") != "nip44_encrypt":
            super()._on_event(event)
            return
        self.seen.append(payload)
        peer, plaintext = payload["params"]
        ciphertext = crypto.encrypt_to(plaintext, self.user_sk, bytes.fromhex(peer))
        try:
            self.send({"id": payload["id"], "result": ciphertext})
        except ValueError:
            pass  # no message can carry this answer


def _turn() -> None:
    QCoreApplication.processEvents()


# -- requests that cannot be sent ------------------------------------------------

def _pair():
    """A client paired with an ``EncryptingSigner``."""
    pool = FakePool()
    signer = EncryptingSigner(pool)
    client = bunker.BunkerClient(pool)
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    outcome = {}
    client.connect_to_bunker(
        f"bunker://{signer.pk}?relay=wss%3A%2F%2Ffake&secret=abc",
        on_success=lambda pk: outcome.setdefault("ok", pk),
        on_failure=lambda r: outcome.setdefault("err", r),
        local_sk=local_sk,
    )
    assert outcome.get("ok") == signer.user_pk, outcome
    return client, signer


def test_an_oversized_request_fails_through_on_failure_instead_of_raising():
    client, signer = _pair()
    sent_before = len(signer.seen)
    unsigned = {"kind": 30023, "content": "x" * 70_000, "tags": [], "created_at": 1}
    outcome = {}

    client.sign_event(unsigned, on_success=lambda ev: outcome.setdefault("ok", ev),
                      on_failure=lambda r: outcome.setdefault("err", r))
    # Not inside the call: callers set up their waiting state after it.
    assert outcome == {}
    _turn()

    reason = outcome.get("err", "")
    assert "too large" in reason and "sign_event" in reason, outcome
    assert str(crypto.NIP44_MAX_PLAINTEXT_LEN) in reason
    # Nothing was sent, nothing is left waiting for an answer.
    assert len(signer.seen) == sent_before
    assert client._pending == {}
    # Asking again would be refused the same way, not taken for silence.
    assert bunker.is_signer_answer(reason) and not bunker.is_signer_silent(reason)
    client.close()


def test_nip44_encrypt_up_to_the_bound_is_answered():
    client, signer = _pair()
    plaintext = "x" * SIGNER_NIP44_MAX_PLAINTEXT
    outcome = {}

    client.nip44_encrypt_self(plaintext, on_success=lambda ct: outcome.setdefault("ok", ct),
                              on_failure=lambda r: outcome.setdefault("err", r))

    assert "ok" in outcome, outcome
    assert crypto.decrypt_from(outcome["ok"], signer.user_sk,
                               bytes.fromhex(signer.user_pk)) == plaintext
    client.close()


def test_nip44_encrypt_beyond_what_an_answer_carries_fails_at_once():
    client, signer = _pair()
    sent_before = len(signer.seen)
    outcome = {}

    # 20,481 two-byte characters: 40,962 bytes. The bound is in bytes.
    client.nip44_encrypt_self("ä" * 20_481,
                              on_success=lambda ct: outcome.setdefault("ok", ct),
                              on_failure=lambda r: outcome.setdefault("err", r))
    assert outcome == {}
    _turn()

    reason = outcome.get("err", "")
    assert "too large" in reason and "40962" in reason and str(SIGNER_NIP44_MAX_PLAINTEXT) in reason
    assert len(signer.seen) == sent_before
    assert client._pending == {}
    client.close()


def test_without_the_check_the_signer_never_answers():
    # What the early refusal saves: the request fits, the answer does not,
    # and the only outcome left would be the 30-second timeout.
    client, signer = _pair()
    outcome = {}
    plaintext = "x" * (SIGNER_NIP44_MAX_PLAINTEXT + 1)
    client._send_request(method="nip44_encrypt", params=[signer.user_pk, plaintext],
                         on_success=lambda ct: outcome.setdefault("ok", ct),
                         on_failure=lambda r: outcome.setdefault("err", r),
                         timeout_ms=60_000)
    assert signer.seen[-1]["method"] == "nip44_encrypt"
    _turn()
    assert outcome == {}
    assert len(client._pending) == 1
    client.close()
