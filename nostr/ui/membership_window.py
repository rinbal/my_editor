# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""EINUNDZWANZIG Membership: join the association, pay the fee, see what it unlocks.

One window with a page for each stage of joining, in the order it happens:

    overview   what membership is, what it costs, and Continue
    apply      the statutes, consent, and the optional details
    pay        the invoice as a QR code to scan with a wallet, and Copy Invoice
    member     the membership card, then what MyEditor set up and the one
               step left for each, and Manage for the rest
    working    the short wait while the signer and the association answer

The window follows Apple's Human Interface Guidelines the way the rest of
the app does: one default button at the trailing edge with Cancel-type
buttons beside it, Go Back at the leading edge, field problems shown under
the field they belong to, progress shown where the work happens, and plain
words throughout (no protocol names, status codes or key formats).

The member page is read at a glance, as System Settings shows an
account's iCloud+: a card with the year and until when the membership
runs (it is renewed by hand, and nothing is charged automatically), then
a row for each thing MyEditor sets up. A row says it is done, in words
beside a check mark, only when it is (the relay list has the members'
relay, the profile shows the address); otherwise it offers the one
button left. What the association's guide sets up gets a row of its
own, without a check mark. What needs the membership service is said to
be unavailable while the service can't be reached, with Try Again,
rather than vanishing. Receipt, details, export and deletion are in
Manage; Done starts with the focus.

Nothing is signed by opening the window. The fee is read without a
signature; every request that needs one (status, application, invoice,
payment check) starts from a button the person clicked, and the working
page says when their signer app will ask (an account whose key is kept
on this computer signs without asking, and is not told to look for a
prompt). The window is not modal: paying from a phone can take a minute,
and the editor stays usable meanwhile.

The window owns the API client it is given, so closing it ends every
request, and an answer that arrives after it closed is dropped.

