# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the NIP-46 pairing handshake against a signer that follows the spec.

Every one of these failed as a silent timeout before, which is the worst
possible shape for a login bug: the app looks like it is still waiting and
the user has nothing to report but "it does not work".

The fake pool delivers synchronously, so no event loop pumping is needed
and the assertions describe exactly one round trip.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal

from nostr import crypto, events
from nostr.bunker import BunkerClient, connect_handshake_secret


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


# --------------------------------------------------------------------- #
# Fakes                                                                  #
# --------------------------------------------------------------------- #

class _Sub(QObject):
    event = Signal(dict)

    def __init__(self, filters):
        super().__init__()
        self.filters = filters
        self.closed = False

    def close(self):
        self.closed = True


class _Job(QObject):
    all_done = Signal(list)
    first_accept = Signal(str)
    relay_result = Signal(str, bool, str)


class FakePool(QObject):
    """Routes published events straight to every matching subscription."""

    def __init__(self):
        super().__init__()
        self.subs = []

    def subscribe(self, urls, filters, sub_id=None):
        sub = _Sub(filters)
        self.subs.append(sub)
        return sub

    def publish(self, urls, event, timeout_ms=8000):
        for sub in list(self.subs):
            if sub.closed:
                continue
            if any(_matches(f, event) for f in sub.filters):
                sub.event.emit(event)
        return _Job()


def _matches(f, event):
    if "kinds" in f and event.get("kind") not in f["kinds"]:
        return False
    if "authors" in f and event.get("pubkey") not in f["authors"]:
        return False
    if "#p" in f:
        ps = [t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == "p"]
        if not any(p in f["#p"] for p in ps):
            return False
    return True


class FakeSigner:
    """A remote signer that answers exactly as NIP-46 describes."""

    def __init__(self, pool):
        self.pool = pool
        self.sk = crypto.generate_secret_key()
        self.pk = crypto.get_public_key(self.sk).hex()
        self.user_pk = crypto.get_public_key(crypto.generate_secret_key()).hex()
        self.seen = []
        self.auth_url_for_connect = None
        self.withhold_after_auth = False
        self.refuse_ping = False
        self.client_pk = None

    def watch(self, client_pk):
        self.client_pk = client_pk
        self.conv = crypto.conversation_key(self.sk, bytes.fromhex(client_pk))
        sub = self.pool.subscribe([], [{"kinds": [24133], "#p": [self.pk]}])
        sub.event.connect(self._on_event)
        self._sub = sub

    def _on_event(self, event):
        if event.get("pubkey") == self.pk:
            return
        try:
            payload = json.loads(crypto.decrypt(event["content"], self.conv))
        except (ValueError, json.JSONDecodeError):
            return
        if "method" not in payload:
            return
        self.seen.append(payload)
        rid = payload.get("id")
        method = payload["method"]

        if method == "connect" and self.auth_url_for_connect is not None:
            url = self.auth_url_for_connect
            self.auth_url_for_connect = None
            self.send({"id": rid, "result": "auth_url", "error": url})
            if not self.withhold_after_auth:
                self.send({"id": rid, "result": "ack"})
            return

        if method == "connect":
            self.send({"id": rid, "result": "ack"})
        elif method == "get_public_key":
            self.send({"id": rid, "result": self.user_pk})
        elif method == "ping":
            if self.refuse_ping:
                self.send({"id": rid, "error": "unknown method"})
            else:
                self.send({"id": rid, "result": "pong"})

    def send(self, payload):
        self.send_raw(crypto.encrypt(json.dumps(payload), self.conv))

    def send_raw(self, content):
        ev = events.build_event(
            kind=24133, content=content,
            tags=[["p", self.client_pk]], sk=self.sk,
        )
        self.pool.publish([], ev)

    def open_channel_to(self, client_pk):
        """Used for the QR flow, where the signer speaks first."""
        self.client_pk = client_pk
        self.conv = crypto.conversation_key(self.sk, bytes.fromhex(client_pk))
        sub = self.pool.subscribe([], [{"kinds": [24133], "#p": [self.pk]}])
        sub.event.connect(self._on_event)
        self._sub = sub


