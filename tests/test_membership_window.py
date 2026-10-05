# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the EINUNDZWANZIG Membership window, page by page.

What must hold:

  Opening the window signs nothing: the only request is the unsigned fee
  lookup. Every signed request starts from a button.

  Without a signer the default action is Connect Signer; without a client
  key it is Join on the Website. Neither offers a dead end.

  The application cannot be sent before the statutes are accepted, a bad
  name is explained under the field, and only the fields filled in are
  sent (an empty email means "no email").

  Paying shows the invoice as a code and offers Copy Invoice and Open in
  Wallet; nothing is polled until the person says they paid, and the
  check ends in a clear next step (Check Again, Create New Invoice).

  A confirmed payment confirms the membership to the window that owns the
  benefits, and the member page names what is active.

  One default button, at the trailing edge; Go Back at the leading edge.

The API and the payment watcher are fakes with the real ones' shape; no
network, no signer, no modal loop.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from nostr import einundzwanzig_api as e21  # noqa: E402
from nostr.einundzwanzig import JOIN_URL  # noqa: E402
from nostr.ui import membership_window as mw  # noqa: E402
from nostr.ui.membership_window import (  # noqa: E402
    APPLY, MEMBER, OVERVIEW, PAY, WORKING, MembershipWindow,
)

PUBKEY = "ab" * 32


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    shown = []
    monkeypatch.setattr(mw, "inform", lambda parent, **kw: shown.append(kw))
    monkeypatch.setattr(mw, "confirm_destructive", lambda parent, **kw: True)
    return shown


class FakeApi:
    """The client's shape. ``service`` is what the membership service says
    when asked: True (can sign people up), False (no key), None (no service
    configured in this build). ``answer_later`` holds the answer back."""

    def __init__(self, service=True, *, answer_later=False):
        self.configured = service is not None
        self._service = bool(service)
        self.calls = []
        self.canceled = 0
        self.pending_check = None
        self._answer_later = answer_later

    def check_service(self, on_done):
        self.calls.append(SimpleNamespace(name="check_service", ok=on_done, fail=None, kw={}))
        if self._answer_later:
            self.pending_check = lambda: on_done(self._service)
        else:
            on_done(self._service)

    def _record(self, name, ok, fail, **kw):
        self.calls.append(SimpleNamespace(name=name, ok=ok, fail=fail, kw=kw))

    def config(self, ok, fail):
        self._record("config", ok, fail)

    def me(self, ok, fail):
        self._record("me", ok, fail)

    def apply(self, ok, fail, **kw):
        self._record("apply", ok, fail, **kw)

    def create_invoice(self, year, ok, fail):
        self._record("create_invoice", ok, fail, year=year)

    def export_data(self, ok, fail):
        self._record("export_data", ok, fail)

    def erase(self, ok, fail):
        self._record("erase", ok, fail)

    def cancel(self):
        self.canceled += 1

    def names(self):
        return [c.name for c in self.calls]

    def last(self, name):
        return [c for c in self.calls if c.name == name][-1]


class FakeWatcher(QObject):
    paid = Signal(object)
    still_waiting = Signal(int)
    expired = Signal(object)
    gave_up = Signal()
    failed = Signal(object)

    def __init__(self, api, year):
        super().__init__()
        self.year = year
        self.started = []
        self.stopped = 0
        self.released = False

    def start(self, *, poll_now=False):
        self.started.append(poll_now)

    def stop(self):
        self.stopped += 1

    def deleteLater(self):
        self.released = True
        super().deleteLater()


def config():
    return e21.MembershipConfig(
        fee=21_000, currency="SATS", year=2026,
        statutes_url="https://einundzwanzig.space/files/Statuten_v1.3.pdf",
        statutes_version="1.3", statutes_adopted_at="2024-04-20",
        required_fields=("statutes_accepted",), optional_fields=(),
        application_text_max_length=2000)


def status(state, *, year=2026, paid=False, receipt=None):
    return e21.MembershipStatus(
        pubkey=PUBKEY, membership_status=state, association_status="DEFAULT",
        statutes_accepted_at=None, applied_at=None,
        current_year=e21.CurrentYear(year=year, fee=21_000, currency="SATS",
                                     paid=paid, receipt_url=receipt))


def invoice(*, paid=False, bolt11="lnbcrt1", checkout="https://pay.einundzwanzig.space/i/x",
            year=2026):
    return e21.Invoice(
        checkout_url=checkout, bolt11=bolt11, created=True,
        payment=e21.FeeEntry(year=year, amount=21_000, currency="SATS", paid=paid,
                             receipt_url=None))