The association API lives in nostr/einundzwanzig_api/. This module only
decides what to show; it never touches the network itself.
"""

from __future__ import annotations

import datetime
import json
from typing import Callable, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from alerts import CANCEL, Button, ask, confirm_destructive, inform
from atomic_file import save_text_document
from alerts import DEFAULT as ALERT_DEFAULT
import i18n
from i18n import _
from media_files import format_storage
from nostr import einundzwanzig_api as e21
from nostr.einundzwanzig import (
    BENEFITS_URL, JOIN_URL, MAX_FILE_LABEL, MEMBER_RELAY, MEMBER_SERVICES, NIP05_DOMAIN,
    PER_USER_BYTES, PER_USER_LABEL, nip05_address,
)
from nostr.qr import make_qr_pixmap
from nostr.ui import grouped_list
from . import appearance
from nostr.ui.assistant import (
    DEFAULT, LEADING, NORMAL, AssistantWindow, busy_bar, link_button, page, text_label,
)
from nostr.ui.grouped_list import ListGroup, ListRow, Notice, group_header, usage_bar
from nostr.ui.membership_card import ORANGE, MembershipCard

# Pages.
OVERVIEW = "overview"
WORKING = "working"
APPLY = "apply"
PAY = "pay"
MEMBER = "member"

_QR_SIZE = 216
_ASK_SIGNER = _("Approve the request in your signer app.")
_ONE_MOMENT = _("One moment.")
_SIGNER_MAY_ASK = _("Your signer app may ask you to approve.")
_DEFAULT_NOTE = _("Membership belongs to your Nostr account and runs for a calendar "
                  "year. You renew it yourself; nothing is charged automatically.")
_CALENDAR_NOTE = _("Membership runs for a calendar year. You renew it yourself; nothing "
                   "is charged automatically.")

# The member page's rows: each one's tile, as System Settings tints its
# icons (the relay in EINUNDZWANZIG's orange).
_RELAY_TINT = ORANGE.name()
_STORAGE_TINT = "#0A84FF"
_ADDRESS_TINT = "#5E5CE6"
_MORE_TINT = "#8E8E93"
# From here the storage is said to be almost full, in the bar's color too.
_ALMOST_FULL = 0.9
# The member page is never narrower than this, so each row's words and its
# button sit side by side with room to spare, German ones too, whatever
# the line under the card says.
_MEMBER_PAGE_WIDTH = 512


def format_date(iso: str) -> str:
    """``April 20, 2024`` from ``2024-04-20`` (``20. April 2024`` in German),
    the language's own way of writing a day; the input unchanged if it is
    not a date."""
    try:
        day = datetime.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return i18n.format_day(day)


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def _service_title(service) -> str:
    if service.experimental:
        return _("{service} (experimental)").format(service=service.title)
    return service.title


def _service_detail(service) -> str:
    return f"{service.summary} {service.note}" if service.note else service.summary


class MembershipWindow(AssistantWindow):
    """Joining EINUNDZWANZIG, start to finish, for the active Nostr identity."""

    connect_requested = Signal()      # the person wants to connect a signer first
    link_activated = Signal(str)      # a web address for the window to open
    member_confirmed = Signal(str)    # the association confirmed this pubkey as a member
    add_relay_requested = Signal()    # add the members' relay to the relay list
    # publish the recommended relay list (with the members' relay) for an
    # account that has none; only ever after the person clicked for it
    publish_relay_list_requested = Signal()
    address_saved = Signal(str, str)  # pubkey, the Nostr address name the association took
    # show this Nostr address on the profile; only ever after the person
    # clicked for it
    profile_address_requested = Signal(str)
    data_erased = Signal(str)         # pubkey whose membership data the association deleted
    media_library_requested = Signal()  # open the Media Library, from Media storage

    def __init__(self, api, *, pubkey: Optional[str] = None,
                 known_member: Optional[bool] = None, handle: Optional[str] = None,
                 signs_locally: bool = False, profile_address: str = "",
                 relay_listed: bool = False, member_year: Optional[int] = None,
                 storage_used: Optional[Callable[[], Optional[int]]] = None,
                 is_dark: bool = True,
                 watcher_factory: Optional[Callable] = None,
                 prices=None,
                 parent=None):
        super().__init__(_("EINUNDZWANZIG Membership"), is_dark=is_dark, parent=parent)
        self.setProperty("nightVioletMembership", True)
        self.setStyleSheet(self.styleSheet() + grouped_list.stylesheet(is_dark)
                          + f"QLabel#amount {{ color: {appearance.roles(is_dark).amount}; }}"
                          + appearance.accent_button_css('QPushButton[payment="true"]', is_dark, payment=True))
        appearance.surface(self, is_dark, membership=True)
        self._watcher_factory = watcher_factory or (
            lambda api_, year, parent_: e21.PaymentWatcher(api_, year, parent=parent_))
        # Today's Bitcoin price, to show the fee in sats, CHF and EUR. None
        # shows the association's amount alone.
        self._price_lookup = prices
        if isinstance(prices, QObject):
            prices.setParent(self)
        self._prices = None
        self._prices_asked = False
        self._watcher = None
        self._api = None
        self._relay_publishes_list = False
        # Bytes the account keeps on the members' media server, as the
        # Media Library last listed them in full; None when that is not
        # known. Read whenever the member page shows.
        self._storage_used = storage_used or (lambda: None)

        self._build_overview()
        self._build_working()
        self._build_apply()
        self._build_pay()
        self._build_member()

        self.set_identity(api, pubkey=pubkey, known_member=known_member, handle=handle,
                          signs_locally=signs_locally, profile_address=profile_address,
                          relay_listed=relay_listed, member_year=member_year)

    # ------------------------------------------------------------------ #
    # Identity                                                            #
    # ------------------------------------------------------------------ #

    @property
    def pubkey(self) -> Optional[str]:
        """The identity the window speaks for, or None before a signer."""
        return self._pubkey

    def apply_theme(self, is_dark: bool) -> None:
        super().apply_theme(is_dark)
        self.setProperty("nightVioletMembership", True)
        self.setStyleSheet(self.styleSheet() + grouped_list.stylesheet(is_dark)
                          + f"QLabel#amount {{ color: {appearance.roles(is_dark).amount}; }}"
                          + appearance.accent_button_css('QPushButton[payment="true"]', is_dark, payment=True))
        appearance.surface(self, is_dark, membership=True)

    def set_identity(self, api, *, pubkey: Optional[str], known_member: Optional[bool] = None,
                     handle: Optional[str] = None, signs_locally: bool = False,
                     profile_address: str = "", relay_listed: bool = False,
                     member_year: Optional[int] = None) -> None:
        """Start over for this identity (after connecting a signer, or a
        profile switch). Anything in flight for the previous one is dropped,
        and so is the client that asked it.

        ``relay_listed``: the account's relay list, as last known, has the
        members' relay. ``member_year``: the year of the roster that lists
        the account, when one does."""
        self._stop_watching()
        previous = self._api
        if previous is not None and previous is not api:
            self._release(previous)
        if isinstance(api, QObject):
            api.setParent(self)
        self._api = api
        self._pubkey = pubkey
        self._handle = handle
        self._signs_locally = signs_locally
        # The Nostr address the profile shows now, as last read.
        self._profile_address = (profile_address or "").strip()
        self._profile_offered = False
        self._relay_listed = relay_listed
        self._member_year = member_year
        self._config = None
        self._status = None
        self._invoice = None
        self._name_only = False
        self._just_joined = False
        self._relay_publishes_list = False
        self._service_retrying = False
        for row in (self._relay_row, self._address_row):
            row.set_note("")
        # Joining in the app needs the membership service (which holds the
        # association's key). Until it has answered, Continue waits; without
        # it, the association's website is the way to join.
        self._service = "checking" if api is not None and api.configured else "unavailable"
        if pubkey and known_member:
            self._show_member()
        else:
            self._show_overview()
        if self._service == "checking":
            api.check_service(lambda ok, asked=api: self._on_service_checked(asked, ok))

    def _release(self, api) -> None:
        """End a client: nothing it still has in flight is answered, and a
        client this window owns is deleted with its connections."""
        api.cancel()
        if isinstance(api, QObject) and api.parent() is self:
            api.deleteLater()

    def _joining_available(self) -> bool:
        return self._service == "available"

    def _on_service_checked(self, api, ok: bool) -> None:
        if api is None or api is not self._api:
            return      # an identity no longer shown, or a window already closed
        self._service = "available" if ok else "unavailable"
        self._service_retrying = False
        if ok:
            api.config(self._on_config, lambda _error: None)
        if self.page == OVERVIEW:
            self._show_overview()
        elif self.page == MEMBER:
            self._show_member()

    # ------------------------------------------------------------------ #
    # Overview                                                            #
    # ------------------------------------------------------------------ #

    def _build_overview(self) -> None:
        body, col = page()
        col.addWidget(text_label(_("Become an EINUNDZWANZIG Member"), "title"))
        col.addWidget(text_label(
            _("EINUNDZWANZIG brings Bitcoiners together across the German-speaking "
              "world, with local meetups, education and exchange. Membership supports "
              "the community and adds services to your Nostr account.")))
        col.addSpacing(4)
        # Everything the association lists for its members, in its order.
        for service in MEMBER_SERVICES:
            col.addLayout(self._item(_service_title(service), _service_detail(service)))
        col.addWidget(link_button(_("Details on the EINUNDZWANZIG Website"),
                                  lambda: self.link_activated.emit(BENEFITS_URL)),
                      0, Qt.AlignLeft)
        col.addSpacing(4)
        self._fee_label = text_label("", "item_title")
        self._fee_label.hide()
        col.addWidget(self._fee_label)
        self._overview_note = text_label("", "muted")
        col.addWidget(self._overview_note)
        col.addStretch(1)
        self.add_page(OVERVIEW, body)

    def _item(self, title: str, detail: str) -> QHBoxLayout:
        """A checked row of the overview: what membership includes."""
        mark = text_label("✓", "check", wrap=False)
        mark.setFixedWidth(18)
        mark.setAccessibleName(_("Included"))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(text_label(title, "item_title"))
        text.addWidget(text_label(detail, "muted"))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(mark, 0, Qt.AlignTop)
        row.addLayout(text, 1)
        return row

    def _show_overview(self, note: str = "") -> None:
        self._set_overview_note(note)
        if not self._pubkey:
            buttons = [("not_now", _("Not Now"), NORMAL, self.reject),
                       ("connect", _("Connect Signer App\u2026"), DEFAULT,
                        self._ask_to_connect)]
        elif self._service == "unavailable":
            buttons = [("not_now", _("Not Now"), NORMAL, self.reject),
                       ("website", _("Join on the Website"), DEFAULT, self._join_on_website)]
        else:
            buttons = [("not_now", _("Not Now"), NORMAL, self.reject),
                       ("continue", _("Continue"), DEFAULT, lambda: self._load_status())]
        self.show_page(OVERVIEW, buttons)
        if "continue" in self.buttons:
            self.buttons["continue"].setEnabled(self._joining_available())

    def _set_overview_note(self, note: str) -> None:
        if not self._pubkey:
            note = note or _("Membership belongs to a Nostr account. Connect your "
                             "signer app, or create an account, to join.")
        elif self._service == "unavailable":
            note = note or _("Joining in the app isn’t available right now. "
                             "You can join on the EINUNDZWANZIG website.")
        elif self._service == "checking":
            note = note or _("Checking whether you can join from here…")
        self._overview_note.setText(note or _DEFAULT_NOTE)

    def _on_config(self, config) -> None:
        self._config = config
        self._show_fee()
        self._ask_for_prices()

    def _show_fee(self) -> None:
        config = self._config
        if config is None:
            return
        first, rest = e21.format_fee(config.fee, config.currency, self._prices)
        shown = f"{first} ({rest})" if rest else first
        self._fee_label.setText(_("Annual fee for {year}: {fee}").format(year=config.year,
                                                                         fee=shown))
        self._fee_label.show()

    def _ask_for_prices(self) -> None:
        """Once per window: the price only converts the fee for display."""
        if self._price_lookup is None or self._prices_asked:
            return
        self._prices_asked = True
        self._price_lookup.lookup(self._on_prices)

    def _on_prices(self, prices) -> None:
        if prices is None or self._api is None:
            return      # no price, or a window already closed
        self._prices = prices
        self._show_fee()
        if self._invoice is not None and self.page == PAY:
            self._show_invoice_amount(self._invoice)

    def _ask_to_connect(self) -> None:
        self.connect_requested.emit()

    def _join_on_website(self) -> None:
        self.link_activated.emit(JOIN_URL)
        self.accept()

    # ------------------------------------------------------------------ #
    # Working                                                             #
    # ------------------------------------------------------------------ #

    def _build_working(self) -> None:
        body, col = page()
        col.addStretch(1)
        self._working_title = text_label("", "item_title")
        self._working_title.setAlignment(Qt.AlignCenter)
        self._working_detail = text_label("", "muted")
        self._working_detail.setAlignment(Qt.AlignCenter)
        col.addWidget(self._working_title)
        col.addWidget(self._working_detail)
        col.addWidget(busy_bar(), 0, Qt.AlignHCenter)
        col.addStretch(2)
        self.add_page(WORKING, body)

    def _signer_may_ask(self) -> str:
        """The sentence that warns of a signer prompt, or nothing for an
        account whose key is kept on this computer (it never prompts)."""
        return "" if self._signs_locally else " " + _SIGNER_MAY_ASK

    def _working(self, detail: str, back: Callable[[], None],
                 title: Optional[str] = None) -> None:
        """Show the wait. Cancel drops the request and goes back. The title
        names the signer app only when there is one to look at."""
        if title is None:
            title = _ONE_MOMENT if self._signs_locally else _ASK_SIGNER
        self._working_title.setText(title)
        self._working_detail.setText(detail)

        def cancel():
            self._api.cancel()
            back()

        self.show_page(WORKING, [("cancel", _("Cancel"), NORMAL, cancel)])

    def _fail(self, error, back: Callable[[], None]) -> None:
        back()
        title, message = e21.humanize(error)
        inform(self, title=title, message=message, is_dark=self._is_dark, nostr=True)

    # ------------------------------------------------------------------ #
    # Status                                                              #
    # ------------------------------------------------------------------ #

    def _load_status(self, back: Optional[Callable[[], None]] = None) -> None:
        """Ask where the membership stands. Cancel and failure go ``back``:
        to the start by default, to the member page from its details link."""
        back = back or self._show_overview
        self._working(_("MyEditor is asking EINUNDZWANZIG where your membership stands."),
                      back=back)
        self._api.me(self._on_status, lambda error: self._fail(error, back))

    def _on_status(self, status) -> None:
        self._status = status
        if status.is_member:
            self.member_confirmed.emit(self._pubkey or status.pubkey)
            self._show_member(note=self._paid_note())
        elif status.needs_payment:
            self._create_invoice()
        else:
            self._name_only = False
            self._show_apply()

    # ------------------------------------------------------------------ #
    # Apply                                                               #
    # ------------------------------------------------------------------ #

    def _build_apply(self) -> None:
        body, col = page()
        self._apply_title = text_label(_("Your Application"), "title")
        col.addWidget(self._apply_title)

        self._statutes_box = QWidget()
        statutes = QVBoxLayout(self._statutes_box)
        statutes.setContentsMargins(0, 0, 0, 0)
        statutes.setSpacing(6)
        statutes.addWidget(text_label(
            _("Joining means agreeing to the association’s statutes. "
              "Please read them first."), ""))
        row = QHBoxLayout()
        self._statutes_label = text_label("", "muted")
        self._statutes_button = QPushButton(_("Open Statutes"))
        self._statutes_button.setAutoDefault(False)
        self._statutes_button.clicked.connect(self._open_statutes)
        row.addWidget(self._statutes_label, 1)
        row.addWidget(self._statutes_button)
        statutes.addLayout(row)
        self._consent = QCheckBox(_("I have read the statutes and agree to them."))
        self._consent.toggled.connect(self._update_send_button)
        statutes.addWidget(self._consent)
        col.addWidget(self._statutes_box)
        col.addSpacing(6)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self._handle_edit = QLineEdit()
        self._handle_edit.setPlaceholderText("satoshi")
        self._handle_edit.textEdited.connect(self._lowercase_handle)
        self._handle_edit.textChanged.connect(self._check_handle)
        suffix = text_label(f"@{NIP05_DOMAIN}", "muted", wrap=False)
        handle_row = QHBoxLayout()
        handle_row.setSpacing(4)
        handle_row.addWidget(self._handle_edit, 1)
        handle_row.addWidget(suffix)
        self._handle_help = text_label(_("Optional. Lowercase letters, digits, - and _."),
                                       "help")
        self._handle_error = text_label("", "error")
        handle_field = self._field(handle_row, self._handle_help, self._handle_error)
        form.addRow(_("Nostr address:"), handle_field)
        self._name_field(form, handle_field, self._handle_edit, self._handle_help)

        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText(_("name@example.com"))
        self._email_edit.textChanged.connect(self._check_email)
        self._email_help = text_label(_("Optional. Only for membership notices, like "
                                        "renewal reminders."), "help")
        self._email_error = text_label("", "error")
        self._email_row = self._field(self._email_edit, self._email_help, self._email_error)
        form.addRow(_("Email:"), self._email_row)
        self._name_field(form, self._email_row, self._email_edit, self._email_help)

        self._message_edit = QPlainTextEdit()
        self._message_edit.setFixedHeight(70)
        self._message_edit.setTabChangesFocus(True)
        self._message_edit.textChanged.connect(self._check_message)
        self._message_help = text_label(_("Optional. Anything you’d like the association "
                                          "to know."), "help")
        self._message_error = text_label("", "error")
        self._message_row = self._field(self._message_edit, self._message_help,
                                        self._message_error)
        form.addRow(_("Message:"), self._message_row)
        self._name_field(form, self._message_row, self._message_edit, self._message_help)
        self._form = form
        col.addLayout(form)
        col.addStretch(1)
        self.add_page(APPLY, body)

    @staticmethod
    def _field(field, help_label: QLabel, error_label: QLabel) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        if isinstance(field, QWidget):
            col.addWidget(field)
        else:
            col.addLayout(field)
        col.addWidget(help_label)
        error_label.hide()
        col.addWidget(error_label)
        return box

    @staticmethod
    def _name_field(form: QFormLayout, row: QWidget, field: QWidget, help_label: QLabel) -> None:
        """Let a screen reader announce ``field`` by its row's label and read
        its help. The field sits in a box with its help and error lines, so
        the form's label points at the box, and Qt names a field only after
        a label beside it; a click on the label now reaches the field too."""
        label = form.labelForField(row)
        if isinstance(label, QLabel):
            label.setBuddy(field)
            field.setAccessibleName(label.text().rstrip(":").strip())
        field.setAccessibleDescription(help_label.text())

    def _show_apply(self, *, name_only: bool = False) -> None:
        self._name_only = name_only
        if self._config is None and not name_only:
            self._working(_("Loading the statutes…"), back=self._show_overview,
                          title=_ONE_MOMENT)
            self._api.config(lambda c: (self._on_config(c), self._show_apply()),
                             lambda error: self._fail(error, self._show_overview))
            return
        self._apply_title.setText(_("Choose a Nostr Address") if name_only
                                  else _("Your Application"))
        self._statutes_box.setVisible(not name_only)
        self._email_row.setVisible(not name_only)
        self._message_row.setVisible(not name_only)
        for widget in (self._email_row, self._message_row):
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(not name_only)
        self._handle_help.setText(
            _("Lowercase letters, digits, - and _. It can take a few minutes to "
              "appear everywhere.") if name_only else
            _("Optional. Lowercase letters, digits, - and _."))
        if self._config is not None:
            version = self._config.statutes_version
            adopted = format_date(self._config.statutes_adopted_at)
            self._statutes_label.setText(_("Version {version}, adopted {date}.").format(
                version=version, date=adopted))
        back = self._show_member if name_only else self._show_overview
        label = _("Save Address") if name_only else _("Send Application")
        self.show_page(APPLY, [("back", _("Go Back"), LEADING, back),
                           ("send", label, DEFAULT, self._send_application)])
        self._update_send_button()
        (self._handle_edit if name_only else self._consent).setFocus()

    def _open_statutes(self) -> None:
        if self._config is not None:
            self.link_activated.emit(self._config.statutes_url)

    def _lowercase_handle(self, text: str) -> None:
        if text != text.lower():
            position = self._handle_edit.cursorPosition()
            self._handle_edit.setText(text.lower())
            self._handle_edit.setCursorPosition(position)

    def _set_error(self, label: QLabel, message: Optional[str]) -> None:
        label.setText(message or "")
        label.setVisible(bool(message))

    def _check_handle(self) -> None:
        text = self._handle_edit.text().strip()
        self._set_error(self._handle_error, e21.nip05_handle_problem(text) if text else None)
        self._update_send_button()

    def _check_email(self) -> None:
        text = self._email_edit.text().strip()
        self._set_error(self._email_error, e21.email_problem(text) if text else None)
        self._update_send_button()

    def _check_message(self) -> None:
        text = self._message_edit.toPlainText()
        self._set_error(self._message_error,
                        e21.application_text_problem(text) if text.strip() else None)
        self._update_send_button()

    def _update_send_button(self, *_args) -> None:
        button = self.buttons.get("send")
        if button is None or self._page != APPLY:
            return
        problems = any(label.isVisibleTo(self._pages[APPLY]) for label in
                       (self._handle_error, self._email_error, self._message_error))
        if self._name_only:
            ready = bool(self._handle_edit.text().strip())
        else:
            ready = self._consent.isChecked()
        button.setEnabled(ready and not problems)

    def _send_application(self) -> None:
        handle = self._handle_edit.text().strip()
        kwargs = {}
        if self._name_only:
            kwargs.update(statutes_accepted=e21.UNSET, nip05_handle=handle)
        else:
            email = self._email_edit.text().strip()
            message = self._message_edit.toPlainText().strip()
            kwargs["statutes_accepted"] = True
            if handle:
                kwargs["nip05_handle"] = handle
            if email:
                kwargs.update(email=email, no_email=False)
            else:
                kwargs["no_email"] = True
            if message:
                kwargs["application_text"] = message
        detail = (_("MyEditor is saving your Nostr address.") if self._name_only
                  else _("MyEditor is sending your application to EINUNDZWANZIG."))
        name_only = self._name_only
        self._working(detail, back=lambda: self._show_apply(name_only=name_only))
        self._api.apply(lambda status: self._on_applied(status, handle, name_only),
                        lambda error: self._on_apply_failed(error, name_only), **kwargs)

    def _on_applied(self, status, handle: str, name_only: bool) -> None:
        self._status = status
        if handle:
            self._handle = handle
            if self._pubkey:
                # So the name shows next time too, before the roster lists it.
                self.address_saved.emit(self._pubkey, handle)
        if name_only:
            self._address_row.set_note(_("Your Nostr address is saved. It can take a few "
                                         "minutes to appear everywhere."))
            self._show_member()
            self._profile_offered = False
            self._offer_address_on_profile()
        else:
            self._on_status(status)

    def _on_apply_failed(self, error, name_only: bool) -> None:
        self._show_apply(name_only=name_only)
        if error.code == e21.ErrorCode.VALIDATION and error.field_errors:
            shown = False
            for label, message in (
                (self._handle_error, e21.handle_field_message(error)),
                (self._email_error, e21.field_message(error, "email")),
                (self._message_error, e21.field_message(error, "application_text")),
            ):
                if message:
                    self._set_error(label, message)
                    shown = True
            self._update_send_button()
            if shown:
                return
        title, message = e21.humanize(error)
        inform(self, title=title, message=message, is_dark=self._is_dark, nostr=True)

    # ------------------------------------------------------------------ #
    # Pay                                                                 #
    # ------------------------------------------------------------------ #

    def _build_pay(self) -> None:
        body, col = page()
        self._pay_title = text_label("", "title")
        col.addWidget(self._pay_title)
        self._pay_intro = text_label("")
        col.addWidget(self._pay_intro)
        self._amount = text_label("", "amount", wrap=False)
        self._amount.setAlignment(Qt.AlignCenter)
        col.addWidget(self._amount)
        self._amount_detail = text_label("", "muted")
        self._amount_detail.setAlignment(Qt.AlignCenter)
        col.addWidget(self._amount_detail)

        self._qr = QLabel()
        self._qr.setObjectName("qr")
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.setAccessibleName(_("Lightning invoice code to scan with a wallet"))
        col.addWidget(self._qr, 0, Qt.AlignHCenter)

        # The code is the way to pay (owner decision, Q6). Copy Invoice is
        # its text twin: for a wallet on this computer, and for anyone who
        # cannot scan, such as a VoiceOver user.
        actions = QHBoxLayout()
        actions.addStretch(1)
        self._copy_button = QPushButton(_("Copy Invoice"))
        self._copy_button.setProperty("payment", True)
        self._copy_button.setAutoDefault(False)
        self._copy_button.clicked.connect(self._copy_invoice)
        actions.addWidget(self._copy_button)
        actions.addStretch(1)
        col.addLayout(actions)
        # Only when there is no invoice to show as a code: the payment page.
        self._browser_button = link_button(_("Pay in Browser"), self._pay_in_browser)
        col.addWidget(self._browser_button, 0, Qt.AlignHCenter)

        self._pay_status = text_label("", "muted")
        self._pay_status.setAlignment(Qt.AlignCenter)
        self._pay_busy = busy_bar()
        col.addWidget(self._pay_status)
        col.addWidget(self._pay_busy, 0, Qt.AlignHCenter)
        col.addStretch(1)
        self.add_page(PAY, body)

    def _fee_year(self) -> int:
        if self._status is not None:
            return self._status.current_year.year
        if self._config is not None:
            return self._config.year
        return datetime.date.today().year

    def _create_invoice(self) -> None:
        self._stop_watching()
        self._working(_("MyEditor is asking EINUNDZWANZIG for your invoice."),
                      back=self._show_overview)
        self._api.create_invoice(self._fee_year(), self._on_invoice,
                                 lambda error: self._fail(error, self._show_overview))

    def _on_invoice(self, invoice) -> None:
        self._invoice = invoice
        if invoice.payment.paid:
            self._on_paid(invoice)
            return
        renewing = self._status is not None and self._status.membership_status == e21.STATUS_LAPSED
        year = invoice.payment.year
        title = (_("Renew Your Membership for {year}") if renewing
                 else _("Pay Your {year} Membership Fee"))
        self._pay_title.setText(title.format(year=year))
        self._show_invoice_amount(invoice)
        self._ask_for_prices()

        has_invoice = bool(invoice.bolt11)
        if has_invoice:
            self._qr.setPixmap(make_qr_pixmap(invoice.bolt11.upper(), size=_QR_SIZE,
                                              dark="#000000", light="#FFFFFF"))
        self._qr.setVisible(has_invoice)
        self._copy_button.setVisible(has_invoice)
        self._browser_button.setVisible(not has_invoice and bool(invoice.checkout_url))
        self._pay_intro.setText(
            _("Scan the code with any Lightning wallet. When you’ve paid, click "
              "I’ve Paid and MyEditor confirms it with EINUNDZWANZIG.") if has_invoice else
            _("Pay on the EINUNDZWANZIG payment page. When you’ve paid, click "
              "I’ve Paid and MyEditor confirms it."))
        self._pay_intro.setVisible(True)
        self._set_pay_status("")
        self._show_pay_buttons("paid", _("I’ve Paid"))

    def _show_invoice_amount(self, invoice) -> None:
        """The exact sats the invoice asks for, large; under it the fee in
        francs and euros (the association's own amount exact, the other
        converted)."""
        amounts = e21.fee_amounts(invoice.payment.amount, invoice.payment.currency,
                                  self._prices)
        sats = invoice.amount_sats
        if sats is not None:
            first = e21.format_one(e21.SATS, sats)
            rest = [entry for entry in amounts if entry[0] != e21.SATS]
        else:
            first = e21.format_one(*amounts[0])
            rest = amounts[1:]
        self._amount.setText(first)
        self._amount_detail.setText(", ".join(e21.format_one(*entry) for entry in rest))
        self._amount_detail.setVisible(bool(self._amount_detail.text()))

    def _show_pay_buttons(self, key: str, label: str, enabled: bool = True) -> None:
        handlers = {"paid": self._start_watching, "again": self._start_watching,
                    "new_invoice": self._create_invoice}
        self.show_page(PAY, [("close", _("Close"), NORMAL, self.reject),
                         (key, label, DEFAULT, handlers[key])])
        self.buttons[key].setProperty("payment", True)
        self.buttons[key].style().unpolish(self.buttons[key])
        self.buttons[key].style().polish(self.buttons[key])
        self.buttons[key].setEnabled(enabled)

    def _set_pay_status(self, text: str, *, busy: bool = False) -> None:
        self._pay_status.setText(text)
        self._pay_status.setVisible(bool(text))
        self._pay_busy.setVisible(busy)

    def _copy_invoice(self) -> None:
        if self._invoice is None or not self._invoice.bolt11:
            return
        QApplication.clipboard().setText(self._invoice.bolt11)
        self._copy_button.setText(_("Copied"))
        # The button is the timer's context: closing the window before it
        # fires cancels it rather than touching a deleted button.
        button = self._copy_button
        QTimer.singleShot(1500, button, lambda: button.setText(_("Copy Invoice")))

    def _pay_in_browser(self) -> None:
        if self._invoice is not None and self._invoice.checkout_url:
            self.link_activated.emit(self._invoice.checkout_url)

    def _start_watching(self) -> None:
        self._stop_watching()
        watcher = self._watcher_factory(self._api, self._invoice.payment.year, self)
        watcher.paid.connect(self._on_paid)
        watcher.still_waiting.connect(lambda _n: self._set_pay_status(
            _("Not confirmed yet. Checking again…"), busy=True))
        watcher.expired.connect(self._on_expired)
        watcher.gave_up.connect(self._on_gave_up)
        watcher.failed.connect(self._on_watch_failed)
        self._watcher = watcher
        self._set_pay_status(_("Checking for your payment…") + self._signer_may_ask(),
                             busy=True)
        self._show_pay_buttons("paid", _("Checking…"), enabled=False)
        watcher.start(poll_now=True)

    def _stop_watching(self) -> None:
        if self._watcher is not None:
            watcher, self._watcher = self._watcher, None
            watcher.stop()
            if isinstance(watcher, QObject):
                watcher.deleteLater()

    def _on_expired(self, _invoice) -> None:
        self._stop_watching()
        self._set_pay_status(_("This invoice has expired. Create a new one to pay."))
        self._show_pay_buttons("new_invoice", _("Create New Invoice"))

    def _on_gave_up(self) -> None:
        self._stop_watching()
        self._set_pay_status(_("Your payment isn’t confirmed yet. If you paid, wait a "
                               "moment, then click Check Again."))
        self._show_pay_buttons("again", _("Check Again"))

    def _on_watch_failed(self, error) -> None:
        self._stop_watching()
        self._set_pay_status("")
        self._show_pay_buttons("paid", _("I’ve Paid"))
        title, message = e21.humanize(error)
        inform(self, title=title, message=message, is_dark=self._is_dark, nostr=True)

    def _on_paid(self, _invoice) -> None:
        self._stop_watching()
        self._just_joined = True

        def back():
            # Paid: the invoice and the ways to pay it are no longer the point.
            for widget in (self._qr, self._copy_button, self._browser_button):
                widget.setVisible(False)
            self._pay_title.setText(_("Payment Received"))
            self._pay_intro.setText("")
            self._pay_intro.setVisible(False)
            self.show_page(PAY, [("close", _("Close"), NORMAL, self.reject),
                             ("again", _("Check Again"), DEFAULT,
                              lambda: self._load_status(back=back))])
            self._set_pay_status(_("Your payment arrived. Click Check Again to finish."))

        self._working(_("Your payment arrived. MyEditor is confirming your membership."),
                      back=back)

        def confirmed(status):
            self._status = status
            if status.is_member:
                self.member_confirmed.emit(self._pubkey or status.pubkey)
                self._show_member()
                self._offer_address_on_profile()
            else:
                back()
                self._set_pay_status(_("Your payment arrived. EINUNDZWANZIG is still "
                                       "confirming it. Check again in a moment."))

        self._api.me(confirmed, lambda error: self._fail(error, back))

    # ------------------------------------------------------------------ #
    # Member                                                              #
    # ------------------------------------------------------------------ #

    def _build_member(self) -> None:
        body, col = page(spacing=0)
        self._card = MembershipCard(is_dark=self._is_dark)
        self._card.setMinimumWidth(_MEMBER_PAGE_WIDTH)
        col.addWidget(self._card)
        col.addSpacing(8)
        # One line under the card: how membership runs, a welcome, or what
        # just happened.
        self._note = text_label("", "muted")
        col.addWidget(self._note)
        # What needs the membership service, while it can't be reached.
        self._service_line = Notice(
            _("Choosing an address, receipts and data export aren’t available right now."),
            _("Try Again"), self._check_service_again, is_dark=self._is_dark)
        self._retry_link = self._service_line.link
        col.addSpacing(8)
        col.addWidget(self._service_line)
        col.addSpacing(16)

        col.addWidget(group_header(_("In MyEditor")))
        col.addSpacing(8)
        mine = ListGroup(is_dark=self._is_dark)
        self._relay_row = mine.add_row(ListRow(
            _("Members’ relay"), symbol="relay", tint=_RELAY_TINT))
        self._relay_row.set_detail(_("Readers find your notes and articles at {relay}.").format(
            relay=_host(MEMBER_RELAY)))
        # Each row starts as a member's who has set up nothing yet; the page
        # says where things stand whenever it shows (_show_member).
        self._relay_row.show_button(_("Add"))
        self._relay_row.button.clicked.connect(self._add_relay)
        self._storage_row = mine.add_row(ListRow(
            _("Media storage"), symbol="photo", tint=_STORAGE_TINT))
        self._usage_bar = usage_bar(_("Storage used"))
        self._storage_row.add_detail(self._usage_bar)
        self._storage_row.show_button(_("Open Media Library"))
        self._storage_row.button.clicked.connect(self.media_library_requested.emit)
        self._address_row = mine.add_row(ListRow(
            _("Nostr address"), symbol="at", tint=_ADDRESS_TINT))
        self._address_row.show_button(_("Choose…"))
        self._address_row.button.clicked.connect(self._on_address_button)
        col.addWidget(mine)
        col.addSpacing(16)

        # What MyEditor doesn't set up itself: the association's guide does.
        # The sentence names the services of MEMBER_SERVICES that are not
        # in_app, in words of its own.
        more = ListGroup(is_dark=self._is_dark)
        self._more_row = more.add_row(ListRow(
            _("More for members"), symbol="ellipsis", tint=_MORE_TINT))
        self._more_row.set_detail(_("Lightning watchtower, community group and Buzz (in "
                                    "testing) are set up on the association’s website."))
        # The arrow says it opens in the browser, as a link out of an app does.
        self._more_row.show_button(f"{_('Open Guide')} ↗",
                                   description=_("Opens in your browser."))
        self._more_row.button.setAccessibleName(_("Open Guide"))
        self._more_row.button.clicked.connect(lambda: self.link_activated.emit(BENEFITS_URL))
        col.addWidget(more)
        col.addStretch(1)
        # The page's other commands, under Manage (filled as the page shows).
        self._manage_menu = QMenu(self)
        self.add_page(MEMBER, body)

    def _show_member(self, note: str = "") -> None:
        self._stop_watching()
        self._card.set_member(self._member_year_shown(), self._address())
        if note:
            self._note.setText(note)
        elif self._just_joined:
            self._note.setText(_("Welcome to EINUNDZWANZIG. Thank you for supporting the "
                                 "community."))
        else:
            self._note.setText(_CALENDAR_NOTE)
        self._show_service_line()
        self._show_relay()
        self._show_storage()
        self._show_address()
        buttons = [("done", _("Done"), DEFAULT, self.accept)]
        if self._fill_manage_menu():
            buttons.insert(0, ("manage", _("Manage"), LEADING, self._manage_menu))
        self.show_page(MEMBER, buttons)
        # Done starts with the focus: nothing else on the page (an Add, a
        # link) may look like what to do next. A button laid out now is
        # shown only later, so it is shown first.
        done = self.buttons["done"]
        done.show()
        done.setFocus()

    def _member_year_shown(self) -> int:
        """The year the membership is known for: the one the association
        says is paid, else the year of the roster that lists the member,
        else this calendar year."""
        status = self._status
        if status is not None and status.current_year.paid:
            return status.current_year.year
        return self._member_year or datetime.date.today().year

    def _paid_note(self) -> str:
        """What the association's answer adds to the card: that the year
        is paid."""
        status = self._status
        if status is None or not status.current_year.paid:
            return ""
        return _("Your membership is paid for {year}.").format(year=status.current_year.year)

    def event(self, event) -> bool:
        # Back in front (from the Media Library, say): the storage as it
        # was last listed.
        if event.type() == QEvent.Type.WindowActivate and self.page == MEMBER:
            self._show_storage()
        return super().event(event)

    # -- the membership service, while it can't be reached --------------------

    def _show_service_line(self) -> None:
        """Say what needs the membership service while it can't be reached,
        with Try Again (none for a build that has no service to ask)."""
        retrying = self._service == "checking" and self._service_retrying
        self._service_line.setVisible(self._service == "unavailable" or retrying)
        self._retry_link.setVisible(self._api is not None and self._api.configured)
        self._retry_link.setText(_("Checking…") if retrying else _("Try Again"))
        self._retry_link.setEnabled(not retrying)

    def _check_service_again(self) -> None:
        api = self._api
        if api is None or not api.configured or self._service == "checking":
            return
        self._service = "checking"
        self._service_retrying = True
        self._show_service_line()
        api.check_service(lambda ok, asked=api: self._on_service_checked(asked, ok))

    # -- the members' relay ------------------------------------------------------

    def _show_relay(self) -> None:
        if self._relay_listed:
            self._relay_row.show_status(_("In your relay list"))
        elif self._relay_publishes_list:
            self._relay_row.show_button(_("Create Relay List"))
        else:
            self._relay_row.show_button(_("Add"))

    def _add_relay(self) -> None:
        self._relay_row.button.setEnabled(False)
        if self._relay_publishes_list:
            self._relay_row.set_note(_("Publishing…") + self._signer_may_ask())
            self.publish_relay_list_requested.emit()
        else:
            self._relay_row.set_note(_("Adding…") + self._signer_may_ask())
            self.add_relay_requested.emit()

    def set_relay_result(self, message: str, *, done: bool = False,
                         offer_list: bool = False) -> None:
        """The outcome of add_relay_requested (or of publishing a list),
        from the window that ran it. ``done``: the relay list has the
        members' relay now, which the row says in place of the button.
        Otherwise ``message`` says why not under the row, and the button
        offers to try again; with ``offer_list`` it offers to create the
        recommended relay list, for an account that has none."""
        self._relay_listed = self._relay_listed or done
        self._relay_publishes_list = offer_list and not done
        self._relay_row.set_note("" if done else message)
        self._show_relay()

    # -- media storage -------------------------------------------------------------

    def _show_storage(self) -> None:
        """The allowance, and how much of it is used once the Media Library
        has listed the members' server in full (a number from a listing
        that failed would be a guess)."""
        used = self._storage_used()
        bar = self._usage_bar
        if used is None:
            detail = _("{total} for images and videos · up to {per_file} per file").format(
                total=PER_USER_LABEL, per_file=MAX_FILE_LABEL)
        else:
            detail = _("{used} of {total} used · up to {per_file} per file").format(
                used=format_storage(used), total=PER_USER_LABEL, per_file=MAX_FILE_LABEL)
            share = min(1.0, used / PER_USER_BYTES)
            bar.setValue(round(share * bar.maximum()))
            # Almost full is said in the attention color too, never in it alone.
            bar.setProperty("tone", "full" if share >= _ALMOST_FULL else "")
            bar.style().unpolish(bar)
            bar.style().polish(bar)
        self._storage_row.set_detail(detail)
        bar.setVisible(used is not None)

    # -- the Nostr address -----------------------------------------------------------

    def _address(self) -> str:
        return nip05_address(self._handle) if self._handle else ""

    def _profile_shows_address(self) -> bool:
        address = self._address()
        return bool(address) and self._profile_address.lower() == address.lower()

    def _show_address(self) -> None:
        """The address, and whether the profile shows it: said when it
        does, offered when it doesn't; without one, Choose… while the
        membership service can take one."""
        row = self._address_row
        address = self._address()
        row.set_detail(address or _("Not chosen yet"), selectable=bool(address))
        if address and self._profile_shows_address():
            row.show_status(_("On your profile"))
        elif address:
            row.show_button(_("Add to Profile"))
        elif self._joining_available():
            row.show_button(_("Choose…"))
        else:
            row.clear_accessory()

    def _on_address_button(self) -> None:
        if self._address():
            self._show_address_on_profile()
        else:
            self._show_apply(name_only=True)

    def _offer_address_on_profile(self) -> None:
        """Ask once, right after an address was chosen, whether the profile
        should show it. Saying Not Now leaves Add to Profile on the page."""
        if (self._profile_offered or not self._pubkey or not self._address()
                or self._profile_shows_address()):
            return
        self._profile_offered = True
        address = self._address()
        message = _("People who look at your profile then see {address} instead of a "
                    "long key. It shows as confirmed once EINUNDZWANZIG has approved "
                    "the name.").format(address=address)
        if self._profile_address:
            message += " " + _("It replaces {address}.").format(address=self._profile_address)
        message += " " + _("You can also do this later, here in EINUNDZWANZIG Membership.")
        choice = ask(self, title=_("Show Your Nostr Address on Your Profile?"),
                     message=message,
                     buttons=(Button(_("Not Now"), False, CANCEL),
                              Button(_("Show on Profile"), True, ALERT_DEFAULT)),
                     is_dark=self._is_dark, nostr=True)
        if choice is True:
            self._show_address_on_profile()

    def _show_address_on_profile(self) -> None:
        address = self._address()
        if not address:
            return
        self._address_row.button.setEnabled(False)
        self._address_row.set_note(_("Updating your profile…") + self._signer_may_ask())
        self.profile_address_requested.emit(address)

    def set_profile_address_result(self, message: str, *, done: bool = False) -> None:
        """The outcome of profile_address_requested, from the window that ran
        it. ``done`` means the profile shows the address now, which the row
        says; otherwise ``message`` says why not, and Add to Profile is
        offered again."""
        if done:
            self._profile_address = self._address()
        self._address_row.set_note("" if done else message)
        self._show_address()

    def _copy_address(self) -> None:
        address = self._address()
        if address:
            QApplication.clipboard().setText(address)

    # -- Manage ------------------------------------------------------------------------

    def _fill_manage_menu(self) -> bool:
        """Manage's commands that apply now, the destructive one last and
        apart, as a Mac menu keeps it; False when none does."""
        menu = self._manage_menu
        menu.clear()
        status = self._status
        service = self._joining_available()
        if status is not None and status.current_year.receipt_url:
            menu.addAction(_("Receipt for {year}").format(year=status.current_year.year),
                           self._open_receipt)
        if service and status is None:
            # Details cost a signature, so they come on request.
            menu.addAction(_("Membership Details"),
                           lambda: self._load_status(back=self._show_member))
        if service and self._handle:
            menu.addAction(_("Change Nostr Address…"), lambda: self._show_apply(name_only=True))
        if self._handle:
            menu.addAction(_("Copy Nostr Address"), self._copy_address)
        if service:
            if not menu.isEmpty():
                menu.addSeparator()
            menu.addAction(_("Export My Data…"), self._export)
            menu.addAction(_("Delete My Data…"), self._erase)
        return not menu.isEmpty()

    def _open_receipt(self) -> None:
        if self._status is not None and self._status.current_year.receipt_url:
            self.link_activated.emit(self._status.current_year.receipt_url)

    def _export(self) -> None:
        self._working(_("MyEditor is asking EINUNDZWANZIG for the data it keeps about you."),
                      back=self._show_member)
        self._api.export_data(self._on_exported,
                              lambda error: self._fail(error, self._show_member))

    def _on_exported(self, export) -> None:
        self._show_member()
        path, _filter = QFileDialog.getSaveFileName(
            self, _("Export Membership Data"), "einundzwanzig-membership.json",
            "JSON (*.json)")
        if not path:
            return
        try:
            save_text_document(path, json.dumps(export.document, ensure_ascii=False,
                                                indent=2))
        except OSError as exc:
            inform(self, title=_("Couldn’t save your data"), message=str(exc),
                   is_dark=self._is_dark, nostr=True)
            return
        self._show_member(note=_("Your membership data is saved."))

    def _erase(self) -> None:
        if not confirm_destructive(
                self, title=_("Delete your membership data?"),
                message=_("EINUNDZWANZIG deletes the personal details of your "
                          "membership. Fees you paid stay on record for bookkeeping. "
                          "This can’t be undone."),
                action=_("Delete Data"), caution=True, is_dark=self._is_dark, nostr=True):
            return
        self._working(_("MyEditor is asking EINUNDZWANZIG to delete your data."),
                      back=self._show_member)

        def erased(_result):
            self._status = None
            self._handle = None
            self._just_joined = False
            if self._pubkey:
                # What the app knew from the association no longer holds.
                self.data_erased.emit(self._pubkey)
            self._show_overview(note=_("Your membership data was deleted."))

        self._api.erase(erased, lambda error: self._fail(error, self._show_member))

    # ------------------------------------------------------------------ #
    # Plumbing                                                            #
    # ------------------------------------------------------------------ #

    def done(self, result: int) -> None:
        # Closing the window ends the wait; nothing keeps prompting the
        # signer for a window nobody is looking at, and an answer that
        # arrives later finds no client to belong to and is dropped.
        self._stop_watching()
        api, self._api = self._api, None
        if api is not None:
            self._release(api)
        super().done(result)