def _qr_client(secret="s3cr3t"):
    pool = FakePool()
    signer = FakeSigner(pool)
    client = BunkerClient(pool)
    outcome = {}
    local_pk = client.listen_for_nostrconnect(
        relays=["wss://fake"], secret=secret,
        on_success=lambda pk: outcome.setdefault("ok", pk),
        on_failure=lambda r: outcome.setdefault("err", r),
        timeout_ms=60_000,
    )
    signer.open_channel_to(local_pk)
    return pool, signer, client, outcome, secret


def _bunker_client(secret="abc"):
    """Pair over a bunker:// URI, with the signer already listening.

    The client keypair is fixed up front so the signer can subscribe to the
    right channel before anything is published, which the synchronous fake
    pool requires.
    """
    pool = FakePool()
    signer = FakeSigner(pool)
    client = BunkerClient(pool)
    outcome = {}
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    uri = f"bunker://{signer.pk}?relay=wss%3A%2F%2Ffake&secret={secret}"

    def start():
        client.connect_to_bunker(
            uri,
            on_success=lambda pk: outcome.setdefault("ok", pk),
            on_failure=lambda r: outcome.setdefault("err", r),
            local_sk=local_sk,
        )

    return pool, signer, client, outcome, start


# --------------------------------------------------------------------- #
# The secret extractor                                                   #
# --------------------------------------------------------------------- #

def test_connect_response_carries_the_secret():
    assert connect_handshake_secret({"id": "1", "result": "hunter2"}) == "hunter2"


def test_connect_request_carries_the_secret():
    payload = {"id": "1", "method": "connect", "params": ["pk", "hunter2"]}
    assert connect_handshake_secret(payload) == "hunter2"


def test_ack_alone_is_not_a_secret():
    # Otherwise any onlooker could pair by shouting "ack".
    assert connect_handshake_secret({"id": "1", "result": "ack"}) is None


def test_auth_url_is_not_a_secret():
    assert connect_handshake_secret({"id": "1", "result": "auth_url"}) is None


def test_unrelated_traffic_is_not_a_handshake():
    assert connect_handshake_secret({"id": "1", "method": "ping"}) is None
    assert connect_handshake_secret({}) is None


# --------------------------------------------------------------------- #
# QR pairing, where the signer speaks first                              #
# --------------------------------------------------------------------- #

def test_qr_pairs_on_a_spec_compliant_connect_response():
    _, signer, _, outcome, secret = _qr_client()
    signer.send({"id": "nc1", "result": secret})
    assert outcome.get("ok") == signer.user_pk


def test_qr_pairs_on_a_legacy_connect_request():
    _, signer, client, outcome, secret = _qr_client()
    signer.send({"id": "nc1", "method": "connect",
                 "params": [client._local_pk_hex, secret]})
    assert outcome.get("ok") == signer.user_pk


def test_qr_answers_a_connect_request_but_not_a_connect_response():
    # A response is already an answer; replying to it would be noise.
    _, signer, client, _, secret = _qr_client()
    signer.send({"id": "nc1", "result": secret})
    assert all(p.get("method") for p in signer.seen), signer.seen
    assert "connect" not in [p.get("method") for p in signer.seen]


def test_qr_rejects_a_wrong_secret_without_pairing():
    _, signer, _, outcome, _ = _qr_client()
    signer.send({"id": "nc1", "result": "not-the-secret"})
    assert outcome == {}


def test_qr_rejects_a_bare_ack():
    _, signer, _, outcome, _ = _qr_client()
    signer.send({"id": "nc1", "result": "ack"})
    assert outcome == {}


# --------------------------------------------------------------------- #
# Auth challenges                                                        #
# --------------------------------------------------------------------- #

def test_auth_url_is_surfaced_and_does_not_fail_the_connect():
    _, signer, client, outcome, start = _bunker_client()
    signer.auth_url_for_connect = "https://signer.example/auth?t=1"
    seen = []
    client.auth_challenge.connect(seen.append)
    start()
    assert seen == ["https://signer.example/auth?t=1"]
    assert outcome.get("ok") == signer.user_pk


