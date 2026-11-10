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

import app_links  # noqa: E402
import settings  # noqa: E402
from nostr.bunker import NIP46_RELAYS, BunkerClient  # noqa: E402
from nostr.profiles import Profile, ProfileStore  # noqa: E402
from nostr.ui import connect_dialog  # noqa: E402
from nostr.ui.connect_dialog import ConnectDialog, bunker46_address  # noqa: E402

PK = "ab" * 32
# A connection address whose key is a public key (the curve's generator).
PASTED = "bunker://" + "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798" \
    + "?relay=wss://relay.example&secret=x"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """The Bunker46 address goes to a settings file of the test's own."""
    monkeypatch.setattr(settings, "_SETTINGS_FILE", str(tmp_path / "settings.json"))


def turn():
    """The answers due on a later turn of the event loop."""
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture
def opened(monkeypatch):
    """What the dialog hands the system to open: a web page (QUrl), or a
    signer app's link (text). ``opened.answer`` is what the system says
    of a link (an app_links answer; None: it has not answered yet, and
    ``opened.done`` answers it), ``opened.browser`` whether a browser
    opened a page."""
    class Opened(list):
        answer = app_links.OPENED
        browser = True
        done = None
    urls = Opened()

    def open_url(url):
        urls.append(url)
        return urls.browser

    def open_link(uri, done):
        urls.append(uri)
        urls.done = done
        if urls.answer is not None:
            done(urls.answer)
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(open_url))
    monkeypatch.setattr(app_links, "open_link", open_link)
    return urls


class FakeSubscription(QObject):
    event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


class FakeJob(QObject):
    all_done = Signal(list)


class FakePool:
    def __init__(self):
        self.subscriptions = []
        self.published = []

    def subscribe(self, urls, filters, sub_id=None, *, sign_in=None):
        sub = FakeSubscription()
        self.subscriptions.append(sub)
        return sub

    def publish(self, urls, event, timeout_ms=8000, *, sign_in=None):
        self.published.append(event)
        return FakeJob()


class PairedClient:
    bunker_pubkey = "b" * 64
    relays = ["wss://relay.example"]
    local_secret_hex = "c" * 64

    def __init__(self):
        self.closed = False

    def close(self, reason=""):
        self.closed = True


def listening(dialog, client):
    """``client`` stands for a code the dialog listens with (its answer
    counts only from a way in flight)."""
    dialog._listeners.append(client)
    return client


def pair(dialog):
    got = []
    dialog.profile_connected.connect(got.append)
    dialog._on_pair_success(PK, listening(dialog, PairedClient()))
    return got


def dialog_for(tmp_path, pool=None, **options):
    return ConnectDialog(pool or FakePool(), ProfileStore(tmp_path / "p.json"), **options)


def dark_modules(dialog) -> bool:
    """Whether the code shown has a dark module, so something to scan."""
    image = dialog._qr_label.pixmap().toImage()
    return any(image.pixelColor(x, y).lightness() < 128
               for y in range(0, image.height(), 2) for x in range(0, image.width(), 2))


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
    codes = list(dialog._listeners)
    client = listening(dialog, PairedClient())
    dialog.profile_connected.connect(lambda _profile: None)
    dialog._on_pair_success(PK, client)
    assert dialog._client is None and dialog._listeners == []
    assert all(c._subscription is None or c._subscription.closed for c in codes)


def test_the_primary_words_are_the_persons_not_the_protocols(tmp_path):
    # Lesson 26: no protocol terms in primary text; Bunker46 is a name.
    import re
    dialog = dialog_for(tmp_path)
    shown = shown_words(dialog).lower()
    for jargon in ("uri", "pubkey", "hex", "nip", "relay", "nostrconnect", "qr", "kind"):
        assert not re.search(rf"\b{jargon}", shown), jargon
    assert not re.search(r"bunker(?!46)", shown)
    assert dialog.windowTitle() == "Connect Signer App"
    # The facts are the help button's, in its tooltip and read with it:
    # the protocol by its NIP (as the canvas words it), the relays the
    # code meets on, and the other form an address takes (RG-M2r2b 16).
    facts = dialog.help_button.toolTip()
    assert "Nostr Connect" in facts and "nostrconnect" in facts
    assert "connection by NIP-46" in facts and "bunker://" in facts
    for relay in NIP46_RELAYS:
        assert urlsplit(relay).netloc in facts
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
    # A replaced code listens one full renewal more (RG-M2r2b 10).
    assert connect_dialog._GRACE_S >= connect_dialog._CODE_LIFE_S
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
    assert second in dialog._listeners                       # still listening
    got = []
    dialog.profile_connected.connect(got.append)
    dialog._on_pair_success(PK, listening(dialog, PairedClient()))
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
    assert not dark_modules(dialog) and dialog._qr_uri == ""   # no phone scans it
    dialog._regen_btn.click()
    assert dialog._regen_btn.isHidden() and dialog._client is not None
    assert dialog._renewals == 0 and dialog._renew_timer.isActive()
    dialog.reject()


