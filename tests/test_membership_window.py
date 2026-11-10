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
  benefits.

  The member page says what is done at a glance: a card with the year and
  until when the membership runs, then one row per thing MyEditor sets
  up, each with either a status in words (only when it is done) or the
  one button that is left, never both. What needs the membership service
  is said to be unavailable when it cannot be reached, with Try Again,
  rather than vanishing. The rest is in Manage; Done starts with the
  focus.

  One default button, at the trailing edge; Go Back at the leading edge.

The API and the payment watcher are fakes with the real ones' shape; no
network, no signer, no modal loop.
"""

import datetime
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from nostr import einundzwanzig_api as e21  # noqa: E402
from nostr.einundzwanzig import BENEFITS_URL, JOIN_URL, MEMBER_SERVICES  # noqa: E402
from nostr.ui import membership_window as mw  # noqa: E402
from nostr.ui.membership_window import (  # noqa: E402
    APPLY, MEMBER, OVERVIEW, PAY, WORKING, MembershipWindow,
)
from tests.accessibility import out_of_tab_reach, unnamed_controls  # noqa: E402

PUBKEY = "ab" * 32
GB = 1024 ** 3


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class Alerts(list):
    """Every failure the window says (alerts.ask_try_again); each is
    answered with ``again`` (False is Cancel)."""

    again = False


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    shown = Alerts()
    monkeypatch.setattr(mw, "ask_try_again",
                        lambda parent, **kw: shown.append(kw) or shown.again)
    monkeypatch.setattr(mw, "confirm_destructive", lambda parent, **kw: True)
    return shown


@pytest.fixture(autouse=True)
def questions(monkeypatch):
    """Every question the window asks; each is answered with ``answer``
    (False is Not Now)."""
    asked = SimpleNamespace(kw=[], answer=False)
    monkeypatch.setattr(mw, "ask", lambda parent, **kw: asked.kw.append(kw) or asked.answer)
    return asked


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


def window(api=None, *, pubkey=PUBKEY, known_member=None, handle=None,
           signs_locally=False, prices=None, profile_address="", relay_listed=False,
           member_year=None, storage_used=None):
    watchers = []

    def factory(api_, year, parent):
        watcher = FakeWatcher(api_, year)
        watchers.append(watcher)
        return watcher

    win = MembershipWindow(api if api is not None else FakeApi(), pubkey=pubkey,
                           known_member=known_member, handle=handle, is_dark=False,
                           signs_locally=signs_locally, profile_address=profile_address,
                           relay_listed=relay_listed, member_year=member_year,
                           storage_used=storage_used,
                           watcher_factory=factory,
                           prices=prices)
    win.watchers = watchers
    return win


def recorder(signal):
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


def default_button(win):
    return next(b for b in win.buttons.values() if b.isDefault())


def manage(win):
    """Manage's commands as its menu lists them, a separator as "-"."""
    return ["-" if action.isSeparator() else action.text()
            for action in win._manage_menu.actions()]


def choose(win, command):
    """Choose ``command`` in Manage, as a click on it does."""
    assert win.buttons["manage"].menu() is win._manage_menu
    next(a for a in win._manage_menu.actions() if a.text() == command).trigger()


def shows(widget) -> bool:
    """Whether ``widget`` is shown with its page (the window itself need
    not be on screen)."""
    return widget.isVisibleTo(widget.window())


def status_of(row) -> str:
    """The words a row's status says, or "" when it shows none."""
    return row.status_label.accessibleName() if shows(row.status_label) else ""


def button_of(row) -> str:
    """The title of a row's button, or "" when it has none."""
    return row.button.text() if shows(row.button) else ""


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
    assert win._fee_label.text() == "Annual fee for 2026: 21,000 sats"


class FakePrices:
    """The price lookup's shape; ``answer`` hands over today's price."""

    def __init__(self):
        self.asked = []

    def lookup(self, on_done):
        self.asked.append(on_done)

    def answer(self, prices):
        for on_done in self.asked:
            on_done(prices)


PRICES = e21.Prices(chf=100_000.0, eur=110_000.0)    # one bitcoin


def labels(widget):
    from PySide6.QtWidgets import QLabel
    return [label.text() for label in widget.findChildren(QLabel)]