def window(api=None, *, pubkey=PUBKEY, known_member=None, handle=None, opened=None,
           signs_locally=False):
    watchers = []

    def factory(api_, year, parent):
        watcher = FakeWatcher(api_, year)
        watchers.append(watcher)
        return watcher

    win = MembershipWindow(api if api is not None else FakeApi(), pubkey=pubkey,
                           known_member=known_member, handle=handle, is_dark=False,
                           signs_locally=signs_locally,
                           watcher_factory=factory,
                           open_lightning=opened if opened is not None else (lambda b: True))
    win.watchers = watchers
    return win


def recorder(signal):
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


def default_button(win):
    return next(b for b in win.buttons.values() if b.isDefault())


def to_apply(win, api):
    api.last("config").ok(config())
    win.buttons["continue"].click()
    api.last("me").ok(status(e21.STATUS_NONE))


# -- opening ----------------------------------------------------------------------

def test_opening_signs_nothing():
    api = FakeApi()
    win = window(api)
    assert win.page == OVERVIEW
    assert api.names() == ["check_service", "config"]   # availability and fee, unsigned


def test_the_fee_is_shown_as_the_association_states_it():
    api = FakeApi()
    win = window(api)
    api.last("config").ok(config())
    assert win._fee_label.text() == "Annual fee: 21,000 sats for 2026"


def test_without_a_signer_the_way_on_is_connecting_one():
    win = window(pubkey=None)
    asked = recorder(win.connect_requested)
    assert default_button(win).text() == "Connect Signer…"
    default_button(win).click()
    assert asked == [()]


@pytest.mark.parametrize("service", [None, False])
def test_without_a_service_that_can_sign_up_the_way_on_is_the_website(service):
    # None: no service in this build. False: the service has no key.
    api = FakeApi(service)
    win = window(api)
    links = recorder(win.link_activated)
    assert "config" not in api.names()
    assert default_button(win).text() == "Join on the Website"
    default_button(win).click()
    assert links == [(JOIN_URL,)]


def test_continue_waits_until_the_service_has_answered():
    api = FakeApi(True, answer_later=True)
    win = window(api)
    assert default_button(win).text() == "Continue"
    assert not default_button(win).isEnabled()
    assert "Checking" in win._overview_note.text()
    api.pending_check()
    assert default_button(win).isEnabled()
    assert "config" in api.names()


def test_a_member_page_hides_what_needs_the_service_when_it_has_no_key():
    win = window(FakeApi(False), known_member=True)
    assert win._name_button.isHidden() and win._export_link.isHidden()


def test_a_known_member_goes_straight_to_what_is_active():
    api = FakeApi()
    win = window(api, known_member=True, handle="satoshi")
    assert win.page == MEMBER
    assert api.names() == ["check_service", "config"]
    assert win._name_detail.text() == "satoshi@einundzwanzig.space"
    assert not win._details_link.isHidden()   # details cost a signature, so on request


# -- the default button sits where macOS puts it ----------------------------------

def test_one_default_button_at_the_trailing_edge_and_go_back_leading():
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    row = win._button_row
    widgets = [row.itemAt(i).widget() for i in range(row.count()) if row.itemAt(i).widget()]
    assert widgets[0].text() == "Go Back"
    assert widgets[-1].isDefault()
    assert sum(b.isDefault() for b in win.buttons.values()) == 1


# -- applying -------------------------------------------------------------------------

def test_continue_asks_the_signer_and_says_so():
    api = FakeApi()
    win = window(api)
    win.buttons["continue"].click()
    assert win.page == WORKING
    assert "signer app" in win._working_title.text()
    assert api.names()[-1] == "me"


def test_cancel_while_waiting_drops_the_request_and_goes_back():
    api = FakeApi()
    win = window(api)
    win.buttons["continue"].click()
    win.buttons["cancel"].click()
    assert api.canceled == 1
    assert win.page == OVERVIEW


def test_the_application_waits_for_the_statutes():
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    assert win.page == APPLY
    assert "Version 1.3, adopted 20 April 2024." == win._statutes_label.text()
    send = win.buttons["send"]
    assert not send.isEnabled()
    win._consent.setChecked(True)
    assert send.isEnabled()


def test_the_statutes_open_from_the_window():
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    links = recorder(win.link_activated)
    win._statutes_button.click()
    assert links == [(config().statutes_url,)]


