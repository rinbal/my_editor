# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the pairing dialog, one page (the M1 layout of P1).

What must hold:

  The dialog shows the code to scan right away, captioned for a phone
  and naming Amber, Primal and Amethyst, dark on white whatever the
  theme. It renews by itself with no timer on screen, and an earlier
  code still pairs for a while. Under it, Connect with Bunker46 (its
  address asked once, https only, kept in the settings) and Open Signer
  App on This Computer (one line when nothing on the computer takes such
  a link). Details, collapsed, holds Copy Link and the connection
  address. A help button's tooltip holds the technical facts.

  Opened from Create Account it offers no way off to the windows it is
  part of, and saves nothing: the paired profile goes to the account
  window, which checks it first. Opened from Connect Signer App, it saves
  the paired profile as before.

  It speaks the person's words: no link scheme, key, hex, NIP or relay in
  the primary text, and an address that cannot be used is explained in
  one sentence. Closing the dialog, for any reason, closes every pairing
  channel.
"""

import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QDesktopServices, QFocusEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget  # noqa: E402

import settings  # noqa: E402
from nostr.bunker import BunkerClient  # noqa: E402
from nostr.profiles import Profile, ProfileStore  # noqa: E402
from nostr.ui import connect_dialog  # noqa: E402
from nostr.ui.connect_dialog import ConnectDialog, bunker46_address  # noqa: E402

PK = "ab" * 32


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """The Bunker46 address goes to a settings file of the test's own."""
    monkeypatch.setattr(settings, "_SETTINGS_FILE", str(tmp_path / "settings.json"))


@pytest.fixture
def opened(monkeypatch):
    """What the dialog hands the system to open: a web page (QUrl), or a
    signer app's link (text); ``opened.answer`` is what the system says
    (False: nothing took it)."""
    class Opened(list):
        answer = True
    urls = Opened()

    def open_url(url):
        urls.append(url)
        return urls.answer
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(open_url))
    monkeypatch.setattr(connect_dialog, "open_link", open_url)
    return urls


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

    def subscribe(self, urls, filters, sub_id=None, *, sign_in=None):
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
    got = []
    dialog.profile_connected.connect(got.append)
    dialog._on_pair_success(PK, PairedClient())
    return got


def dialog_for(tmp_path, pool=None, **options):
    return ConnectDialog(pool or FakePool(), ProfileStore(tmp_path / "p.json"), **options)


def shown_words(dialog):
    """Every word the dialog shows now, as a person reads it."""
    from PySide6.QtWidgets import QLabel, QLineEdit, QToolButton
    widgets = dialog.findChildren(QLabel) + dialog.findChildren(QPushButton) \
        + dialog.findChildren(QToolButton)
    words = [w.text() for w in widgets if w.isVisibleTo(dialog)]
    words += [w.placeholderText() for w in dialog.findChildren(QLineEdit)
              if w.isVisibleTo(dialog)]
    return " ".join(words + [dialog.windowTitle()])


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


def test_one_page_opens_on_the_code_for_a_phone_with_no_way_off(tmp_path):
    # P1 M1: the tabs went away; the code first, captioned for a phone.
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool, offer_alternatives=False)
    assert dialog.findChildren(QTabWidget) == []
    # Headed as the window is named, as on the chosen design.
    assert dialog.findChild(QLabel, "title").text() == dialog.windowTitle() == \
        "Connect Signer App"
    assert pool.subscriptions and not dialog._qr_label.pixmap().isNull()
    shown = shown_words(dialog)
    assert "Scan with your phone" in shown
    assert "Open Amber, Primal or Amethyst and scan the code." in shown
    assert "Connect with Bunker46" in shown and "Open Signer App on This Computer" in shown
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert "Create an Account" not in labels and "Restore from Backup" not in labels
    dialog.reject()
    assert pool.subscriptions[0].closed and dialog._client is None


def test_the_alternatives_are_offered_from_connect_signer(tmp_path):
    dialog = dialog_for(tmp_path)
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert {"Create an Account", "Restore from Backup"} <= labels


def test_a_paired_dialog_closes_its_channels_too(tmp_path):
    dialog = dialog_for(tmp_path, persist=False)
    listening = list(dialog._listeners)
    client = PairedClient()
    dialog.profile_connected.connect(lambda _profile: None)
    dialog._on_pair_success(PK, client)
    assert dialog._client is None and dialog._listeners == []
    assert all(c._subscription is None or c._subscription.closed for c in listening)