def test_the_overview_names_everything_the_association_lists():
    win = window()
    shown = labels(win._pages[OVERVIEW])
    titles = [t for t in shown if t in {
        "Members’ relay", "Nostr address", "Lightning watchtower", "Media storage",
        "Community group", "Buzz (experimental)"}]
    # All six, in the association's order.
    assert titles == ["Members’ relay", "Nostr address", "Lightning watchtower",
                      "Media storage", "Community group", "Buzz (experimental)"]
    assert any("5 GB in total, up to 1 GB per file." in t for t in shown)
    assert not any("\u2014" in t for t in shown)


def test_the_overview_links_to_the_associations_own_list():
    from nostr.einundzwanzig import BENEFITS_URL
    win = window()
    opened = recorder(win.link_activated)
    link = [b for b in win._pages[OVERVIEW].findChildren(QPushButton)
            if b.text() == "Details on the EINUNDZWANZIG Website"][0]
    link.click()
    assert opened == [(BENEFITS_URL,)]
    assert win.result() == 0                       # it opens the page, and stays open


def test_the_member_page_points_to_the_services_set_up_elsewhere():
    win = window(known_member=True)
    row = win._more_row
    assert row.detail_label.text() == ("Lightning watchtower, community group and Buzz "
                                       "(in testing) are set up on the association’s "
                                       "website.")
    assert status_of(row) == ""              # nothing here is done in MyEditor
    assert row.button.accessibleName() == "Open Guide"
    assert "browser" in row.button.accessibleDescription()
    opened = recorder(win.link_activated)
    row.button.click()
    assert opened == [(BENEFITS_URL,)]
    assert win.result() == 0                       # it opens the guide, and stays open


def test_the_sentence_names_every_service_set_up_elsewhere():
    # The sentence names them in words of its own: a service the
    # association adds, or one MyEditor starts to set up, changes it.
    assert [s.key for s in MEMBER_SERVICES if not s.in_app] == ["watchtower", "group", "buzz"]


def test_the_fee_is_shown_in_sats_then_francs_then_euros():
    api = FakeApi()
    prices = FakePrices()
    win = window(api, prices=prices)
    api.last("config").ok(config())
    assert len(prices.asked) == 1
    prices.answer(PRICES)
    # 21,000 sats is exact; the francs and euros are converted.
    assert win._fee_label.text() == ("Annual fee for 2026: 21,000 sats "
                                     "(about 21 CHF, about 23 EUR)")


def test_a_fee_in_francs_leads_with_its_converted_sats():
    api = FakeApi()
    prices = FakePrices()
    win = window(api, prices=prices)
    api.last("config").ok(replace(config(), fee=21, currency="CHF"))
    assert win._fee_label.text() == "Annual fee for 2026: 21 CHF"
    prices.answer(PRICES)
    assert win._fee_label.text() == ("Annual fee for 2026: about 21,000 sats "
                                     "(21 CHF, about 23 EUR)")


def test_without_a_price_the_fee_stays_as_the_association_states_it():
    api = FakeApi()
    prices = FakePrices()
    win = window(api, prices=prices)
    api.last("config").ok(config())
    prices.answer(None)
    assert win._fee_label.text() == "Annual fee for 2026: 21,000 sats"


def test_without_a_signer_the_way_on_is_connecting_one():
    win = window(pubkey=None)
    asked = recorder(win.connect_requested)
    assert default_button(win).text() == "Connect Signer App…"
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


def test_without_the_service_the_member_page_says_what_is_not_available():
    # The service has no key, or cannot be reached: what needs it is not
    # offered, and the page says so instead of letting it vanish.
    win = window(FakeApi(False), known_member=True)
    assert shows(win._service_line)
    assert win._service_line.label.text() == ("Choosing an address, receipts and data "
                                              "export aren’t available right now.")
    assert win._address_row.detail_label.text() == "Not chosen yet"
    assert button_of(win._address_row) == "" and status_of(win._address_row) == ""
    assert "manage" not in win.buttons             # nothing in it applies