def test_a_name_is_lowercased_and_explained_when_it_cannot_work():
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    win._consent.setChecked(True)
    win._handle_edit.setText("")
    win._handle_edit.textEdited.emit("Satoshi")
    assert win._handle_edit.text() == "satoshi"
    assert win._handle_error.isHidden()
    win._handle_edit.setText("sat oshi")
    assert not win._handle_error.isHidden()
    assert not win.buttons["send"].isEnabled()


def test_only_what_was_filled_in_is_sent_and_no_email_says_so():
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    win._consent.setChecked(True)
    win._handle_edit.setText("satoshi")
    win.buttons["send"].click()
    sent = api.last("apply").kw
    assert sent == {"statutes_accepted": True, "nip05_handle": "satoshi", "no_email": True}


def test_an_email_and_a_message_are_sent_when_given():
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    win._consent.setChecked(True)
    win._email_edit.setText("me@example.com")
    win._message_edit.setPlainText("Hello")
    win.buttons["send"].click()
    sent = api.last("apply").kw
    assert sent["email"] == "me@example.com" and sent["no_email"] is False
    assert sent["application_text"] == "Hello"
    assert "nip05_handle" not in sent


def test_a_taken_name_is_shown_under_the_field_not_in_an_alert(alerts):
    api = FakeApi()
    win = window(api)
    to_apply(win, api)
    win._consent.setChecked(True)
    win._handle_edit.setText("satoshi")
    win.buttons["send"].click()
    api.last("apply").fail(e21.ApiError(
        e21.ErrorCode.VALIDATION, status=422,
        field_errors={"nip05_handle": ["The nip05 handle has already been taken."]}))
    assert win.page == APPLY
    assert not win._handle_error.isHidden()
    assert "taken" in win._handle_error.text().lower()
    assert alerts == []


def test_other_failures_are_explained_in_plain_words(alerts):
    api = FakeApi()
    win = window(api)
    win.buttons["continue"].click()
    api.last("me").fail(e21.ApiError(e21.ErrorCode.SIGNER_UNREACHABLE))
    assert win.page == OVERVIEW
    assert alerts and "signer" in alerts[0]["title"].lower()
    for word in ("NIP", "401", "bolt11", "kind"):
        assert word not in alerts[0]["title"] + alerts[0]["message"]


# -- paying -----------------------------------------------------------------------------

def to_pay(win, api, state=e21.STATUS_AWAITING_PAYMENT, inv=None):
    api.last("config").ok(config())
    win.buttons["continue"].click()
    api.last("me").ok(status(state))
    api.last("create_invoice").ok(inv or invoice())


def test_an_application_on_file_goes_to_payment_for_the_current_year(monkeypatch):
    monkeypatch.setattr(e21.Invoice, "amount_sats", property(lambda self: 21_000))
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    assert api.last("create_invoice").kw == {"year": 2026}
    assert win.page == PAY
    assert win._pay_title.text() == "Pay Your 2026 Membership Fee"
    assert win._amount.text() == "21,000 sats"
    assert not win._qr.isHidden() and not win._qr.pixmap().isNull()
    assert default_button(win).text() == "I’ve Paid"


def test_a_lapsed_membership_is_a_renewal(monkeypatch):
    monkeypatch.setattr(e21.Invoice, "amount_sats", property(lambda self: 21_000))
    api = FakeApi()
    win = window(api)
    to_pay(win, api, state=e21.STATUS_LAPSED, inv=invoice(year=2027))
    assert win._pay_title.text() == "Renew Your Membership for 2027"


def test_copy_invoice_puts_the_invoice_on_the_clipboard(qt_app):
    api = FakeApi()
    win = window(api)
    to_pay(win, api, inv=invoice(bolt11="lnbc1copyme"))
    win._copy_button.click()
    assert qt_app.clipboard().text() == "lnbc1copyme"
    assert win._copy_button.text() == "Copied"


def test_open_in_wallet_says_what_to_do_when_no_wallet_answers():
    api = FakeApi()
    win = window(api, opened=lambda bolt11: False)
    to_pay(win, api)
    win._wallet_button.click()
    assert "Scan the code" in win._pay_status.text()


def test_without_an_invoice_code_the_payment_page_is_the_way():
    api = FakeApi()
    win = window(api)
    links = recorder(win.link_activated)
    to_pay(win, api, inv=invoice(bolt11=None))
    assert win._qr.isHidden() and win._copy_button.isHidden()
    assert win._browser_button.text() == "Pay in Browser"
    win._browser_button.click()
    assert links == [("https://pay.einundzwanzig.space/i/x",)]


