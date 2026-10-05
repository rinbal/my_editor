# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the pairing dialog as Create Account uses it.

What must hold:

  Opened from Create Account, the dialog shows the code to scan right
  away, offers no way off to the windows it is part of, and saves nothing:
  the paired profile goes to the account window, which checks it first.
  Opened from Connect Signer, it saves the paired profile as before.

  Closing the dialog, for any reason, closes its pairing channel.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from nostr.profiles import Profile, ProfileStore  # noqa: E402
from nostr.ui.connect_dialog import ConnectDialog  # noqa: E402

PK = "ab" * 32


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class FakeSubscription(QObject):
    event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, urls, filters, sub_id=None):
        sub = FakeSubscription()
        self.subscriptions.append(sub)
        return sub


class PairedClient:
    bunker_pubkey = "b" * 64
    relays = ["wss://relay.example"]
    local_secret_hex = "c" * 64

    def __init__(self):
        self.closed = False

    def close(self, reason=""):
        self.closed = True


def pair(dialog):
    dialog._client = PairedClient()
    got = []
    dialog.profile_connected.connect(got.append)
    dialog._on_pair_success(PK)
    return got


def test_create_account_pairing_saves_nothing(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    existing = Profile(user_pubkey=PK, bunker_pubkey="d" * 64, bunker_relays=[],
                       local_secret_hex="e" * 64, display_name="Already here")
    store.upsert(existing)
    dialog = ConnectDialog(FakePool(), store, persist=False)
    got = pair(dialog)
    assert got and got[0].bunker_pubkey == "b" * 64
    assert store.get(PK).display_name == "Already here"
    assert ProfileStore(tmp_path / "profiles.json").get(PK).bunker_pubkey == "d" * 64


def test_connect_signer_pairing_saves_the_profile(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    dialog = ConnectDialog(FakePool(), store)
    pair(dialog)
    assert store.get(PK).bunker_pubkey == "b" * 64


def test_create_account_opens_on_the_code_with_no_way_off(tmp_path):
    pool = FakePool()
    dialog = ConnectDialog(pool, ProfileStore(tmp_path / "p.json"), start_on_qr=True,
                           offer_alternatives=False)
    assert dialog._tabs.currentIndex() == 1
    assert pool.subscriptions and not dialog._qr_label.pixmap().isNull()
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert "Create an Account" not in labels and "Restore from Backup" not in labels
    dialog.reject()
    assert pool.subscriptions[0].closed and dialog._client is None


def test_the_alternatives_are_offered_from_connect_signer(tmp_path):
    dialog = ConnectDialog(FakePool(), ProfileStore(tmp_path / "p.json"))
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert {"Create an Account", "Restore from Backup"} <= labels
    assert dialog._tabs.currentIndex() == 0


def test_a_paired_dialog_closes_its_channel_too(tmp_path):
    dialog = ConnectDialog(FakePool(), ProfileStore(tmp_path / "p.json"), persist=False)
    dialog._client = client = PairedClient()
    dialog.profile_connected.connect(lambda _profile: None)
    dialog._on_pair_success(PK)
    assert client.closed and dialog._client is None