def test_try_again_asks_the_service_again_and_brings_back_what_needs_it():
    api = FakeApi(False, answer_later=True)
    win = window(api, known_member=True, handle="hal")
    api.pending_check()
    assert shows(win._service_line)
    assert manage(win) == ["Copy Nostr Address"]
    retry = win._retry_link
    assert retry.text() == "Try Again"
    api._service = True                            # reachable again
    retry.click()
    assert api.names().count("check_service") == 2
    assert retry.text() == "Checking…" and not retry.isEnabled()
    api.pending_check()
    assert not shows(win._service_line)
    assert "Export My Data…" in manage(win)


def test_a_build_without_a_service_does_not_offer_to_try_again():
    win = window(FakeApi(None), known_member=True)
    assert shows(win._service_line)
    assert not shows(win._retry_link)


def test_a_known_member_goes_straight_to_what_is_active():
    api = FakeApi()
    win = window(api, known_member=True, handle="satoshi")
    assert win.page == MEMBER
    assert api.names() == ["check_service", "config"]
    assert win._address_row.detail_label.text() == "satoshi@einundzwanzig.space"
    assert not shows(win._service_line)
    # Details cost a signature, so they come on request.
    assert "Membership Details" in manage(win)


def test_the_member_page_opens_on_the_card_not_a_title():
    win = window(known_member=True, handle="satoshi", member_year=2026)
    card = win._card
    assert card.brand.text() == "EINUNDZWANZIG"
    assert card.title.text() == "Member 2026"
    assert card.address.text() == "satoshi@einundzwanzig.space"
    assert card.status.text() == "Active · until December 31, 2026"
    # Read with a pause, not as "middle dot".
    assert card.status.accessibleName() == "Active, until December 31, 2026"
    # The window's title names the window; the page does not say it again.
    assert not [label for label in win._pages[MEMBER].findChildren(QLabel)
                if label.objectName() == "title"]
    assert win._note.text() == ("Membership runs for a calendar year. You renew it "
                                "yourself; nothing is charged automatically.")


def test_the_card_never_shows_a_key():
    win = window(known_member=True)
    assert not shows(win._card.address)
    texts = " ".join(label.text() for label in win._card.findChildren(QLabel))
    assert PUBKEY not in texts and "npub" not in texts


def test_the_card_names_the_year_the_membership_is_known_for():
    # The year the association says is paid, else the year of the roster
    # that lists the member, else this one.
    assert window(known_member=True)._card.title.text() == (
        f"Member {datetime.date.today().year}")
    api = FakeApi()
    win = window(api, known_member=True, member_year=2025)
    assert win._card.title.text() == "Member 2025"
    choose(win, "Membership Details")
    api.last("me").ok(status(e21.STATUS_MEMBER, year=2027, paid=True))
    assert win.page == MEMBER
    assert win._card.title.text() == "Member 2027"
    assert win._card.status.text() == "Active · until December 31, 2027"
    assert win._note.text() == "Your membership is paid for 2027."


def test_a_fee_year_not_paid_yet_does_not_move_the_card():
    # In December the association may already collect next year's fee.
    api = FakeApi()
    win = window(api, known_member=True, member_year=2026)
    choose(win, "Membership Details")
    api.last("me").ok(status(e21.STATUS_MEMBER, year=2027, paid=False))
    assert win._card.title.text() == "Member 2026"


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
    # The language's own way of writing a day, not a padded fixed pattern.
    assert "Version 1.3, adopted April 20, 2024." == win._statutes_label.text()
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
    # Lessons 36: what did not happen, one sentence with the cause and what
    # to do, what the server said only behind Details, Try Again and Cancel.
    api = FakeApi()
    win = window(api)
    win.buttons["continue"].click()
    api.last("me").fail(e21.ApiError(e21.ErrorCode.SIGNER_UNREACHABLE, message="timed out"))
    assert win.page == OVERVIEW
    shown, = alerts
    assert shown["title"] == "Your membership couldn’t be checked"
    assert shown["message"] == "Your signer app didn’t answer: open it and try again."
    assert shown["details"] == "signer_unreachable: timed out"
    for word in ("NIP", "401", "bolt11", "kind", "timed out"):
        assert word not in shown["title"] + shown["message"]


