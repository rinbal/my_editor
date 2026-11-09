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

from nostr import bunker, crypto, events
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


# -- what a signer app can hand back (RG-M0 R7) -------------------------------------
#
# A signer app's answer carries its result as a JSON string, escaped. nak
# bunker 0.20.7 writes it with json-iterator (fiatjaf.com/nostr nip46,
# ``jsoniter.ConfigFastest``) and a signed event with easyjson: quotes and
# backslashes two bytes, \n \r \t two, every other control character six,
# <, >, & and U+2028 raw. JSON.stringify writes \b and \f in two.

def test_an_answer_is_measured_as_a_signer_app_writes_it():
    assert bunker._as_signer_writes('a"b\\c\n') == '"a\\"b\\\\c\\n"'
    assert bunker._as_signer_writes("\b\f\x01") == '"\\u0008\\u000c\\u0001"'
    assert bunker._as_signer_writes("<&>   ä") == '"<&>   ä"'
    # A literal backslash before a b is not an escaped backspace.
    assert bunker._as_signer_writes("\\b") == '"\\\\b"'
    # nak bunker returned a 32,000-byte page of nothing but quotes, and
    # not one of 32,800 (contract 13): the measure agrees with both.
    assert bunker.signer_answer_size('"' * 32_000) <= crypto.NIP44_MAX_PLAINTEXT_LEN
    assert bunker.signer_answer_size('"' * 32_800) > crypto.NIP44_MAX_PLAINTEXT_LEN


def test_what_can_be_read_back_depends_on_where_the_key_is():
    from types import SimpleNamespace
    remote, local = SimpleNamespace(signer="remote"), SimpleNamespace(signer="local")
    assert bunker.readable_back(remote, "x" * SIGNER_NIP44_MAX_PLAINTEXT)
    assert not bunker.readable_back(remote, "x" * (SIGNER_NIP44_MAX_PLAINTEXT + 1))
    # Encrypted fine, but the answer to nip44_decrypt would be 80,000 bytes.
    assert not bunker.readable_back(remote, '"' * 40_000)
    assert bunker.readable_back(remote, '"' * 32_000)
    # With the key here nothing is answered: NIP-44's own bound.
    assert bunker.readable_back(local, '"' * crypto.NIP44_MAX_PLAINTEXT_LEN)
    assert not bunker.readable_back(local, "x" * (crypto.NIP44_MAX_PLAINTEXT_LEN + 1))


class SigningSigner(EncryptingSigner):
    """Signs for real and answers compactly, as a signer app does; an
    answer too large for one NIP-44 message is never sent."""

    def _on_event(self, event):
        if event.get("pubkey") == self.pk:
            return
        try:
            payload = json.loads(crypto.decrypt(event["content"], self.conv))
        except (ValueError, json.JSONDecodeError, KeyError):
            payload = {}
        if payload.get("method") != "sign_event":
            super()._on_event(event)
            return
        self.seen.append(payload)
        signed = events.sign_event(json.loads(payload["params"][0]), self.user_sk)
        answer = bunker._as_signer_writes(
            {"id": payload["id"], "result": bunker._as_signer_writes(signed)})
        try:
            self.send_raw(crypto.encrypt(answer, self.conv))
        except ValueError:
            pass  # no message can carry this answer


def _pair_signing():
    pool = FakePool()
    signer = SigningSigner(pool)
    client = bunker.BunkerClient(pool)
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    outcome = {}
    client.connect_to_bunker(
        f"bunker://{signer.pk}?relay=wss%3A%2F%2Ffake&secret=abc",
        on_success=lambda pk: outcome.setdefault("ok", pk),
        on_failure=lambda r: outcome.setdefault("err", r), local_sk=local_sk)
    assert outcome.get("ok") == signer.user_pk, outcome
    return client, signer


# 65,143 bytes of content: the signed event's answer is exactly 65,535 bytes.
_LARGEST_SIGNABLE = 65_143


def _note(size: int) -> dict:
    return {"kind": 1, "content": "x" * size, "tags": [], "created_at": 1}


def test_the_largest_event_whose_signed_answer_fits_is_signed():
    client, signer = _pair_signing()
    answers, failures = [], []
    client.sign_event(_note(_LARGEST_SIGNABLE), answers.append, failures.append)
    _turn()
    assert failures == [] and len(answers) == 1
    assert len(answers[0]["content"]) == _LARGEST_SIGNABLE and events.verify_event(answers[0])
    client.close()


def test_an_event_whose_signed_answer_cannot_be_sent_is_refused_at_once():
    client, signer = _pair_signing()
    sent = len(signer.seen)
    answers, failures = [], []
    client.sign_event(_note(_LARGEST_SIGNABLE + 1), answers.append, failures.append)
    assert failures == []
    _turn()
    assert answers == [] and len(failures) == 1
    assert "sign_event" in failures[0] and "65536" in failures[0]
    assert len(signer.seen) == sent and client._pending == {}
    client.close()


def test_without_the_check_that_event_would_wait_out_the_timeout():
    # The request itself fits one message; only the answer does not.
    client, signer = _pair_signing()
    payload = json.dumps(_note(_LARGEST_SIGNABLE + 1), separators=(",", ":"))
    answers, failures = [], []
    client._send_request(method="sign_event", params=[payload], on_success=answers.append,
                         on_failure=failures.append, timeout_ms=60_000)
    assert signer.seen[-1]["method"] == "sign_event"
    _turn()
    assert answers == [] and failures == [] and len(client._pending) == 1
    client.close()