def test_nothing_is_checked_until_the_person_says_they_paid():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    assert win.watchers == []
    default_button(win).click()
    assert win.watchers[0].started == [True]
    assert win.watchers[0].year == 2026
    assert not default_button(win).isEnabled()


def test_a_check_that_runs_out_offers_check_again():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    win.watchers[0].gave_up.emit()
    assert default_button(win).text() == "Check Again"
    assert "isn’t confirmed yet" in win._pay_status.text()


def test_an_expired_invoice_offers_a_new_one():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    win.watchers[0].expired.emit(invoice())
    assert default_button(win).text() == "Create New Invoice"
    default_button(win).click()
    assert api.names().count("create_invoice") == 2


def test_a_confirmed_payment_confirms_the_membership():
    api = FakeApi()
    win = window(api)
    confirmed = recorder(win.member_confirmed)
    to_pay(win, api)
    default_button(win).click()
    win.watchers[0].paid.emit(invoice(paid=True))
    assert win.page == WORKING
    api.last("me").ok(status(e21.STATUS_MEMBER, paid=True,
                             receipt="https://pay.einundzwanzig.space/r/1"))
    assert confirmed == [(PUBKEY,)]
    assert win.page == MEMBER
    assert win._member_title.text() == "You’re a Member"
    assert "paid for 2026" in win._member_subtitle.text()
    assert not win._receipt_link.isHidden()


def test_closing_the_window_stops_checking():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    watcher = win.watchers[0]
    win.reject()
    assert watcher.stopped >= 1
    assert api.canceled >= 1


# -- being a member -----------------------------------------------------------------------

def test_choosing_an_address_later_sends_only_the_address():
    api = FakeApi()
    win = window(api, known_member=True)
    assert win._name_detail.text() == "Not chosen yet."
    win._name_button.click()
    assert win.page == APPLY
    assert win._statutes_box.isHidden() and win._email_row.isHidden()
    send = win.buttons["send"]
    assert send.text() == "Save Address" and not send.isEnabled()
    win._handle_edit.setText("hal")
    send.click()
    assert api.last("apply").kw == {"statutes_accepted": e21.UNSET, "nip05_handle": "hal"}
    api.last("apply").ok(status(e21.STATUS_MEMBER, paid=True))
    assert win.page == MEMBER
    assert win._name_detail.text() == "hal@einundzwanzig.space"


def test_adding_the_relay_is_asked_of_the_window_and_its_result_shown():
    win = window(known_member=True)
    asked = recorder(win.add_relay_requested)
    win._relay_button.click()
    assert asked == [()]
    assert not win._relay_button.isEnabled()
    win.set_relay_result("Added to your relay list.", done=True)
    assert win._relay_result.text() == "Added to your relay list."
    assert win._relay_button.isHidden()


def test_deleting_data_asks_first_and_returns_to_the_start():
    api = FakeApi()
    win = window(api, known_member=True)
    win._erase_link.click()
    api.last("erase").ok(SimpleNamespace(erased=True, retained_payments=1))
    assert win.page == OVERVIEW
    assert "deleted" in win._overview_note.text()


def test_deleting_data_says_what_happens_and_tells_the_app(monkeypatch):
    seen = []
    api = FakeApi()
    win = window(api, known_member=True)
    erased = recorder(win.data_erased)
    monkeypatch.setattr(mw, "confirm_destructive",
                        lambda parent, **kw: seen.append(kw) or True)
    win._erase_link.click()
    assert "deletes the personal details of your membership" in seen[0]["message"]
    assert "Fees you paid stay on record for bookkeeping." in seen[0]["message"]
    assert erased == []                            # nothing is gone until it answers
    api.last("erase").ok(SimpleNamespace(erased=True, retained_payments=1))
    assert erased == [(PUBKEY,)]


def test_a_saved_address_is_told_to_the_app():
    api = FakeApi()
    win = window(api, known_member=True)
    saved = recorder(win.address_saved)
    win._name_button.click()
    win._handle_edit.setText("hal")
    win.buttons["send"].click()
    api.last("apply").ok(status(e21.STATUS_MEMBER, paid=True))
    assert saved == [(PUBKEY, "hal")]