def test_the_primary_words_are_the_persons_not_the_protocols(tmp_path):
    # Lesson 26: no protocol terms in primary text; Bunker46 is a name.
    import re
    dialog = dialog_for(tmp_path)
    shown = shown_words(dialog).lower()
    for jargon in ("uri", "pubkey", "hex", "nip", "relay", "nostrconnect", "qr", "kind"):
        assert not re.search(rf"\b{jargon}", shown), jargon
    assert not re.search(r"bunker(?!46)", shown)
    assert dialog.windowTitle() == "Connect Signer App"
    # The facts are the help button's, in its tooltip and read with it.
    facts = dialog.help_button.toolTip()
    assert "Nostr Connect" in facts and "nostrconnect" in facts
    assert dialog.help_button.accessibleName() == "Help"
    assert dialog.help_button.accessibleDescription() == facts


def test_the_help_button_and_the_tooltips_show_without_a_mouse(tmp_path):
    from nostr.ui import tips
    shown = []
    dialog = dialog_for(tmp_path)
    original = tips.show_tip
    tips.show_tip = lambda widget: shown.append(widget)
    try:
        dialog.help_button.click()
        assert shown == [dialog.help_button]
        QApplication.sendEvent(dialog._open_here_btn,
                               QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
        assert shown[-1] is dialog._open_here_btn
        QApplication.sendEvent(dialog._open_here_btn, QFocusEvent(
            QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
        assert len(shown) == 2                              # a click shows no tooltip
    finally:
        tips.show_tip = original
        dialog.reject()
    assert "Amber Desktop" in dialog._open_here_btn.toolTip()


def test_the_code_is_dark_on_white_in_either_theme(tmp_path):
    for dark in (True, False):
        dialog = dialog_for(tmp_path, is_dark=dark)
        image = dialog._qr_label.pixmap().toImage()
        corner = image.pixelColor(2, 2)                  # the quiet zone
        assert corner.lightness() > 240, dark
        dialog.reject()


def test_the_code_renews_by_itself_and_an_earlier_one_still_pairs(tmp_path):
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool, persist=False)
    first, first_link = dialog._client, dialog._qr_uri
    assert dialog._renew_timer.isActive()
    assert dialog._renew_timer.interval() == connect_dialog._CODE_LIFE_S * 1000
    assert first._nc_timer.interval() == (connect_dialog._CODE_LIFE_S
                                          + connect_dialog._GRACE_S) * 1000
    dialog._renew_timer.timeout.emit()
    second = dialog._client
    assert second is not first and dialog._qr_uri != first_link
    assert len(pool.subscriptions) == 2 and not pool.subscriptions[0].closed
    assert "second" not in shown_words(dialog)               # no timer on screen
    # The earlier code runs out: let go without a word.
    dialog._on_pair_failure("timed out waiting for a signer to scan the QR", first)
    assert pool.subscriptions[0].closed and dialog._listeners == [second]
    assert dialog._status.text() == "" and dialog._regen_btn.isHidden()
    # A phone that scanned the earlier code pairs all the same.
    dialog._renew_timer.timeout.emit()
    third = dialog._client
    got = []
    dialog.profile_connected.connect(got.append)
    paired = PairedClient()
    dialog._on_pair_success(PK, paired)
    assert got and third is not second


def test_a_window_left_open_stops_renewing_and_offers_a_new_code(tmp_path):
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    for _ in range(connect_dialog._RENEWALS):
        dialog._renew_timer.timeout.emit()
    assert dialog._regen_btn.isHidden()
    dialog._renew_timer.timeout.emit()
    assert not dialog._regen_btn.isHidden()
    assert dialog._code_note.text() == "This code has expired."
    assert dialog._status.text() == "" and dialog._client is None
    assert all(sub.closed for sub in pool.subscriptions)
    assert not dialog._bunker46_btn.isEnabled() and not dialog._open_here_btn.isEnabled()
    dialog._regen_btn.click()
    assert dialog._regen_btn.isHidden() and dialog._client is not None
    assert dialog._renewals == 0 and dialog._renew_timer.isActive()
    dialog.reject()


def test_open_signer_app_hands_the_link_to_the_system_and_waits(tmp_path, opened):
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.click()
    link, = opened
    assert link == dialog._qr_uri and link.startswith("nostrconnect://")
    assert dialog._status.text() == ("Waiting for your signer app. Approve the connection "
                                     "there.")
    assert not dialog._renew_timer.isActive()               # the app has this code
    assert dialog._client._nc_timer.interval() == connect_dialog._APP_WAIT_S * 1000
    assert dialog._open_here_btn.isEnabled()                # open it again
    # The app did not answer in time: a new code, no error.
    dialog._on_pair_failure("timed out waiting for a signer to scan the QR", dialog._client)
    assert dialog._status.text() == "" and dialog._renew_timer.isActive()
    assert dialog._regen_btn.isHidden()
    dialog.reject()


def test_with_no_signer_app_on_this_computer_one_line_says_so(tmp_path, opened):
    opened.answer = False
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.click()
    assert len(opened) == 1
    assert dialog._status.text() == ("No signer app for connection links is set up on this "
                                     "computer.")
    assert dialog._status.objectName() == "muted"
    assert dialog._renew_timer.isActive() and dialog._client in dialog._listeners
    dialog.reject()


def test_bunker46_asks_its_address_once_and_opens_its_connections_page(tmp_path, opened):
    dialog = dialog_for(tmp_path)
    dialog.show()
    dialog._bunker46_btn.click()
    assert opened == [] and dialog._bunker46_box.isVisibleTo(dialog)
    assert dialog.focusWidget() is dialog._bunker46_edit
    for wrong in ("http://bunker.example.com", "https://me@bunker.example.com",
                  "https://bunker.example.com/?x=1", "ftp://bunker.example.com"):
        dialog._bunker46_edit.setText(wrong)
        dialog._bunker46_go.click()
        assert opened == [] and not dialog._bunker46_problem.isHidden(), wrong
        assert "https://" in dialog._bunker46_problem.text()
    dialog._bunker46_edit.setText("bunker.example.com/b46/")
    dialog._bunker46_go.click()
    assert settings.load_settings()["bunker46_address"] == "https://bunker.example.com/b46"
    url, = opened
    parts = urlsplit(url.toString(url.ComponentFormattingOption.FullyEncoded))
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "bunker.example.com",
                                                        "/b46/connections")
    assert parse_qs(parts.query)["nostrconnect"] == [dialog._qr_uri]
    assert "%2F%2F" in parts.query and unquote(parts.query.split("=", 1)[1]) == dialog._qr_uri
    assert not dialog._bunker46_box.isVisibleTo(dialog)
    assert dialog.focusWidget() is dialog._bunker46_btn    # back where it was asked from
    assert dialog._status.text() == "Waiting for Bunker46. Approve the connection there."
    dialog.reject()


def test_a_remembered_bunker46_opens_at_once_and_can_be_changed(tmp_path, opened):
    settings.save_setting("bunker46_address", "https://b46.example.org")
    dialog = dialog_for(tmp_path)
    dialog._bunker46_btn.click()
    assert not dialog._bunker46_box.isVisibleTo(dialog)
    assert opened[0].host() == "b46.example.org"
    dialog.details_toggle.setChecked(True)
    assert "b46.example.org" in shown_words(dialog)
    dialog._change_bunker46()
    assert dialog._bunker46_edit.text() == "https://b46.example.org"
    dialog._bunker46_edit.setText("https://other.example.org")
    dialog._bunker46_go.click()
    assert settings.load_settings()["bunker46_address"] == "https://other.example.org"
    assert opened[-1].host() == "other.example.org"
    dialog.reject()


@pytest.mark.parametrize("typed, kept", [
    ("bunker.example.com", "https://bunker.example.com"),
    ("https://bunker.example.com/", "https://bunker.example.com"),
    ("https://bunker46.lan:8443/x", "https://bunker46.lan:8443/x"),   # its own network
    ("http://bunker.example.com", None),
    ("https://", None),
    ("https://a b.example", None),
    ("https://bunker.example.com#x", None),
    ("https://bunker.example.com:port", None),
    ("", None),
])
def test_a_bunker46_address_is_https_and_nothing_else(typed, kept):
    assert bunker46_address(typed) == kept


def test_details_are_collapsed_and_hold_the_link_and_the_connection_address(tmp_path,
                                                                            monkeypatch):
    copied = []
    monkeypatch.setattr(connect_dialog.QApplication, "clipboard",
                        staticmethod(lambda: type("Clip", (), {
                            "setText": lambda _self, text: copied.append(text)})()))
    dialog = dialog_for(tmp_path)
    assert not dialog.details.isVisibleTo(dialog)
    assert "Copy Link" not in shown_words(dialog)
    dialog.details_toggle.setChecked(True)
    assert dialog.details.isVisibleTo(dialog)
    assert dialog.details_toggle.arrowType() == Qt.ArrowType.DownArrow
    dialog._copy_btn.click()
    assert copied == [dialog._qr_uri]
    assert dialog._status.text() == "Link copied. Paste it in your signer app."
    dialog.details_toggle.setChecked(False)
    assert not dialog.details.isVisibleTo(dialog)
    dialog.reject()


def test_an_address_that_cannot_be_used_is_explained_in_one_sentence(tmp_path):
    dialog = dialog_for(tmp_path)
    dialog.details_toggle.setChecked(True)
    dialog._paste_edit.setText("bunker://not-a-key?relay=wss://a.example")
    assert dialog._paste_btn.isEnabled() and dialog._paste_btn.isDefault()
    listening = dialog._client
    dialog._paste_btn.click()
    message = dialog._status.text()
    assert "bunker://" in message and "hex" not in message and "pubkey" not in message
    assert dialog._client is listening                      # the code still pairs


def test_a_pasted_connection_address_connects_and_a_failure_brings_the_code_back(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(BunkerClient, "connect_to_bunker",
                        lambda self, uri, on_success, on_failure: calls.append(
                            (self, uri, on_success, on_failure)))
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    dialog.details_toggle.setChecked(True)
    # A key that is a public key (the curve's generator): about half of
    # all 64-digit keys are not, and those are refused at once.
    uri = ("bunker://79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
           "?relay=wss://relay.example&secret=x")
    dialog._paste_edit.setText(uri)
    dialog._paste_btn.click()
    (client, sent, _ok, failed), = calls
    assert sent == uri and dialog._client is client
    assert pool.subscriptions[0].closed and dialog._listeners == []   # one way at a time
    assert not dialog._paste_btn.isEnabled()
    failed("timed out waiting for signer")
    assert dialog._status.objectName() == "error"
    assert "did not answer" in dialog._status.text()
    assert dialog._client in dialog._listeners and not pool.subscriptions[-1].closed
    assert dialog._paste_btn.isEnabled()
    dialog.reject()


def test_a_signer_apps_link_is_not_a_url_qt_takes():
    # Why default_apps.open_link, not QDesktopServices: the key is a
    # 64-character host label, one more than a host label may have.
    from PySide6.QtCore import QUrl
    link = "nostrconnect://" + "ab" * 32 + "?relay=wss%3A%2F%2Fnos.lol&secret=x"
    assert not QUrl(link).isValid()
    assert QUrl(link.replace("ab" * 32, "a" * 63)).isValid()


@pytest.mark.parametrize("platform, answer, command", [
    ("mac", 0, ["open"]),
    ("mac", 1, ["open"]),
    ("linux", 0, ["xdg-open"]),
    ("linux", 4, ["xdg-open"]),
])
def test_the_system_says_whether_an_app_took_the_link(monkeypatch, platform, answer, command):
    import subprocess
    import default_apps
    ran = []
    monkeypatch.setattr(default_apps, "_platform", lambda: platform)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: (
        ran.append(args), type("Done", (), {"returncode": answer})())[1])
    link = "nostrconnect://" + "ab" * 32 + "?secret=x"
    assert default_apps.open_link(link) is (answer == 0)
    assert ran == [command + [link]]


def test_windows_says_no_app_when_its_shell_has_none(monkeypatch):
    import default_apps
    monkeypatch.setattr(default_apps, "_platform", lambda: "windows")
    started = []

    def startfile(uri):
        started.append(uri)
        raise OSError("no application is associated")
    monkeypatch.setattr(default_apps.os, "startfile", startfile, raising=False)
    assert default_apps.open_link("nostrconnect://" + "ab" * 32) is False and started


@pytest.mark.parametrize("link", ["", "-a nostrconnect://x", "nostrconnect://a b",
                                  "nostrconnect://\u00e4", "no-scheme", "1x://y"])
def test_only_a_link_is_handed_to_the_system(monkeypatch, link):
    import subprocess
    import default_apps
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("ran"))
    assert default_apps.open_link(link) is False


def test_the_two_buttons_side_by_side_keep_their_whole_words(tmp_path):
    # The style sheet's small minimum width let the layout cut "Signer-App
    # auf diesem Computer öffnen" (P1 screenshots, German).
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.setText("Signer-App auf diesem Computer öffnen, recht lang")
    dialog.show()
    for button in (dialog._bunker46_btn, dialog._open_here_btn):
        assert button.minimumWidth() == button.sizeHint().width() > 102
    dialog.reject()
