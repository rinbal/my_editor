# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the account controller: the network work behind the account windows.

What must hold:

  A created account is published, relay list then name. When that fails,
  Try Again in the window sends it again and can succeed, as often as it
  takes. Finish Later is a promise kept: the account stays marked, and the
  next time it becomes the active one the setup runs again, without
  asking, creating only what is missing, once.

  The work never depends on the window: closing it mid-setup lets the
  setup finish, and nothing breaks.

  A restored account is read, not written; one that has nothing yet is
  offered a relay list, published only when the person says so. A key
  restored for an account paired with a signer app signs here from then on.

  Signing out forgets every key kept for the account and its signer.
  Pairing a signer app for an account whose key is kept here offers to
  delete that key. The pairing dialog opens over the account window, on
  the code, saving nothing, and is deleted afterwards.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from nostr import account_controller as ac  # noqa: E402
from nostr import bunker, crypto  # noqa: E402
from nostr.bunker import BunkerSessionPool  # noqa: E402
from nostr.key_vault import KeyVault  # noqa: E402
from nostr.local_signer import LocalSigner  # noqa: E402
from nostr.outbox.directory import RelayDirectory  # noqa: E402
from nostr.outbox.lookup import Lookup  # noqa: E402
from nostr.outbox.policy import LookupState  # noqa: E402
from nostr.profiles import ProfileStore  # noqa: E402
from nostr.ui import account_windows as aw  # noqa: E402
from nostr.ui.account_windows import (  # noqa: E402
    DONE, SETUP, STEP_PROFILE, STEP_RELAYS, SetupReport, local_profile,
)
from tests.outbox_fakes import ABSENT, NOW, FakePool, FakeQuery, found, signed  # noqa: E402
from tests.test_local_keys import FakeRemote, remote_profile  # noqa: E402