def test_membership_details_come_back_to_the_member_page():
    # Cancel and failure from the member page's details go back there,
    # not to the join page.
    api = FakeApi()
    win = window(api, known_member=True)
    win._details_link.click()
    assert win.page == WORKING
    win.buttons["cancel"].click()
    assert win.page == MEMBER
    win._details_link.click()
    api.last("me").fail(e21.ApiError(e21.ErrorCode.SIGNER_UNREACHABLE))
    assert win.page == MEMBER


def test_an_account_that_signs_here_is_not_sent_to_a_signer_app():
    api = FakeApi()
    win = window(api, signs_locally=True)
    win.buttons["continue"].click()
    assert win.page == WORKING
    assert "signer" not in win._working_title.text().lower()
    api.last("me").ok(status(e21.STATUS_AWAITING_PAYMENT))
    api.last("create_invoice").ok(invoice())
    default_button(win).click()                    # I've Paid
    assert "signer" not in win._pay_status.text().lower()


def test_a_signer_account_is_told_where_to_look():
    win = window(FakeApi(), known_member=True)
    win._relay_button.click()
    assert "signer app" in win._relay_result.text()
    local = window(FakeApi(), known_member=True, signs_locally=True)
    local._relay_button.click()
    assert "signer" not in local._relay_result.text().lower()


def test_no_relay_list_offers_the_recommended_one_on_request():
    win = window(known_member=True)
    asked = recorder(win.add_relay_requested)
    publish = recorder(win.publish_relay_list_requested)
    win._relay_button.click()
    win.set_relay_result("You don’t have a relay list yet.", offer_list=True)
    assert publish == []                           # never without a click
    assert win._relay_button.text() == "Publish Recommended Relay List"
    assert win._relay_button.isEnabled() and not win._relay_button.isHidden()
    win._relay_button.click()
    assert publish == [()] and asked == [()]


def test_a_list_that_could_not_be_read_offers_to_try_again():
    win = window(known_member=True)
    asked = recorder(win.add_relay_requested)
    win._relay_button.click()
    win.set_relay_result("Try again later.")
    assert win._relay_button.text() == "Add to My Relay List"
    assert win._relay_button.isEnabled()
    win._relay_button.click()
    assert asked == [(), ()]


def test_once_paid_the_invoice_and_its_buttons_go_away():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    win.watchers[0].paid.emit(invoice(paid=True))
    api.last("me").ok(status(e21.STATUS_AWAITING_PAYMENT))   # not confirmed yet
    assert win.page == PAY
    for widget in (win._qr, win._copy_button, win._wallet_button, win._browser_button):
        assert widget.isHidden()
    assert "payment arrived" in win._pay_status.text()
    assert default_button(win).text() == "Check Again"


def test_a_finished_watcher_is_released():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    watcher = win.watchers[0]
    watcher.gave_up.emit()
    assert watcher.released


def test_the_copied_label_resets_on_a_timer_tied_to_the_button(monkeypatch):
    calls = []
    monkeypatch.setattr(mw, "QTimer", SimpleNamespace(
        singleShot=lambda *args: calls.append(args)))
    api = FakeApi()
    win = window(api)
    to_pay(win, api, inv=invoice(bolt11="lnbc1copyme"))
    win._copy_button.click()
    (ms, context, reset), = calls
    assert context is win._copy_button             # gone with the window, never late
    reset()
    assert win._copy_button.text() == "Copy Invoice"


def test_a_late_service_answer_after_closing_is_ignored():
    api = FakeApi(True, answer_later=True)
    win = window(api)
    win.reject()
    api.pending_check()                            # arrives after the window closed
    assert "config" not in api.names()


def test_the_window_owns_its_client_and_lets_go_of_a_replaced_one(qt_app):
    from PySide6.QtCore import QCoreApplication, QEvent
    import shiboken6

    class QtApi(QObject):
        configured = False

        def __init__(self):
            super().__init__()
            self.canceled = 0

        def check_service(self, on_done):
            on_done(False)

        def cancel(self):
            self.canceled += 1

    first, second = QtApi(), QtApi()
    win = window(first)
    assert first.parent() is win
    win.set_identity(second, pubkey=PUBKEY)
    assert first.canceled == 1 and second.parent() is win
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not shiboken6.isValid(first)
    assert shiboken6.isValid(second)


def test_no_text_in_the_window_uses_an_em_dash():
    api = FakeApi()
    win = window(api, known_member=True)
    texts = [w.text() for w in win.findChildren(QPushButton)]
    texts += [w.text() for w in win.findChildren(mw.QLabel)]
    assert all("\u2014" not in t for t in texts)