@pytest.mark.parametrize("ask, title", [
    ("me", "Your membership couldn’t be checked"),
    ("create_invoice", "The invoice wasn’t created"),
    ("apply", "Your application wasn’t sent"),
    ("export_data", "Your data wasn’t exported"),
    ("erase", "Your data wasn’t deleted"),
])
def test_try_again_asks_again_what_did_not_work(alerts, ask, title):
    api = FakeApi()
    if ask in ("export_data", "erase"):
        win = window(api, known_member=True)
        choose(win, "Export My Data…" if ask == "export_data" else "Delete My Data…")
    else:
        win = window(api)
        api.last("config").ok(config())
        win.buttons["continue"].click()
        if ask == "create_invoice":
            api.last("me").ok(status(e21.STATUS_AWAITING_PAYMENT))
        elif ask == "apply":
            api.last("me").ok(status(e21.STATUS_NONE))
            win._consent.setChecked(True)
            win.buttons["send"].click()

    def asked() -> int:
        return len([call for call in api.calls if call.name == ask])

    before = asked()
    alerts.again = True
    api.last(ask).fail(e21.ApiError(e21.ErrorCode.SERVER, status=503))
    assert [a["title"] for a in alerts] == [title]
    assert alerts[0]["message"] == ("EINUNDZWANZIG had a problem just now: try again in a few "
                                    "minutes.")
    assert alerts[0]["details"] == "server (503)"
    assert asked() == before + 1                    # asked again, the same request
    alerts.again = False
    api.last(ask).fail(e21.ApiError(e21.ErrorCode.SERVER, status=503))
    assert len(alerts) == 2 and asked() == before + 1   # Cancel: nothing more


def test_a_payment_check_that_did_not_work_is_said_once_and_try_again_checks_again(alerts):
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    win.buttons["paid"].click()
    first = win.watchers[-1]
    alerts.again = True
    first.failed.emit(e21.ApiError(e21.ErrorCode.TIMEOUT))
    assert [a["title"] for a in alerts] == ["Your payment couldn’t be checked"]
    assert alerts[0]["message"] == ("EINUNDZWANZIG took too long to answer: check your "
                                    "internet connection and try again.")
    assert len(win.watchers) == 2 and win.watchers[-1].started == [True]


def test_data_that_could_not_be_saved_is_said_with_the_systems_words_behind_details(
        alerts, monkeypatch, tmp_path):
    api = FakeApi()
    win = window(api, known_member=True)
    places = [str(tmp_path / "missing" / "membership.json"), str(tmp_path / "membership.json")]
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (places.pop(0), "")))
    alerts.again = True
    choose(win, "Export My Data…")
    api.last("export_data").ok(SimpleNamespace(document={"membership_status": "member"}))
    shown, = alerts
    assert shown["title"] == "Your data wasn’t saved"
    assert shown["message"] == ("This computer couldn’t write the file there: choose another "
                                "place and try again.")
    assert "missing" in shown["details"] and "missing" not in shown["message"]
    assert (tmp_path / "membership.json").exists()          # Try Again: another place


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


def test_the_invoice_shows_its_exact_sats_and_the_fee_in_francs_and_euros(monkeypatch):
    monkeypatch.setattr(e21.Invoice, "amount_sats", property(lambda self: 20_987))
    api = FakeApi()
    prices = FakePrices()
    win = window(api, prices=prices)
    inv = invoice()
    to_pay(win, api, inv=replace(inv, payment=replace(inv.payment, amount=21, currency="CHF")))
    assert win._amount.text() == "20,987 sats"
    assert win._amount_detail.text() == "21 CHF"
    prices.answer(PRICES)
    assert win._amount.text() == "20,987 sats"
    assert win._amount_detail.text() == "21 CHF, about 23 EUR"
    assert len(prices.asked) == 1     # once per window, not once per page


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


def test_paying_is_the_code_and_its_text_twin():
    # Owner decision (Q6): the code is the way to pay, with Copy Invoice for
    # a wallet on this computer or anyone who cannot scan. No wallet hand-off,
    # and no browser while there is a code.
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    assert not win._qr.isHidden() and not win._copy_button.isHidden()
    assert win._browser_button.isHidden()
    assert "Scan the code" in win._pay_intro.text()
    assert not hasattr(win, "_wallet_button")


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
    # Welcomed once, in place of how membership runs; the card says the rest.
    assert win._note.text() == ("Welcome to EINUNDZWANZIG. Thank you for supporting "
                                "the community.")
    assert win._card.title.text() == "Member 2026"
    assert manage(win)[0] == "Receipt for 2026"


