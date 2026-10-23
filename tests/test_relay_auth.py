# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins signing in as the account to its relays that ask (NIP-42).

What must hold:
- The account signs in only to relays it chose: its relay list, its
  draft relays, those it reads drafts left behind on, those being chosen
  for drafts, a membership's. Any other relay that asks is told nothing.
- The answer is signed by the account's own signer, and only an answer
  for this relay, this challenge and this account is sent.
- A signer app that says no is told apart from other failures, so the
  window can say so; a key kept here never "declines".
- What the person sees: "Signing in to …", and plain words when it did
  not work.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.local_signer import LOCAL  # noqa: E402
from nostr.outbox.directory import RelayDirectory  # noqa: E402
from nostr.relay import NOT_CHOSEN  # noqa: E402
from nostr.relay_auth import DECLINED, AccountSignIn  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    OTHER_SK, PK, FakeClient, FakePool, FakeSessionPool, Profile, settle, signed,
)
from tests.relay_fakes import accept_sign_in, fake_relay_pool, refused_read  # noqa: E402

MINE = "wss://mine.example"
DRAFTS = "wss://drafts.example"
STRANGER = "wss://stranger.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pool(monkeypatch):
    return fake_relay_pool(monkeypatch)


def own_directory(*, relays=(MINE,), drafts=()):
    directory = RelayDirectory(FakePool(), own_pubkeys=lambda: [PK], store_path=None)
    directory.remember(signed(10002, [["r", url] for url in relays]))
    if drafts:
        directory.set_draft_relays(PK, list(drafts))
    return directory


def account(directory=None, *, client=None, entitled=(), profile=None):
    profile = profile or Profile()
    sessions = FakeSessionPool(client or FakeClient())
    return AccountSignIn(profile=lambda: profile, session_pool=sessions,
                         directory=directory or own_directory(),
                         entitled=lambda: list(entitled)), sessions


def failures(pool):
    seen = []
    pool.sign_in_failed.connect(lambda *args: seen.append(args))
    return seen


def test_the_account_signs_in_only_where_it_chose():
    directory = own_directory(relays=(MINE,), drafts=(DRAFTS,))
    directory.leave_drafts_behind(PK, ["wss://left.example"], since=None)
    directory.choosing_draft_relays(PK, ["wss://chosen.example"])
    sign_in, _ = account(directory, entitled=["wss://members.example/"])
    for url in (MINE, DRAFTS, "wss://left.example", "wss://chosen.example",
                "wss://members.example", "WSS://Mine.Example/"):
        assert sign_in.may_sign_in(url), url
    assert not sign_in.may_sign_in(STRANGER)
    directory.choosing_draft_relays(PK, ())
    assert not sign_in.may_sign_in("wss://chosen.example")


def test_a_relay_the_account_did_not_choose_is_told_nothing(pool):
    pool.sign_in, sessions = account()
    failed = failures(pool)
    sub, relay, seen = refused_read(pool, STRANGER)
    settle()
    assert relay.sent_of("AUTH") == [] and sessions.client.requests == []
    assert seen["closed"] and failed == []


def test_the_account_signs_the_answer_with_its_own_signer(pool):
    pool.sign_in, sessions = account()
    sub, relay, seen = refused_read(pool, MINE)
    settle()
    answer = accept_sign_in(relay)
    assert answer["pubkey"] == PK and len(sessions.client.requests) == 1
    assert len(relay.sent_of("REQ")) == 2 and seen["closed"] == []


def test_an_answer_signed_by_someone_else_is_not_sent(pool):
    pool.sign_in, _ = account(client=FakeClient(sk=OTHER_SK))
    sub, relay, seen = refused_read(pool, MINE)
    settle()
    assert relay.sent_of("AUTH") == [] and seen["closed"]


def test_a_signer_app_that_says_no_is_told_apart(pool):
    pool.sign_in, _ = account(client=FakeClient(fail="user rejected"))
    failed = failures(pool)
    refused_read(pool, MINE)
    settle()
    assert failed == [(MINE, DECLINED, False)]


def test_a_signer_app_that_does_not_answer_did_not_say_no(pool):
    pool.sign_in, _ = account(client=FakeClient(fail="timed out waiting for signer"))
    failed = failures(pool)
    refused_read(pool, MINE)
    settle()
    assert failed == [(MINE, "timed out waiting for signer", False)]


def test_a_challenge_shaped_like_a_secret_key_is_no_decline(pool):
    from nostr import bech32
    key_shaped = bech32.encode_nsec("6e" * 32)
    pool.sign_in, sessions = account()
    failed = failures(pool)
    sub, relay, seen = refused_read(pool, MINE, challenge=key_shaped)
    settle()
    assert sessions.client.requests == [] and relay.sent_of("AUTH") == []
    assert len(failed) == 1 and failed[0][1] != DECLINED
    again = pool.subscribe([MINE], [{"kinds": [1]}])
    relay.says("CLOSED", again.sub_id, "auth-required: sign in")
    settle()
    assert len(failed) == 1                      # refused for good on this connection


def test_only_the_signer_apps_answer_is_final():
    sign_in, _ = account()
    assert sign_in.is_final(DECLINED)
    assert not sign_in.is_final("timed out waiting for signer")
    assert not sign_in.is_final("not connected")


def test_a_key_kept_here_never_declines(pool):
    local = Profile()
    local.signer = LOCAL
    pool.sign_in, _ = account(profile=local,
                              client=FakeClient(fail="could not complete the request"))
    failed = failures(pool)
    refused_read(pool, MINE)
    settle()
    assert failed == [(MINE, "could not complete the request", False)]


def test_without_an_account_nobody_signs_in(pool):
    sign_in = AccountSignIn(profile=lambda: None, session_pool=FakeSessionPool(FakeClient()),
                            directory=own_directory())
    done = []
    pool.get_or_create(MINE).sign_in(sign_in, lambda ok, why: done.append((ok, why)))
    assert done == [(False, NOT_CHOSEN)]


def test_the_window_says_what_happened(qt_app):
    from main_window import MainWindow
    window = MainWindow.__new__(MainWindow)
    shown = []

    class Status:
        def showMessage(self, text, _ms, **context):
            shown.append(text)

    window.status = Status()
    MainWindow._on_relay_signing_in(window, MINE)
    MainWindow._on_relay_sign_in_failed(window, MINE, DECLINED, False)
    MainWindow._on_relay_sign_in_failed(window, MINE, "restricted", True)
    MainWindow._on_relay_sign_in_failed(window, MINE, "relay disconnected", False)
    MainWindow._on_relay_sign_in_failed(window, STRANGER, NOT_CHOSEN, False)
    assert shown == [
        f"Signing in to {MINE}…",
        f"{MINE} asks you to sign in, and your signer app declined.",
        f"{MINE} didn’t accept your sign-in.",
        f"Couldn’t sign in to {MINE}.",
    ]


def test_every_change_of_account_ends_the_other_accounts_sign_ins(qt_app):
    from main_window import MainWindow
    kept = []

    class Pool:
        def end_sign_ins(self, keep):
            kept.append(keep)

    class Store:
        current = Profile()

        def default(self):
            return self.current

    window = MainWindow.__new__(MainWindow)
    window._relay_pool, window._profile_store = Pool(), Store()
    window._update_refresh_command = lambda: None
    MainWindow._update_account_actions(window)
    Store.current = None                         # signed out
    MainWindow._update_account_actions(window)
    assert kept == [PK, ""]