SK = bytes.fromhex("5c" * 32)
PK = crypto.get_public_key(SK).hex()


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions raised inside Qt slots, which would otherwise only print."""
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc[1]))
    return errors


class Alerts:
    def __init__(self):
        self.answers = []
        self.asked = []

    def ask(self, parent, **kw):
        self.asked.append(kw)
        return self.answers.pop(0) if self.answers else False

    def confirm(self, parent, **kw):
        self.asked.append(kw)
        return self.answers.pop(0) if self.answers else False

    def inform(self, parent, **kw):
        self.asked.append(kw)


@pytest.fixture
def alerts(monkeypatch):
    found_alerts = Alerts()
    monkeypatch.setattr(ac, "ask", found_alerts.ask)
    monkeypatch.setattr(ac, "confirm_destructive", found_alerts.confirm)
    monkeypatch.setattr(ac, "inform", found_alerts.inform)
    monkeypatch.setattr(aw, "ask", lambda parent, **kw: False)
    return found_alerts


class Everything:
    def __contains__(self, _url):
        return True


def settle(rounds: int = 30):
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone(5_000)
        QApplication.processEvents()


def delete_now(obj):
    obj.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


class Setup:
    """A controller with real windows, a real local signer and fake relays."""

    def __init__(self, tmp_path, *, answers=None, fetch=None):
        self.store = ProfileStore(tmp_path / "profiles.json")
        self.vault = KeyVault(tmp_path / "keys.json")
        self.relays = FakePool()
        self.query = FakeQuery({10002: ABSENT, 0: ABSENT} if answers is None else answers)
        self.directory = RelayDirectory(FakePool(), query=self.query, store_path=None)
        self.sessions = BunkerSessionPool(pool=None, vault=self.vault)
        self.window = QWidget()
        self.fetched = []
        self.controller = ac.AccountController(
            relay_pool=self.relays, directory=self.directory, vault=self.vault,
            session_pool=self.sessions, store=self.store, window=self.window,
            fetch=fetch or self.fetch, writer_deps={"query": self.query,
                                                    "clock": lambda: NOW})
        self.activated, self.statuses = [], []
        self.controller.activate.connect(self.activated.append)
        self.controller.status.connect(lambda text, ms: self.statuses.append(text))

    def fetch(self, pool, relays, *, kind, author, on_done, parent=None, timeout_ms=0):
        self.fetched.append(kind)
        on_done(self.query.answers.get(kind, Lookup(LookupState.UNKNOWN)))

    def kinds_published(self):
        return [event["kind"] for _targets, event in self.relays.published]

    def local_account(self, *, name="Satoshi", pending=False):
        self.vault.store(SK)
        profile = local_profile(PK)
        profile.display_name = name
        profile.setup_pending = pending
        self.store.upsert(profile)
        return profile


# -- a created account ---------------------------------------------------------------

def test_a_failed_setup_can_be_tried_again_until_it_succeeds(tmp_path, alerts, slot_errors):
    s = Setup(tmp_path)
    s.relays.refuse = Everything()            # offline
    win = s.controller.create_account()
    win._name_edit.setText("Satoshi")
    win.buttons["continue"].click()
    win.backup_form.copy_private_key()
    win.buttons["continue"].click()
    win.buttons["continue"].click()           # keep the key on this computer
    settle()
    assert win.page == SETUP and set(win.buttons) == {"later", "retry"}
    assert s.store.get(win.pubkey).setup_pending
    assert [p.user_pubkey for p in s.activated] == [win.pubkey]

    win.buttons["retry"].click()              # still offline
    settle()
    assert set(win.buttons) == {"later", "retry"}

    s.relays.refuse = set()                   # back online
    win.buttons["retry"].click()
    settle()
    assert win.page == DONE and slot_errors == []
    assert not s.store.get(win.pubkey).setup_pending
    assert s.kinds_published()[-2:] == [10002, 0]
    win.reject()


def test_activating_the_new_account_does_not_start_a_second_setup(tmp_path, alerts):
    s = Setup(tmp_path)
    s.controller.activate.connect(s.controller.profile_activated)   # as the main window
    profile = s.local_account()
    report = SetupReport()
    s.controller._account_ready(profile, "Satoshi", report, created=True)
    settle()
    assert s.kinds_published() == [10002, 0]
    assert not s.store.get(PK).setup_pending


def test_closing_the_window_mid_setup_lets_the_work_finish(tmp_path, alerts, slot_errors):
    s = Setup(tmp_path)
    profile = s.local_account()
    window = QWidget()
    report = SetupReport(window)
    s.controller._account_ready(profile, "Satoshi", report, created=True)
    delete_now(window)                        # closed before the relays answered
    settle()
    assert slot_errors == [] and s.kinds_published() == [10002, 0]
    assert not s.store.get(PK).setup_pending
    assert any("set up" in text for text in s.statuses)
    assert s.controller._setups == {}


def test_a_setup_left_for_later_finishes_when_the_account_is_next_used(tmp_path, alerts):
    s = Setup(tmp_path)
    profile = s.local_account(pending=True)
    s.controller.profile_activated(profile)
    s.controller.profile_activated(profile)   # switched to twice: one run
    settle()
    assert s.kinds_published() == [10002, 0]
    assert json.loads(s.relays.published[1][1]["content"])["name"] == "Satoshi"
    assert not s.store.get(PK).setup_pending
    assert ProfileStore(tmp_path / "profiles.json").get(PK).setup_pending is False
    assert alerts.asked == []                 # never asked: MyEditor made the account
    s.controller.profile_activated(profile)
    settle()
    assert len(s.relays.published) == 2


def test_a_setup_left_for_later_creates_only_what_is_missing(tmp_path, alerts):
    theirs = signed(0, content=json.dumps({"name": "Chosen since"}))
    s = Setup(tmp_path, answers={10002: ABSENT, 0: found(theirs)})
    s.local_account(pending=True)
    s.controller.profile_activated(s.store.get(PK))
    settle()
    assert s.kinds_published() == [10002]
    assert not s.store.get(PK).setup_pending


def test_a_setup_left_for_later_waits_while_the_network_cant_be_read(tmp_path, alerts):
    s = Setup(tmp_path, answers={})
    s.local_account(pending=True)
    s.controller.profile_activated(s.store.get(PK))
    settle()
    assert s.relays.published == [] and s.store.get(PK).setup_pending
    assert s.controller._setups == {}         # free to try at the next use


def test_accounts_not_made_here_are_never_set_up(tmp_path, alerts):
    s = Setup(tmp_path)
    s.controller.profile_activated(s.local_account(pending=False))
    settle()
    assert s.relays.published == []


# -- a restored account --------------------------------------------------------------

def test_a_restore_that_finds_nothing_offers_a_relay_list_once(tmp_path, alerts):
    s = Setup(tmp_path)
    profile = s.local_account(name="")
    report = SetupReport()
    steps, finished = [], []
    report.step.connect(lambda key, state, detail: steps.append((key, state, detail)))
    report.finished.connect(lambda ok, msg: finished.append(ok))
    s.controller._account_ready(profile, "", report, created=False)
    settle()
    assert (STEP_RELAYS, "done", "This account has no relay list yet.") in steps
    assert (STEP_PROFILE, "done", "No public profile found.") in steps
    assert [key for key, state, _d in steps if state == "active"] == [STEP_RELAYS, STEP_PROFILE]
    assert finished == [True] and s.relays.published == []
    assert [a["title"] for a in alerts.asked] == ["Publish a relay list for this account?"]
    s.controller.offer_relay_list(profile)    # declined: not asked again
    assert len(alerts.asked) == 1


def test_the_relay_list_is_published_when_the_person_says_so(tmp_path, alerts):
    s = Setup(tmp_path)
    profile = s.local_account()
    alerts.answers.append(True)
    s.controller.offer_relay_list(profile)
    settle()
    assert s.kinds_published() == [10002]
    assert "Your relay list is published." in s.statuses


def test_a_restored_name_is_learned_and_a_strange_profile_is_survived(tmp_path, alerts):
    named = signed(0, content=json.dumps({"name": "Satoshi"}))
    s = Setup(tmp_path, answers={10002: found(signed(10002, [["r", "wss://a.com"]])),
                                 0: found(named)})
    changed = []
    s.controller.profile_changed.connect(changed.append)
    profile = s.local_account(name="")
    report = SetupReport()
    finished = []
    report.finished.connect(lambda ok, msg: finished.append(ok))
    s.controller._account_ready(profile, "", report, created=False)
    settle()
    assert finished == [True] and s.store.get(PK).display_name == "Satoshi"
    assert changed and alerts.asked == []

    odd = dict(named, content="[1, 2, 3]")
    s.query.answers[0] = found(odd)
    report = SetupReport()
    report.finished.connect(lambda ok, msg: finished.append(ok))
    s.controller._account_ready(profile, "", report, created=False)
    settle()
    assert finished == [True, True]


def test_a_key_restored_for_an_app_paired_account_signs_here(tmp_path, alerts, monkeypatch):
    FakeRemote.made = []
    monkeypatch.setattr(bunker, "BunkerClient", FakeRemote)
    s = Setup(tmp_path)
    paired = remote_profile(PK)
    s.store.upsert(paired)
    got = []
    s.sessions.get(paired, got.append, print)
    FakeRemote.made[0].answer()
    restore = s.controller.restore_account()
    restore._paste_link.click()
    restore._paste_edit.setText(SK.hex())
    restore.buttons["continue"].click()
    settle()
    s.sessions.get(s.store.get(PK), got.append, print)
    assert isinstance(got[-1], LocalSigner) and FakeRemote.made[0].closed
    restore.reject()


# -- signing out and signer apps -----------------------------------------------------------

def test_signing_out_of_a_local_account_forgets_its_key_and_signer(tmp_path, alerts):
    s = Setup(tmp_path)
    profile = s.local_account()
    signers = []
    s.sessions.get(profile, signers.append, print)
    alerts.answers.append(True)
    assert s.controller.sign_out(profile)
    assert "deletes the private key" in alerts.asked[0]["message"]
    assert not s.vault.has(PK) and s.store.get(PK) is None
    assert not signers[0].is_connected


def test_signing_out_of_an_app_account_also_forgets_a_key_kept_here(tmp_path, alerts):
    s = Setup(tmp_path)
    s.vault.store(SK)
    paired = remote_profile(PK)
    s.store.upsert(paired)
    alerts.answers.append(True)
    assert s.controller.sign_out(paired)
    message = alerts.asked[0]["message"]
    assert "copy of the key" in message and alerts.asked[0]["action"] == "Sign Out and Delete Key"
    assert not s.vault.has(PK)


def test_a_key_that_cant_be_deleted_keeps_the_account(tmp_path, alerts):
    s = Setup(tmp_path)
    profile = s.local_account()

    def refuse(_pubkey):
        raise OSError(30, "Read-only file system")
    s.vault.forget = refuse
    alerts.answers.append(True)
    assert not s.controller.sign_out(profile)
    assert s.store.get(PK) is not None
    assert alerts.asked[-1]["title"] == "The private key couldn’t be deleted"
    assert "Errno" not in alerts.asked[-1]["message"]


def test_pairing_an_app_offers_to_delete_the_key_kept_here(tmp_path, alerts):
    s = Setup(tmp_path, answers={10002: found(signed(10002, [["r", "wss://a.com"]]))})
    s.vault.store(SK)
    paired = remote_profile(PK)
    alerts.answers.append(False)              # Keep Key
    s.controller.signer_paired(paired)
    assert s.vault.has(PK)
    alerts.answers.append(True)               # Delete Key
    s.controller.signer_paired(paired)
    settle()
    assert not s.vault.has(PK)
    titles = [a["title"] for a in alerts.asked]
    assert titles == ["Delete the private key kept on this computer?"] * 2


def test_backing_up_a_local_account_without_its_key_says_what_to_do(tmp_path, alerts):
    s = Setup(tmp_path)
    profile = local_profile(PK)
    s.store.upsert(profile)
    assert s.controller.backup_account() is None
    message = alerts.asked[0]["message"]
    assert "signer app" not in message and "restore" in message


class FakeConnectDialog(QDialog):
    made = []
    profile_connected = Signal(object)

    def __init__(self, pool, store, parent=None, **kwargs):
        super().__init__(parent)
        self.kwargs = kwargs
        FakeConnectDialog.made.append(self)

    def exec(self):
        return 0


def test_the_pairing_dialog_opens_over_the_account_window_and_goes_away(
        tmp_path, alerts, monkeypatch):
    FakeConnectDialog.made = []
    monkeypatch.setattr(ac, "ConnectDialog", FakeConnectDialog)
    s = Setup(tmp_path)
    account_window = QWidget()
    s.controller._pair_signer(lambda profile: None, parent=account_window)
    dialog, = FakeConnectDialog.made
    assert dialog.parent() is account_window
    assert dialog.kwargs["persist"] is False and dialog.kwargs["start_on_qr"] is True
    assert dialog.kwargs["offer_alternatives"] is False
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not shiboken6.isValid(dialog)