def test_open_signer_app_hands_the_link_to_the_system_and_waits(tmp_path, opened):
    opened.answer = None
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.click()
    link, = opened
    # Never waited for on the GUI thread: said while the system answers.
    assert dialog._status.text() == "Opening your signer app…"
    opened.done(app_links.OPENED)
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
    opened.answer = app_links.NO_APP
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
    turn()
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
    # RG-M2r2b 15: the host as DNS has it, every escape a real one.
    ("bünker.example.com", "https://xn--bnker-kva.example.com"),
    ("https://bunker.example.com/%zz", None),
    ("https://bunker.example.com/a%2", None),
    ("https://bunker.example.com/a%2Fb", "https://bunker.example.com/a%2Fb"),
    ("https://bunker.example.com/wörter", "https://bunker.example.com/w%C3%B6rter"),
    ("https://[2001:db8::1]:8443/b46", "https://[2001:db8::1]:8443/b46"),
])
def test_a_bunker46_address_is_https_and_nothing_else(typed, kept):
    assert bunker46_address(typed) == kept
    if kept is not None:
        link = connect_dialog.bunker46_link(kept, "nostrconnect://" + "ab" * 32 + "?x=y")
        assert link.isValid() and bytes(link.toEncoded()).decode("ascii").startswith(kept)


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


@pytest.mark.parametrize("address", [
    "bunker://not-a-key?relay=wss://a.example",
    # 64 hex digits, but no point of the curve (RG-M2r2b 2): refused at
    # once, never a wait for an answer that cannot come.
    "bunker://" + "00" * 32 + "?relay=wss://a.example",
    "bunker://" + "ff" * 32 + "?relay=wss://a.example",
])
def test_an_address_that_cannot_be_used_is_explained_in_one_sentence(tmp_path, address):
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    dialog.details_toggle.setChecked(True)
    dialog._paste_edit.setText(address)
    assert dialog._paste_btn.isEnabled() and dialog._paste_btn.isDefault()
    code = dialog._client
    dialog._paste_btn.click()
    # Said next to the field, in Details; the primary line keeps its words
    # (lesson 26: no link scheme outside Details).
    message = dialog._paste_problem.text()
    assert dialog._paste_problem.isVisibleTo(dialog) and message.count(".") == 2
    assert "bunker://" in message and "hex" not in message and "pubkey" not in message
    assert dialog._status.text() == "" and "bunker" not in dialog._status.text()
    assert dialog._paste_edit.accessibleDescription() == message
    assert dialog._client is code and len(pool.subscriptions) == 1   # the code still pairs
    assert dialog._paste_btn.isEnabled() and not pool.published
    dialog._paste_edit.setText(address + "x")                # typed anew: the line goes
    assert not dialog._paste_problem.isVisibleTo(dialog)
    dialog.reject()