def test_auth_url_keeps_the_request_open_while_the_user_authenticates():
    _, signer, client, outcome, start = _bunker_client()
    signer.auth_url_for_connect = "https://signer.example/auth"
    signer.withhold_after_auth = True
    start()
    # Neither resolved nor failed: still waiting on the browser step.
    assert outcome == {}
    assert client._pending


def test_auth_url_without_an_address_fails_rather_than_hanging():
    _, signer, client, outcome, start = _bunker_client()
    # A challenge with no address is unactionable, so it must not be held
    # open the way a real one is.
    signer.auth_url_for_connect = ""
    signer.withhold_after_auth = True
    start()
    assert "err" in outcome
    assert not client._pending


# --------------------------------------------------------------------- #
# Unreadable replies                                                     #
# --------------------------------------------------------------------- #

def test_unreadable_reply_is_reported_instead_of_ignored():
    _, signer, client, _, _ = _qr_client()
    seen = []
    client.unreadable_reply.connect(seen.append)
    # NIP-04 ciphertext shape, which is not NIP-44 and cannot be decrypted.
    signer.send_raw("YmFzZTY0Y2lwaGVy?iv=aXZiYXNlNjQ=")
    assert seen == [signer.pk]


def test_unreadable_reply_is_reported_only_once_per_channel():
    _, signer, client, _, _ = _qr_client()
    seen = []
    client.unreadable_reply.connect(seen.append)
    for _ in range(4):
        signer.send_raw("YmFzZTY0Y2lwaGVy?iv=aXZiYXNlNjQ=")
    assert len(seen) == 1


# --------------------------------------------------------------------- #
# The paste and manual tabs still work                                   #
# --------------------------------------------------------------------- #

def test_bunker_uri_pairing_still_works():
    _, signer, client, outcome, start = _bunker_client()
    start()
    assert outcome.get("ok") == signer.user_pk
    assert [p["method"] for p in signer.seen] == ["connect", "get_public_key"]


def test_connect_request_sends_the_secret_and_requested_perms():
    _, signer, client, outcome, start = _bunker_client(secret="pairing-token")
    start()
    connect = signer.seen[0]
    assert connect["params"][0] == signer.pk
    assert connect["params"][1] == "pairing-token"
    assert "sign_event:30023" in connect["params"][2]


# --------------------------------------------------------------------- #
# Reattaching a saved profile                                            #
# --------------------------------------------------------------------- #

def _reattach(signer_answers_ping=True, signer_user_pk=None):
    pool = FakePool()
    signer = FakeSigner(pool)
    if not signer_answers_ping:
        signer.refuse_ping = True
    if signer_user_pk is not None:
        signer.user_pk = signer_user_pk
    client = BunkerClient(pool)
    outcome = {}
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    client.reattach(
        bunker_pubkey=signer.pk,
        relays=["wss://fake"],
        local_sk=local_sk,
        user_pubkey=signer.user_pk,
        on_success=lambda: outcome.setdefault("ok", True),
        on_failure=lambda r: outcome.setdefault("err", r),
    )
    return signer, client, outcome


def test_reattach_succeeds_on_a_ping():
    signer, client, outcome = _reattach()
    assert outcome.get("ok") is True
    assert client.is_connected


def test_reattach_falls_back_when_the_signer_ignores_ping():
    # A signer that does not answer ping is still a working signer.
    signer, client, outcome = _reattach(signer_answers_ping=False)
    assert outcome.get("ok") is True
    assert [p["method"] for p in signer.seen] == ["ping", "get_public_key"]


def test_reattach_refuses_a_signer_holding_a_different_account():
    pool = FakePool()
    signer = FakeSigner(pool)
    signer.refuse_ping = True
    client = BunkerClient(pool)
    outcome = {}
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    # The saved profile names an account the signer no longer holds.
    stale_pk = crypto.get_public_key(crypto.generate_secret_key()).hex()
    client.reattach(
        bunker_pubkey=signer.pk,
        relays=["wss://fake"],
        local_sk=local_sk,
        user_pubkey=stale_pk,
        on_success=lambda: outcome.setdefault("ok", True),
        on_failure=lambda r: outcome.setdefault("err", r),
    )
    assert "different account" in outcome.get("err", "")
    assert not client.is_connected