def test_closing_the_window_stops_checking():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    watcher = win.watchers[0]
    win.reject()
    assert watcher.stopped >= 1
    assert api.canceled >= 1


# -- being a member: the members' relay ------------------------------------------------

def test_a_relay_list_with_the_members_relay_is_said_to_have_it():
    win = window(known_member=True, relay_listed=True)
    row = win._relay_row
    assert row.detail_label.text() == ("Readers find your notes and articles at "
                                       "nostr.einundzwanzig.space.")
    assert status_of(row) == "In your relay list"
    assert row.status_label.text() == "✓ In your relay list"   # the mark, never alone
    assert button_of(row) == ""


def test_adding_the_relay_is_asked_of_the_window_and_then_said_to_be_done():
    win = window(known_member=True)
    row = win._relay_row
    asked = recorder(win.add_relay_requested)
    assert button_of(row) == "Add" and status_of(row) == ""
    row.button.click()
    assert asked == [()]
    assert not row.button.isEnabled()
    assert row.note_label.text().startswith("Adding…")
    win.set_relay_result("Added to your relay list.", done=True)
    assert status_of(row) == "In your relay list" and button_of(row) == ""
    assert not shows(row.note_label)               # the status says it


def test_a_signer_account_is_told_where_to_look():
    win = window(FakeApi(), known_member=True)
    win._relay_row.button.click()
    assert "signer app" in win._relay_row.note_label.text()
    local = window(FakeApi(), known_member=True, signs_locally=True)
    local._relay_row.button.click()
    assert "signer" not in local._relay_row.note_label.text().lower()


def test_no_relay_list_offers_to_create_the_recommended_one_on_request():
    win = window(known_member=True)
    row = win._relay_row
    asked = recorder(win.add_relay_requested)
    publish = recorder(win.publish_relay_list_requested)
    row.button.click()
    win.set_relay_result("You don’t have a relay list yet.", offer_list=True)
    assert publish == []                           # never without a click
    assert button_of(row) == "Create Relay List" and row.button.isEnabled()
    assert row.note_label.text() == "You don’t have a relay list yet."
    row.button.click()
    assert publish == [()] and asked == [()]
    assert row.note_label.text().startswith("Publishing…")


def test_a_list_that_could_not_be_read_offers_to_try_again():
    win = window(known_member=True)
    row = win._relay_row
    asked = recorder(win.add_relay_requested)
    row.button.click()
    win.set_relay_result("Try again later.")
    assert button_of(row) == "Add" and row.button.isEnabled()
    assert row.note_label.text() == "Try again later."
    row.button.click()
    assert asked == [(), ()]


# -- being a member: media storage -------------------------------------------------------

def test_storage_says_the_allowance_until_the_library_has_measured_it():
    win = window(known_member=True)
    assert win._storage_row.detail_label.text() == ("5 GB for images and videos · up to "
                                                    "1 GB per file")
    assert not shows(win._usage_bar)


def test_storage_says_what_is_used_as_the_library_last_listed_it():
    used = {"bytes": int(1.2 * GB)}
    win = window(known_member=True, storage_used=lambda: used["bytes"])
    assert win._storage_row.detail_label.text() == ("1.2 GB of 5 GB used · up to 1 GB "
                                                    "per file")
    assert win._storage_row.detail_label.accessibleName() == (
        "1.2 GB of 5 GB used, up to 1 GB per file")
    bar = win._usage_bar
    assert shows(bar) and bar.value() == 240 and bar.maximum() == 1000
    assert bar.accessibleName() == "Storage used"
    # Read again whenever the window comes back to the front (from the
    # Media Library, say), so it shows what the library listed meanwhile.
    used["bytes"] = int(4.6 * GB)
    QApplication.sendEvent(win, QEvent(QEvent.Type.WindowActivate))
    assert win._storage_row.detail_label.text().startswith("4.6 GB of 5 GB used")
    assert bar.property("tone") == "full"           # said in the numbers, and in red


def test_open_media_library_asks_for_the_library():
    win = window(known_member=True)
    asked = recorder(win.media_library_requested)
    assert button_of(win._storage_row) == "Open Media Library"
    win._storage_row.button.click()
    assert asked == [()]


# -- being a member: the Nostr address ---------------------------------------------------