def test_a_pasted_connection_address_connects_and_a_failure_brings_the_code_back(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(BunkerClient, "connect_to_bunker",
                        lambda self, uri, on_success, on_failure: calls.append(
                            (self, uri, on_success, on_failure)))
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    dialog.details_toggle.setChecked(True)
    uri = PASTED
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




def test_the_two_buttons_side_by_side_keep_their_whole_words(tmp_path):
    # The style sheet's small minimum width let the layout cut "Signer-App
    # auf diesem Computer öffnen" (P1 screenshots, German).
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.setText("Signer-App auf diesem Computer öffnen, recht lang")
    dialog.show()
    for button in (dialog._bunker46_btn, dialog._open_here_btn):
        assert button.minimumWidth() == button.sizeHint().width() > 102
    dialog.reject()


# -- one way in flight owns the wait (RG-M2r2b 1) ---------------------------

def paste(dialog, uri=PASTED):
    dialog.details_toggle.setChecked(True)
    dialog._paste_edit.setText(uri)
    dialog._paste_btn.click()
    return dialog._client


def test_while_an_address_pasted_is_tried_no_code_is_shown_and_new_code_lets_it_go(tmp_path):
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    pasted = paste(dialog)
    assert pasted not in dialog._listeners and pool.published       # really asked
    # Nothing listens for a code now: none is shown, none is copied.
    assert not dark_modules(dialog) and not dialog._copy_btn.isEnabled()
    assert dialog._code_note.text() == "Connecting with the address you pasted."
    assert not dialog._regen_btn.isHidden()
    dialog._regen_btn.click()
    assert pasted._subscription is None and dialog._client is not pasted
    assert dialog._client in dialog._listeners and dark_modules(dialog)
    assert dialog._status.text() == ""                  # letting it go is no failure
    dialog.reject()


def test_opening_an_app_lets_a_pasted_address_go_and_its_late_answer_touches_nothing(
        tmp_path, opened, monkeypatch):
    # The scenario of RG-M2r2b 1: an address pasted while the phone sleeps,
    # then the code opened in Amber Desktop; the paste's late failure must
    # not end the wait for Amber.
    calls = []
    monkeypatch.setattr(BunkerClient, "connect_to_bunker",
                        lambda self, uri, on_success, on_failure: calls.append(
                            (self, on_success, on_failure)))
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    pasted = paste(dialog)
    (_client, succeeded, failed), = calls
    dialog._open_here_btn.click()
    waiting = dialog._client
    assert waiting is not pasted and waiting in dialog._listeners
    assert dialog._status.text() == "Waiting for your signer app. Approve the connection there."
    failed("timed out waiting for signer")                      # 90 s later
    assert dialog._client is waiting and waiting in dialog._listeners
    assert not pool.subscriptions[-1].closed and not dialog._renew_timer.isActive()
    assert dialog._status.text() == "Waiting for your signer app. Approve the connection there."
    got = []
    dialog.profile_connected.connect(got.append)
    succeeded(PK)                                               # nor does a late yes
    assert got == [] and dialog.isVisible() is False and dialog.result() == 0
    assert dialog._client is waiting
    dialog.reject()


def test_cancel_closes_the_address_pasted_and_starts_nothing(tmp_path):
    pool = FakePool()
    dialog = dialog_for(tmp_path, pool)
    pasted = paste(dialog)
    channel = pool.subscriptions[-1]
    before = len(pool.subscriptions)
    dialog._cancel_btn.click()
    assert channel.closed and pasted._subscription is None and pasted._pending == {}
    assert len(pool.subscriptions) == before                    # no code opened on the way
    assert dialog._client is None and dialog._listeners == []


def test_an_answer_after_the_window_closed_touches_nothing(tmp_path, opened):
    opened.answer = None
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.click()
    dialog.reject()
    opened.done(app_links.NO_APP)
    assert dialog._client is None and dialog._listeners == []
    assert dialog._status.text() == "Opening your signer app…"


def test_only_the_last_opening_is_answered(tmp_path, opened):
    opened.answer = None
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.click()
    first = opened.done
    dialog._open_here_btn.click()                               # opened again
    second, waiting = opened.done, dialog._client
    first(app_links.NO_APP)                                     # the first one's answer
    assert dialog._client is waiting and dialog._status.text() == "Opening your signer app…"
    second(app_links.OPENED)
    assert dialog._status.text() == "Waiting for your signer app. Approve the connection there."
    dialog.reject()


def test_when_the_system_cannot_tell_it_says_so_and_the_code_is_there(tmp_path, opened):
    # RG-M2r2b 3: on a Linux desktop xdg-open does not know, or when
    # xdg-mime cannot answer, the system cannot tell whether an app took
    # the link. No wrong yes or no: the code it holds is there to scan.
    opened.answer = app_links.UNKNOWN
    dialog = dialog_for(tmp_path)
    dialog._open_here_btn.click()
    assert dialog._status.text() == ("Waiting for your signer app. If it didn’t open, scan the "
                                     "code with your phone instead.")
    assert dialog._status.objectName() == "muted"
    assert dark_modules(dialog) and dialog._qr_uri == opened[0]
    assert dialog._client._nc_timer.interval() == connect_dialog._APP_WAIT_S * 1000
    dialog.reject()


def test_a_browser_that_does_not_open_brings_the_code_back(tmp_path, opened):
    settings.save_setting("bunker46_address", "https://b46.example.org")
    opened.browser = False
    dialog = dialog_for(tmp_path)
    dialog._bunker46_btn.click()
    turn()
    assert dialog._status.text() == "Your browser couldn’t be opened."
    assert dialog._status.objectName() == "error"
    assert dialog._renew_timer.isActive() and dialog._client in dialog._listeners
    dialog.reject()


# -- plain words, the answer in the tooltip (RG-M2r2b 6) --------------------

@pytest.mark.parametrize("reason, words", [
    ("timed out waiting for signer", "Your signer app did not answer."),
    ("could not deliver request to any relay (wss://a: blocked)",
     "Your signer app did not answer."),
    ("user rejected", "Your signer app declined the connection."),
    ("Unknown client", "Your signer app declined the connection."),
    ("permission denied", "Your signer app declined the connection."),
    ("secret did not match (possible spoof attempt)",
     "The answer was meant for another connection"),
    ("invalid: 42", "The connection didn’t work."),
])
def test_a_pairing_that_did_not_work_is_said_plainly_and_the_answer_is_in_the_tooltip(
        tmp_path, monkeypatch, reason, words):
    calls = []
    monkeypatch.setattr(BunkerClient, "connect_to_bunker",
                        lambda self, uri, on_success, on_failure: calls.append(on_failure))
    dialog = dialog_for(tmp_path)
    paste(dialog)
    calls[0](reason)
    shown = dialog._status.text()
    assert shown.startswith(words) and dialog._status.objectName() == "error"
    for raw in ("timed out", "deliver", "rejected", "unknown client", "denied", "spoof",
                "secret", "invalid", "42", "wss", "relay", ":"):
        assert raw not in shown.lower(), raw
    assert reason in dialog.help_button.toolTip()
    assert dialog.help_button.accessibleDescription() == dialog.help_button.toolTip()
    dialog.reject()


def test_a_listening_code_that_fails_is_said_plainly_too(tmp_path):
    dialog = dialog_for(tmp_path)
    dialog._on_pair_failure("secret did not match (possible spoof attempt)", dialog._client)
    assert dialog._status.text().startswith("The answer was meant for another connection")
    assert "spoof" not in dialog._status.text()
    assert not dialog._regen_btn.isHidden() and not dark_modules(dialog)
    dialog.reject()


def test_the_focus_goes_back_to_what_opened_the_bunker46_address(tmp_path, opened):
    # RG-M2r2b 14: Details, Change, then Cancel or Open: back on Change.
    settings.save_setting("bunker46_address", "https://b46.example.org")
    dialog = dialog_for(tmp_path)
    dialog.show()
    dialog.details_toggle.setChecked(True)
    dialog._bunker46_change.click()
    assert dialog.focusWidget() is dialog._bunker46_edit
    dialog._bunker46_back.click()
    assert dialog.focusWidget() is dialog._bunker46_change
    dialog._bunker46_change.click()
    dialog._bunker46_edit.setText("https://other.example.org")
    dialog._bunker46_go.click()
    assert dialog.focusWidget() is dialog._bunker46_change
    dialog.reject()


def test_the_code_is_drawn_in_the_screens_pixels_three_a_module(tmp_path, monkeypatch):
    # RG-M2r2b 7: error correction L, at least 3 logical pixels a module,
    # drawn at the window's device pixel ratio, in its room of 280.
    from nostr.qr import qr_matrix
    import segno
    for ratio in (1.0, 2.0):
        monkeypatch.setattr(ConnectDialog, "devicePixelRatioF", lambda _self, r=ratio: r)
        dialog = dialog_for(tmp_path)
        pixmap = dialog._code_pixmap
        code = segno.make(dialog._qr_uri, error="l")
        assert code.error == "L" and code.version <= 17
        modules = len(qr_matrix(dialog._qr_uri, "l")) + 8
        assert pixmap.devicePixelRatio() == ratio
        assert pixmap.width() % modules == 0
        assert pixmap.width() // modules >= 3 * ratio
        assert pixmap.deviceIndependentSize().width() <= connect_dialog._QR_SIZE
        dialog.reject()
