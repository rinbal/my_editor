# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A request the signer client refuses before sending it (RG-M0 R6).

Every such refusal reaches ``on_failure`` exactly once, on the next turn
of the event loop, as a signer's own refusal would: never inside the
call, where a caller has not yet set up the state it waits in
(local_signer.py has the same shape). And it arrives even when the
client is gone by then, since a deleted context object would take the
refusal with it and leave the caller waiting for good.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication

from nostr import bech32, bunker, crypto, signer_limits
from tests.test_bunker_request_size import _pair

_app = QCoreApplication.instance() or QCoreApplication([])

_PEER = "ab" * 32


def _turn() -> None:
    QCoreApplication.processEvents()


class Calls:
    """Counts every answer a request gets, success or failure."""

    def __init__(self):
        self.ok = []
        self.failed = []

    def success(self, value=None):
        self.ok.append(value)

    def failure(self, reason):
        self.failed.append(reason)


def _unsigned(content: str = "hello") -> dict:
    return {"kind": 1, "content": content, "tags": [], "created_at": 1}


def _secret_content() -> str:
    return "my key: " + bech32.encode_nsec(crypto.generate_secret_key().hex())


# Requests to a client that is not connected.
_OFFLINE = {
    "sign_event": lambda c, k: c.sign_event(_unsigned(), k.success, k.failure),
    "nip44_encrypt": lambda c, k: c.nip44_encrypt(_PEER, "x", k.success, k.failure),
    "nip44_decrypt": lambda c, k: c.nip44_decrypt(_PEER, "AA", k.success, k.failure),
    "nip04_decrypt": lambda c, k: c.nip04_decrypt(_PEER, "AA", k.success, k.failure),
    "nip44_encrypt_self": lambda c, k: c.nip44_encrypt_self("x", k.success, k.failure),
    "nip44_decrypt_self": lambda c, k: c.nip44_decrypt_self("AA", k.success, k.failure),
    "nip04_decrypt_self": lambda c, k: c.nip04_decrypt_self("AA", k.success, k.failure),
    "connect_to_bunker": lambda c, k: c.connect_to_bunker("not a bunker uri", k.success,
                                                          k.failure),
    # RG-M2r2b 2: 64 hex digits that are no public key (about half are not).
    "connect_to_no_public_key": lambda c, k: c.connect_to_bunker(
        "bunker://" + "cd" * 32 + "?relay=wss://relay.example", k.success, k.failure),
}

# Requests a connected client refuses for what they carry.
_REFUSED = {
    "secret key": lambda c, k: c.sign_event(_unsigned(_secret_content()), k.success,
                                            k.failure),
    "encrypt to a bad peer": lambda c, k: c.nip44_encrypt("zz", "x", k.success, k.failure),
    "decrypt from a bad peer": lambda c, k: c.nip44_decrypt("zz", "AA", k.success, k.failure),
    "nip04 from a bad peer": lambda c, k: c.nip04_decrypt("zz", "AA", k.success, k.failure),
    "empty nip44 ciphertext": lambda c, k: c.nip44_decrypt(_PEER, "", k.success, k.failure),
    "empty nip04 ciphertext": lambda c, k: c.nip04_decrypt(_PEER, "", k.success, k.failure),
    "too large to encrypt": lambda c, k: c.nip44_encrypt(
        _PEER, "x" * (signer_limits.SIGNER_NIP44_MAX_PLAINTEXT + 1), k.success, k.failure),
    "too large to send": lambda c, k: c.sign_event(_unsigned("x" * 70_000), k.success,
                                                   k.failure),
}


@pytest.mark.parametrize("request_", list(_OFFLINE.values()), ids=list(_OFFLINE))
def test_an_offline_request_is_refused_once_on_the_next_turn(request_):
    client = bunker.BunkerClient(None)
    calls = Calls()
    request_(client, calls)
    assert calls.failed == [] and calls.ok == []
    _turn()
    assert len(calls.failed) == 1 and calls.ok == []
    _turn()
    assert len(calls.failed) == 1


@pytest.mark.parametrize("request_", list(_REFUSED.values()), ids=list(_REFUSED))
def test_a_request_refused_for_what_it_carries_is_refused_once_on_the_next_turn(request_):
    client, signer = _pair()
    sent = len(signer.seen)
    calls = Calls()
    request_(client, calls)
    assert calls.failed == [] and calls.ok == []
    _turn()
    assert len(calls.failed) == 1 and calls.ok == []
    assert len(signer.seen) == sent and client._pending == {}
    client.close()


@pytest.mark.parametrize("request_", list(_OFFLINE.values()), ids=list(_OFFLINE))
def test_the_refusal_arrives_when_the_client_is_gone_by_then(request_):
    client = bunker.BunkerClient(None)
    calls = Calls()
    request_(client, calls)
    shiboken6.delete(client)
    _turn()
    assert len(calls.failed) == 1


@pytest.mark.parametrize("request_", list(_REFUSED.values()), ids=list(_REFUSED))
def test_a_refusal_for_what_it_carries_arrives_when_the_client_is_gone(request_):
    client, _signer = _pair()
    calls = Calls()
    request_(client, calls)
    client.close()
    shiboken6.delete(client)
    _turn()
    assert len(calls.failed) == 1


def test_a_channel_that_cannot_be_opened_is_refused_never_waited_for(monkeypatch):
    # RG-M2r2b 2: a key the address check let through that still opens no
    # channel (ValueError in _setup_channel) is a refusal on the next turn,
    # with nothing subscribed and nothing pending, never a wait for an
    # answer that cannot come.
    from tests.test_bunker_handshake import FakePool
    monkeypatch.setattr(bunker.crypto, "is_public_key", lambda _key: True)
    pool = FakePool()
    client = bunker.BunkerClient(pool)
    calls = Calls()
    client.connect_to_bunker("bunker://" + "cd" * 32 + "?relay=wss://relay.example",
                             calls.success, calls.failure)
    assert calls.failed == []
    _turn()
    assert len(calls.failed) == 1 and calls.ok == [] and client._pending == {}
    assert not client.is_connected