def test_choosing_an_address_later_sends_only_the_address():
    api = FakeApi()
    win = window(api, known_member=True)
    row = win._address_row
    assert row.detail_label.text() == "Not chosen yet"
    assert button_of(row) == "Choose…"
    row.button.click()
    assert win.page == APPLY
    assert win._statutes_box.isHidden() and win._email_row.isHidden()
    send = win.buttons["send"]
    assert send.text() == "Save Address" and not send.isEnabled()
    win._handle_edit.setText("hal")
    send.click()
    assert api.last("apply").kw == {"statutes_accepted": e21.UNSET, "nip05_handle": "hal"}
    api.last("apply").ok(status(e21.STATUS_MEMBER, paid=True))
    assert win.page == MEMBER
    assert row.detail_label.text() == "hal@einundzwanzig.space"
    assert row.detail_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert row.note_label.text().startswith("Your Nostr address is saved.")
    assert win._card.address.text() == "hal@einundzwanzig.space"


def choose_address(win, api, name="hal"):
    win._address_row.button.click()
    win._handle_edit.setText(name)
    win.buttons["send"].click()
    api.last("apply").ok(status(e21.STATUS_MEMBER, paid=True))


def test_statutes_that_could_not_be_loaded_are_said_so_and_asked_for_again(alerts):
    api = FakeApi()
    win = window(api)                               # the fee's config never answers
    win.buttons["continue"].click()
    api.last("me").ok(status(e21.STATUS_NONE))
    asked = len([call for call in api.calls if call.name == "config"])
    alerts.again = True
    api.last("config").fail(e21.ApiError(e21.ErrorCode.OFFLINE))
    assert [a["title"] for a in alerts] == ["The statutes couldn’t be loaded"]
    assert len([call for call in api.calls if call.name == "config"]) == asked + 1


def test_an_address_that_was_not_saved_is_said_so_and_try_again_sends_it_again(alerts):
    api = FakeApi()
    win = window(api, known_member=True)
    win._address_row.button.click()
    win._handle_edit.setText("hal")
    win.buttons["send"].click()
    alerts.again = True
    api.last("apply").fail(e21.ApiError(e21.ErrorCode.OFFLINE))
    assert [a["title"] for a in alerts] == ["Your Nostr address wasn’t saved"]
    assert api.last("apply").kw["nip05_handle"] == "hal"
    assert len([call for call in api.calls if call.name == "apply"]) == 2


def test_a_chosen_address_is_offered_for_the_profile_once(questions):
    api = FakeApi()
    win = window(api, known_member=True)
    shown = recorder(win.profile_address_requested)
    choose_address(win, api)
    assert len(questions.kw) == 1
    asked = questions.kw[0]
    assert asked["title"] == "Show Your Nostr Address on Your Profile?"
    assert "hal@einundzwanzig.space" in asked["message"]
    assert [b.label for b in asked["buttons"]] == ["Not Now", "Show on Profile"]
    assert shown == []                                   # Not Now sends nothing
    # Not Now leaves the way to change one's mind on the member page.
    row = win._address_row
    assert button_of(row) == "Add to Profile" and row.button.isEnabled()
    row.button.click()
    assert shown == [("hal@einundzwanzig.space",)]
    assert len(questions.kw) == 1


def test_saying_yes_shows_the_address_and_then_says_it_is_there(questions):
    questions.answer = True
    api = FakeApi()
    win = window(api, known_member=True)
    shown = recorder(win.profile_address_requested)
    choose_address(win, api)
    row = win._address_row
    assert shown == [("hal@einundzwanzig.space",)]
    assert "Updating your profile" in row.note_label.text()
    assert "signer app" in row.note_label.text()
    assert not row.button.isEnabled()
    win.set_profile_address_result("Your profile shows it now.", done=True)
    assert status_of(row) == "On your profile" and button_of(row) == ""
    assert not shows(row.note_label)               # the status says it


def test_a_failed_attempt_can_be_tried_again():
    win = window(known_member=True, handle="hal")
    row = win._address_row
    shown = recorder(win.profile_address_requested)
    row.button.click()
    win.set_profile_address_result("Your profile wasn’t changed. Try again in a moment.")
    assert button_of(row) == "Add to Profile" and row.button.isEnabled()
    assert row.note_label.text() == "Your profile wasn’t changed. Try again in a moment."
    row.button.click()
    assert len(shown) == 2


def test_a_profile_that_shows_the_address_is_said_to_show_it(questions):
    win = window(known_member=True, handle="hal", profile_address="HAL@einundzwanzig.space")
    row = win._address_row
    assert status_of(row) == "On your profile" and button_of(row) == ""
    assert questions.kw == []


def test_another_address_on_the_profile_is_named_before_it_is_replaced(questions):
    api = FakeApi()
    win = window(api, known_member=True, profile_address="hal@example.com")
    choose_address(win, api)
    assert "It replaces hal@example.com." in questions.kw[0]["message"]


def test_joining_with_an_address_offers_it_for_the_profile(questions):
    api = FakeApi()
    win = window(api, handle="hal")
    to_pay(win, api)
    default_button(win).click()
    win.watchers[0].paid.emit(invoice(paid=True))
    api.last("me").ok(status(e21.STATUS_MEMBER, paid=True))
    assert win.page == MEMBER
    assert len(questions.kw) == 1


def test_a_saved_address_is_told_to_the_app():
    api = FakeApi()
    win = window(api, known_member=True)
    saved = recorder(win.address_saved)
    choose_address(win, api)
    assert saved == [(PUBKEY, "hal")]


# -- being a member: Manage and Done ------------------------------------------------------

def test_manage_holds_what_applies_with_delete_last_and_apart():
    win = window(known_member=True, handle="hal")
    assert manage(win) == ["Membership Details", "Change Nostr Address…",
                           "Copy Nostr Address", "-", "Export My Data…", "Delete My Data…"]
    button = win.buttons["manage"]
    assert button.text() == "Manage" and not button.isDefault()
    row = win._button_row
    widgets = [row.itemAt(i).widget() for i in range(row.count()) if row.itemAt(i).widget()]
    assert widgets[0] is button and widgets[-1] is win.buttons["done"]


def test_change_nostr_address_chooses_another_one():
    api = FakeApi()
    win = window(api, known_member=True, handle="hal")
    choose(win, "Change Nostr Address…")
    assert win.page == APPLY and win.buttons["send"].text() == "Save Address"


def test_copy_nostr_address_puts_it_on_the_clipboard(qt_app):
    win = window(known_member=True, handle="hal")
    choose(win, "Copy Nostr Address")
    assert qt_app.clipboard().text() == "hal@einundzwanzig.space"


def test_the_receipt_is_offered_once_the_association_named_it():
    api = FakeApi()
    win = window(api, known_member=True)
    choose(win, "Membership Details")
    api.last("me").ok(status(e21.STATUS_MEMBER, paid=True,
                             receipt="https://pay.einundzwanzig.space/r/1"))
    assert manage(win)[0] == "Receipt for 2026"
    assert "Membership Details" not in manage(win)
    opened = recorder(win.link_activated)
    choose(win, "Receipt for 2026")
    assert opened == [("https://pay.einundzwanzig.space/r/1",)]


def test_membership_details_come_back_to_the_member_page():
    # Cancel and failure from the member page's details go back there,
    # not to the join page.
    api = FakeApi()
    win = window(api, known_member=True)
    choose(win, "Membership Details")
    assert win.page == WORKING
    win.buttons["cancel"].click()
    assert win.page == MEMBER
    choose(win, "Membership Details")
    api.last("me").fail(e21.ApiError(e21.ErrorCode.SIGNER_UNREACHABLE))
    assert win.page == MEMBER


def test_deleting_data_asks_first_and_returns_to_the_start():
    api = FakeApi()
    win = window(api, known_member=True)
    choose(win, "Delete My Data…")
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
    choose(win, "Delete My Data…")
    assert "deletes the personal details of your membership" in seen[0]["message"]
    assert "Fees you paid stay on record for bookkeeping." in seen[0]["message"]
    assert erased == []                            # nothing is gone until it answers
    api.last("erase").ok(SimpleNamespace(erased=True, retained_payments=1))
    assert erased == [(PUBKEY,)]


def test_exported_data_is_saved_where_the_person_says(monkeypatch, tmp_path):
    api = FakeApi()
    win = window(api, known_member=True)
    target = tmp_path / "membership.json"
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    choose(win, "Export My Data…")
    api.last("export_data").ok(SimpleNamespace(document={"membership_status": "member"}))
    assert '"membership_status": "member"' in target.read_text(encoding="utf-8")
    assert win.page == MEMBER and win._note.text() == "Your membership data is saved."


def test_done_starts_with_the_focus_and_nothing_else_looks_chosen():
    win = window(known_member=True, handle="hal")
    assert win.focusWidget() is win.buttons["done"]
    assert win.buttons["done"].isDefault()
    assert sum(b.isDefault() for b in win._pages[MEMBER].findChildren(QPushButton)) == 0


def test_a_check_mark_says_only_what_is_done():
    win = window(known_member=True, handle="hal")     # nothing done yet
    shown = [label.text() for label in win._pages[MEMBER].findChildren(QLabel)
             if shows(label)]
    assert not any("✓" in text for text in shown)


def test_every_control_of_the_member_page_is_named_and_in_reach():
    win = window(known_member=True, handle="hal")
    assert unnamed_controls(win) == []
    assert out_of_tab_reach(win) == []


def test_the_member_page_fits_the_window_without_scrolling(qt_app):
    win = window(known_member=True, handle="hal", storage_used=lambda: GB)
    win.show()
    qt_app.processEvents()
    try:
        assert win._scroll.verticalScrollBar().maximum() == 0
        assert win.buttons["done"].isVisible()
    finally:
        win.close()


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


def test_once_paid_the_invoice_and_its_buttons_go_away():
    api = FakeApi()
    win = window(api)
    to_pay(win, api)
    default_button(win).click()
    win.watchers[0].paid.emit(invoice(paid=True))
    api.last("me").ok(status(e21.STATUS_AWAITING_PAYMENT))   # not confirmed yet
    assert win.page == PAY
    for widget in (win._qr, win._copy_button, win._browser_button):
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
    win = window(api, known_member=True, handle="hal")
    texts = [w.text() for w in win.findChildren(QPushButton)]
    texts += [w.text() for w in win.findChildren(mw.QLabel)]
    texts += manage(win)
    assert all("\u2014" not in t for t in texts)


# -- a page taller than the screen ----------------------------------------------------------

def test_a_very_long_page_scrolls_instead_of_leaving_the_screen():
    win = window()
    win._overview_note.setText("A very long note that wraps over many lines. " * 400)
    win.show()
    QApplication.processEvents()
    available = win.screen().availableGeometry().height()
    assert win.height() <= available
    assert win._scroll.verticalScrollBar().maximum() > 0
    assert default_button(win).isVisible()
    win.close()


def test_the_page_sits_on_the_windows_own_background():
    # A scroll area paints its page by default, which washed out the text
    # (and made it unreadable in dark mode).
    win = window()
    assert not win._stack.autoFillBackground()
    assert not win._scroll.viewport().autoFillBackground()


@pytest.mark.parametrize("long_note", [False, True])
def test_a_page_is_never_cut_off_at_the_side(long_note):
    # The pay page is narrower than the application page; it must be laid
    # out for its own width, inside the window, scrolling or not.
    api = FakeApi()
    win = window(api)
    to_pay(win, api, inv=invoice(bolt11="lnbc210n1pjexample"))
    if long_note:
        win._pay_intro.setText("A very long note that wraps over many lines. " * 400)
    win.show()
    QApplication.processEvents()
    page = win._pages[PAY]
    assert win._stack.minimumSizeHint().width() == page.minimumSizeHint().width()
    assert win._scroll.viewport().width() >= page.minimumSizeHint().width()
    assert win._scroll.geometry().right() <= win.width()
    assert win._pay_intro.width() <= win._scroll.viewport().width()
    win.close()


def test_no_words_on_the_member_page_are_cut_off(qt_app):
    """German words once clipped on this page: every button and status keeps
    its words whole, and a row's line wraps instead."""
    win = window(FakeApi(), known_member=True, handle="hal")
    win.show()
    qt_app.processEvents()
    try:
        for row in (win._relay_row, win._storage_row, win._address_row, win._more_row):
            for widget in (row.button, row.status_label):
                if widget.isVisible():
                    assert widget.width() >= widget.sizeHint().width(), widget.text()
        manage = win.buttons["manage"]
        assert manage.width() >= manage.sizeHint().width()
    finally:
        win.close()
