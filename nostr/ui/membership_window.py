# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""EINUNDZWANZIG Membership: join the association, pay the fee, see what it unlocks.

One window with a page for each stage of joining, in the order it happens:

    overview   what membership is, what it costs, and Continue
    apply      the statutes, consent, and the optional details
    pay        the invoice as a QR code, Copy Invoice, Open in Wallet
    member     what is active now, and the Nostr address
    working    the short wait while the signer and the association answer

The window follows Apple's Human Interface Guidelines the way the rest of
the app does: one default button at the trailing edge with Cancel-type
buttons beside it, Go Back at the leading edge, field problems shown under
the field they belong to, progress shown where the work happens, and plain
words throughout (no protocol names, status codes or key formats).

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

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from alerts import confirm_destructive, inform
from nostr import einundzwanzig_api as e21
from nostr.einundzwanzig import (
    JOIN_URL, MAX_FILE_LABEL, MEMBER_BLOSSOM, MEMBER_RELAY, NIP05_DOMAIN, PER_USER_LABEL,
    nip05_address,
)
from nostr.qr import make_qr_pixmap
from nostr.ui.assistant import (
    DEFAULT, LEADING, NORMAL, AssistantWindow, busy_bar, link_button, page, text_label,
)

# Pages.
OVERVIEW = "overview"
WORKING = "working"
APPLY = "apply"
PAY = "pay"
MEMBER = "member"

_QR_SIZE = 216
_ASK_SIGNER = "Approve the request in your signer app."
_ONE_MOMENT = "One moment."
_SIGNER_MAY_ASK = " Your signer app may ask you to approve."
_DEFAULT_NOTE = ("Membership belongs to your Nostr identity and runs for a calendar "
                 "year. You renew it yourself; nothing is charged automatically.")


def format_amount(amount: int, currency: str) -> str:
    """``21,000 sats`` or ``21 CHF``: the fee the way the association states it."""
    unit = (currency or "").strip().upper()
    if unit in ("SAT", "SATS"):
        return f"{amount:,} sats"
    return f"{amount:,} {unit}".strip()


def format_date(iso: str) -> str:
    """``20 April 2024`` from ``2024-04-20``; the input unchanged if it is not a date."""
    try:
        day = datetime.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return f"{day.day} {day.strftime('%B')} {day.year}"


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def open_lightning_invoice(bolt11: str) -> bool:
    """Hand an invoice to whatever wallet app handles Lightning links.

    Only a well-formed invoice is handed over. False when nothing on this
    computer opened it.
    """
    if not bolt11 or e21.bolt11_amount_sats(bolt11) is None:
        return False
    return QDesktopServices.openUrl(QUrl(f"lightning:{bolt11}"))


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
    data_erased = Signal(str)         # pubkey whose membership data the association deleted

    def __init__(self, api, *, pubkey: Optional[str] = None,
                 known_member: Optional[bool] = None, handle: Optional[str] = None,
                 signs_locally: bool = False,
                 is_dark: bool = True,
                 watcher_factory: Optional[Callable] = None,
                 open_lightning: Callable[[str], bool] = open_lightning_invoice,
                 parent=None):
        super().__init__("EINUNDZWANZIG Membership", is_dark=is_dark, parent=parent)
        self._watcher_factory = watcher_factory or (
            lambda api_, year, parent_: e21.PaymentWatcher(api_, year, parent=parent_))
        self._open_lightning = open_lightning
        self._watcher = None
        self._api = None
        self._relay_publishes_list = False

        self._build_overview()
        self._build_working()
        self._build_apply()
        self._build_pay()
        self._build_member()

        self.set_identity(api, pubkey=pubkey, known_member=known_member, handle=handle,
                          signs_locally=signs_locally)

    # ------------------------------------------------------------------ #
    # Identity                                                            #
    # ------------------------------------------------------------------ #

    @property
    def pubkey(self) -> Optional[str]:
        """The identity the window speaks for, or None before a signer."""
        return self._pubkey

    def set_identity(self, api, *, pubkey: Optional[str], known_member: Optional[bool] = None,
                     handle: Optional[str] = None, signs_locally: bool = False) -> None:
        """Start over for this identity (after connecting a signer, or a
        profile switch). Anything in flight for the previous one is dropped,
        and so is the client that asked it."""
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
        self._config = None
        self._status = None
        self._invoice = None
        self._name_only = False
        self._just_joined = False
        self._relay_publishes_list = False
        self._relay_button.setText("Add to My Relay List")
        self._relay_button.setEnabled(True)
        self._relay_button.setVisible(True)
        self._relay_result.setText("")
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
        col.addWidget(text_label("Become an EINUNDZWANZIG Member", "title"))
        col.addWidget(text_label(
            "EINUNDZWANZIG brings Bitcoiners together across the German-speaking "
            "world, with local meetups, education and exchange. Membership supports "
            "the community and adds services to your Nostr identity."))
        col.addSpacing(4)
        for title, detail in (
            ("Members’ relay",
             "A dependable relay that carries your notes and articles to readers."),
            ("Nostr address",
             f"An address like you@{NIP05_DOMAIN} that people can recognize and share, "
             "instead of a long key."),
            ("Media storage",
             f"{PER_USER_LABEL} for images and videos, up to {MAX_FILE_LABEL} per file."),
        ):
            col.addLayout(self._item(title, detail)[0])
        col.addSpacing(4)
        self._fee_label = text_label("", "item_title")
        self._fee_label.hide()
        col.addWidget(self._fee_label)
        self._overview_note = text_label("", "muted")
        col.addWidget(self._overview_note)
        col.addStretch(1)
        self.add_page(OVERVIEW, body)

    def _item(self, title: str, detail: str, extra: Optional[QWidget] = None):
        """A checked row: title, detail and optional controls.
        Returns ``(layout, detail_label)``."""
        mark = text_label("✓", "check", wrap=False)
        mark.setFixedWidth(18)
        mark.setAccessibleName("Included")
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(text_label(title, "item_title"))
        detail_label = text_label(detail, "muted")
        text.addWidget(detail_label)
        if extra is not None:
            text.addWidget(extra, 0, Qt.AlignLeft)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(mark, 0, Qt.AlignTop)
        row.addLayout(text, 1)
        return row, detail_label

    def _show_overview(self, note: str = "") -> None:
        self._set_overview_note(note)
        if not self._pubkey:
            buttons = [("not_now", "Not Now", NORMAL, self.reject),
                       ("connect", "Connect Signer…", DEFAULT, self._ask_to_connect)]
        elif self._service == "unavailable":
            buttons = [("not_now", "Not Now", NORMAL, self.reject),
                       ("website", "Join on the Website", DEFAULT, self._join_on_website)]
        else:
            buttons = [("not_now", "Not Now", NORMAL, self.reject),
                       ("continue", "Continue", DEFAULT, lambda: self._load_status())]
        self.show_page(OVERVIEW, buttons)
        if "continue" in self.buttons:
            self.buttons["continue"].setEnabled(self._joining_available())

    def _set_overview_note(self, note: str) -> None:
        if not self._pubkey:
            note = note or ("Membership belongs to a Nostr identity. Connect your "
                            "signer to join.")
        elif self._service == "unavailable":
            note = note or ("Joining in the app isn’t available right now. "
                            "You can join on the EINUNDZWANZIG website.")
        elif self._service == "checking":
            note = note or "Checking whether you can join from here…"
        self._overview_note.setText(note or _DEFAULT_NOTE)

    def _on_config(self, config) -> None:
        self._config = config
        self._fee_label.setText(
            f"Annual fee: {format_amount(config.fee, config.currency)} for {config.year}")
        self._fee_label.show()

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
        return "" if self._signs_locally else _SIGNER_MAY_ASK

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

        self.show_page(WORKING, [("cancel", "Cancel", NORMAL, cancel)])

    def _fail(self, error, back: Callable[[], None]) -> None:
        back()
        title, message = e21.humanize(error)
        inform(self, title=title, message=message, is_dark=self._is_dark)

    # ------------------------------------------------------------------ #
    # Status                                                              #
    # ------------------------------------------------------------------ #

    def _load_status(self, back: Optional[Callable[[], None]] = None) -> None:
        """Ask where the membership stands. Cancel and failure go ``back``:
        to the start by default, to the member page from its details link."""
        back = back or self._show_overview
        self._working("MyEditor is asking EINUNDZWANZIG where your membership stands.",
                      back=back)
        self._api.me(self._on_status, lambda error: self._fail(error, back))

    def _on_status(self, status) -> None:
        self._status = status
        if status.is_member:
            self.member_confirmed.emit(self._pubkey or status.pubkey)
            self._show_member()
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
        self._apply_title = text_label("Your Application", "title")
        col.addWidget(self._apply_title)

        self._statutes_box = QWidget()
        statutes = QVBoxLayout(self._statutes_box)
        statutes.setContentsMargins(0, 0, 0, 0)
        statutes.setSpacing(6)
        statutes.addWidget(text_label(
            "Joining means agreeing to the association’s statutes. "
            "Please read them first.", ""))
        row = QHBoxLayout()
        self._statutes_label = text_label("", "muted")
        self._statutes_button = QPushButton("Open Statutes")
        self._statutes_button.setAutoDefault(False)
        self._statutes_button.clicked.connect(self._open_statutes)
        row.addWidget(self._statutes_label, 1)
        row.addWidget(self._statutes_button)
        statutes.addLayout(row)
        self._consent = QCheckBox("I have read the statutes and agree to them.")
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
        self._handle_help = text_label("Optional. Lowercase letters, digits, - and _.", "help")
        self._handle_error = text_label("", "error")
        form.addRow("Nostr address:", self._field(handle_row, self._handle_help,
                                                  self._handle_error))

        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText("name@example.com")
        self._email_edit.textChanged.connect(self._check_email)
        self._email_help = text_label("Optional. Only for membership notices, like renewal "
                                  "reminders.", "help")
        self._email_error = text_label("", "error")
        self._email_row = self._field(self._email_edit, self._email_help, self._email_error)
        form.addRow("Email:", self._email_row)

        self._message_edit = QPlainTextEdit()
        self._message_edit.setFixedHeight(70)
        self._message_edit.setTabChangesFocus(True)
        self._message_edit.textChanged.connect(self._check_message)
        self._message_help = text_label("Optional. Anything you’d like the association "
                                    "to know.", "help")
        self._message_error = text_label("", "error")
        self._message_row = self._field(self._message_edit, self._message_help,
                                        self._message_error)
        form.addRow("Message:", self._message_row)
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

    def _show_apply(self, *, name_only: bool = False) -> None:
        self._name_only = name_only
        if self._config is None and not name_only:
            self._working("Loading the statutes…", back=self._show_overview,
                          title="One moment.")
            self._api.config(lambda c: (self._on_config(c), self._show_apply()),
                             lambda error: self._fail(error, self._show_overview))
            return
        self._apply_title.setText("Choose a Nostr Address" if name_only else "Your Application")
        self._statutes_box.setVisible(not name_only)
        self._email_row.setVisible(not name_only)
        self._message_row.setVisible(not name_only)
        for widget in (self._email_row, self._message_row):
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(not name_only)
        self._handle_help.setText(
            "Lowercase letters, digits, - and _. It can take a few minutes to "
            "appear everywhere." if name_only else
            "Optional. Lowercase letters, digits, - and _.")
        if self._config is not None:
            version = self._config.statutes_version
            adopted = format_date(self._config.statutes_adopted_at)
            self._statutes_label.setText(f"Version {version}, adopted {adopted}.")
        back = self._show_member if name_only else self._show_overview
        label = "Save Address" if name_only else "Send Application"
        self.show_page(APPLY, [("back", "Go Back", LEADING, back),
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
        detail = ("MyEditor is saving your Nostr address." if self._name_only
                  else "MyEditor is sending your application to EINUNDZWANZIG.")
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
            self._show_member(note="Your Nostr address is saved. It can take a few "
                                   "minutes to appear everywhere.")
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
        inform(self, title=title, message=message, is_dark=self._is_dark)

    # ------------------------------------------------------------------ #
    # Pay                                                                 #
    # ------------------------------------------------------------------ #

    def _build_pay(self) -> None:
        body, col = page()
        self._pay_title = text_label("", "title")
        col.addWidget(self._pay_title)
        self._pay_intro = text_label("Pay with any Lightning wallet. When you’ve paid, "
                                 "click I’ve Paid and MyEditor confirms it with "
                                 "EINUNDZWANZIG.")
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
        self._qr.setAccessibleName("Lightning invoice code to scan with a wallet")
        col.addWidget(self._qr, 0, Qt.AlignHCenter)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._copy_button = QPushButton("Copy Invoice")
        self._copy_button.setAutoDefault(False)
        self._copy_button.clicked.connect(self._copy_invoice)
        self._wallet_button = QPushButton("Open in Wallet")
        self._wallet_button.setAutoDefault(False)
        self._wallet_button.clicked.connect(self._open_wallet)
        actions.addWidget(self._copy_button)
        actions.addWidget(self._wallet_button)
        actions.addStretch(1)
        col.addLayout(actions)
        self._browser_button = link_button("Pay in Browser Instead", self._pay_in_browser)
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
        self._working("MyEditor is asking EINUNDZWANZIG for your invoice.",
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
        self._pay_title.setText(f"Renew Your Membership for {year}" if renewing
                                else f"Pay Your {year} Membership Fee")
        sats = invoice.amount_sats
        fee = format_amount(invoice.payment.amount, invoice.payment.currency)
        if sats is not None:
            self._amount.setText(format_amount(sats, "sats"))
            self._amount_detail.setText("" if fee.endswith("sats") else fee)
        else:
            self._amount.setText(fee)
            self._amount_detail.setText("")
        self._amount_detail.setVisible(bool(self._amount_detail.text()))

        has_invoice = bool(invoice.bolt11)
        if has_invoice:
            self._qr.setPixmap(make_qr_pixmap(invoice.bolt11.upper(), size=_QR_SIZE,
                                              dark="#000000", light="#FFFFFF"))
        self._qr.setVisible(has_invoice)
        self._copy_button.setVisible(has_invoice)
        self._wallet_button.setVisible(has_invoice)
        self._browser_button.setVisible(bool(invoice.checkout_url))
        self._browser_button.setText("Pay in Browser Instead" if has_invoice
                                     else "Pay in Browser")
        self._pay_intro.setText(
            "Pay with any Lightning wallet. When you’ve paid, click I’ve Paid "
            "and MyEditor confirms it with EINUNDZWANZIG." if has_invoice else
            "Pay on the EINUNDZWANZIG payment page. When you’ve paid, click "
            "I’ve Paid and MyEditor confirms it.")
        self._pay_intro.setVisible(True)
        self._set_pay_status("")
        self._show_pay_buttons("paid", "I’ve Paid")

    def _show_pay_buttons(self, key: str, label: str, enabled: bool = True) -> None:
        handlers = {"paid": self._start_watching, "again": self._start_watching,
                    "new_invoice": self._create_invoice}
        self.show_page(PAY, [("close", "Close", NORMAL, self.reject),
                         (key, label, DEFAULT, handlers[key])])
        self.buttons[key].setEnabled(enabled)

    def _set_pay_status(self, text: str, *, busy: bool = False) -> None:
        self._pay_status.setText(text)
        self._pay_status.setVisible(bool(text))
        self._pay_busy.setVisible(busy)

    def _copy_invoice(self) -> None:
        if self._invoice is None or not self._invoice.bolt11:
            return
        QApplication.clipboard().setText(self._invoice.bolt11)
        self._copy_button.setText("Copied")
        # The button is the timer's context: closing the window before it
        # fires cancels it rather than touching a deleted button.
        button = self._copy_button
        QTimer.singleShot(1500, button, lambda: button.setText("Copy Invoice"))

    def _open_wallet(self) -> None:
        if self._invoice is None or not self._invoice.bolt11:
            return
        if not self._open_lightning(self._invoice.bolt11):
            self._set_pay_status("No app on this computer opens Lightning invoices. Scan "
                                 "the code with your phone, or copy the invoice.")

    def _pay_in_browser(self) -> None:
        if self._invoice is not None and self._invoice.checkout_url:
            self.link_activated.emit(self._invoice.checkout_url)

    def _start_watching(self) -> None:
        self._stop_watching()
        watcher = self._watcher_factory(self._api, self._invoice.payment.year, self)
        watcher.paid.connect(self._on_paid)
        watcher.still_waiting.connect(lambda _n: self._set_pay_status(
            "Not confirmed yet. Checking again…", busy=True))
        watcher.expired.connect(self._on_expired)
        watcher.gave_up.connect(self._on_gave_up)
        watcher.failed.connect(self._on_watch_failed)
        self._watcher = watcher
        self._set_pay_status("Checking for your payment…" + self._signer_may_ask(),
                             busy=True)
        self._show_pay_buttons("paid", "Checking…", enabled=False)
        watcher.start(poll_now=True)

    def _stop_watching(self) -> None:
        if self._watcher is not None:
            watcher, self._watcher = self._watcher, None
            watcher.stop()
            if isinstance(watcher, QObject):
                watcher.deleteLater()

    def _on_expired(self, _invoice) -> None:
        self._stop_watching()
        self._set_pay_status("This invoice has expired. Create a new one to pay.")
        self._show_pay_buttons("new_invoice", "Create New Invoice")

    def _on_gave_up(self) -> None:
        self._stop_watching()
        self._set_pay_status("Your payment isn’t confirmed yet. If you paid, wait a "
                             "moment, then click Check Again.")
        self._show_pay_buttons("again", "Check Again")

    def _on_watch_failed(self, error) -> None:
        self._stop_watching()
        self._set_pay_status("")
        self._show_pay_buttons("paid", "I’ve Paid")
        title, message = e21.humanize(error)
        inform(self, title=title, message=message, is_dark=self._is_dark)

    def _on_paid(self, _invoice) -> None:
        self._stop_watching()
        self._just_joined = True

        def back():
            # Paid: the invoice and the ways to pay it are no longer the point.
            for widget in (self._qr, self._copy_button, self._wallet_button,
                           self._browser_button):
                widget.setVisible(False)
            self._pay_title.setText("Payment Received")
            self._pay_intro.setText("")
            self._pay_intro.setVisible(False)
            self.show_page(PAY, [("close", "Close", NORMAL, self.reject),
                             ("again", "Check Again", DEFAULT,
                              lambda: self._load_status(back=back))])
            self._set_pay_status("Your payment arrived. Click Check Again to finish.")

        self._working("Your payment arrived. MyEditor is confirming your membership.",
                      back=back)

        def confirmed(status):
            self._status = status
            if status.is_member:
                self.member_confirmed.emit(self._pubkey or status.pubkey)
                self._show_member()
            else:
                back()
                self._set_pay_status("Your payment arrived. EINUNDZWANZIG is still "
                                     "confirming it. Check again in a moment.")

        self._api.me(confirmed, lambda error: self._fail(error, back))

    # ------------------------------------------------------------------ #
    # Member                                                              #
    # ------------------------------------------------------------------ #

    def _build_member(self) -> None:
        body, col = page()
        self._member_title = text_label("", "title")
        self._member_subtitle = text_label("", "muted")
        col.addWidget(self._member_title)
        col.addWidget(self._member_subtitle)
        col.addSpacing(4)

        relay_extra = QWidget()
        relay_row = QHBoxLayout(relay_extra)
        relay_row.setContentsMargins(0, 2, 0, 0)
        relay_row.setSpacing(8)
        self._relay_button = QPushButton("Add to My Relay List")
        self._relay_button.setAutoDefault(False)
        self._relay_button.clicked.connect(self._add_relay)
        self._relay_result = text_label("", "muted")
        relay_row.addWidget(self._relay_button)
        relay_row.addWidget(self._relay_result, 1)
        col.addLayout(self._item(
            "Members’ relay",
            f"Your notes and articles also go to {_host(MEMBER_RELAY)}. Add it to your "
            "relay list so readers find you there too.", relay_extra)[0])

        col.addLayout(self._item(
            "Media storage",
            f"Your uploads also go to {_host(MEMBER_BLOSSOM)}: {PER_USER_LABEL} in "
            f"total, up to {MAX_FILE_LABEL} per file.")[0])

        name_extra = QWidget()
        name_row = QHBoxLayout(name_extra)
        name_row.setContentsMargins(0, 2, 0, 0)
        self._name_button = QPushButton("Choose an Address…")
        self._name_button.setAutoDefault(False)
        self._name_button.clicked.connect(lambda: self._show_apply(name_only=True))
        name_row.addWidget(self._name_button)
        name_row.addStretch(1)
        name_row_layout, self._name_detail = self._item("Nostr address", "", name_extra)
        col.addLayout(name_row_layout)

        self._member_note = text_label("", "muted")
        col.addWidget(self._member_note)
        col.addStretch(1)

        links = QHBoxLayout()
        links.setSpacing(16)
        self._receipt_link = link_button("Receipt", self._open_receipt)
        self._details_link = link_button(
            "Show Membership Details", lambda: self._load_status(back=self._show_member))
        self._export_link = link_button("Export My Data…", self._export)
        self._erase_link = link_button("Delete My Data…", self._erase)
        for link in (self._receipt_link, self._details_link, self._export_link,
                     self._erase_link):
            links.addWidget(link)
        links.addStretch(1)
        col.addLayout(links)
        self.add_page(MEMBER, body)

    def _show_member(self, note: str = "") -> None:
        self._stop_watching()
        status = self._status
        self._member_title.setText("You’re a Member" if self._just_joined
                                   else "You’re an EINUNDZWANZIG Member")
        if status is not None and status.current_year.paid:
            subtitle = f"Your membership is paid for {status.current_year.year}."
        else:
            subtitle = "Your membership is active."
        if self._just_joined:
            subtitle += " Thank you for supporting the community."
        self._member_subtitle.setText(subtitle)

        has_key = self._joining_available()
        if self._handle:
            self._name_detail.setText(nip05_address(self._handle))
            self._name_button.setText("Change Address…")
        else:
            self._name_detail.setText("Not chosen yet.")
            self._name_button.setText("Choose an Address…")
        self._name_button.setVisible(has_key)

        receipt = status.current_year.receipt_url if status is not None else None
        self._receipt_link.setVisible(bool(receipt))
        self._details_link.setVisible(has_key and status is None)
        self._export_link.setVisible(has_key)
        self._erase_link.setVisible(has_key)
        self._member_note.setText(note)
        self._member_note.setVisible(bool(note))
        self.show_page(MEMBER, [("done", "Done", DEFAULT, self.accept)])

    def _add_relay(self) -> None:
        self._relay_button.setEnabled(False)
        if self._relay_publishes_list:
            self._relay_result.setText("Publishing…" + self._signer_may_ask())
            self.publish_relay_list_requested.emit()
        else:
            self._relay_result.setText("Adding…" + self._signer_may_ask())
            self.add_relay_requested.emit()

    def set_relay_result(self, message: str, *, done: bool = False,
                         offer_list: bool = False) -> None:
        """The outcome of add_relay_requested (or of publishing a list),
        from the window that ran it. ``done`` hides the button; with
        ``offer_list`` it offers to publish the recommended relay list,
        for an account that has none; otherwise it offers to add again."""
        self._relay_result.setText(message)
        self._relay_publishes_list = offer_list and not done
        self._relay_button.setText("Publish Recommended Relay List"
                                   if self._relay_publishes_list else "Add to My Relay List")
        self._relay_button.setEnabled(not done)
        self._relay_button.setVisible(not done)

    def _open_receipt(self) -> None:
        if self._status is not None and self._status.current_year.receipt_url:
            self.link_activated.emit(self._status.current_year.receipt_url)

    def _export(self) -> None:
        self._working("MyEditor is asking EINUNDZWANZIG for the data it keeps about you.",
                      back=self._show_member)
        self._api.export_data(self._on_exported,
                              lambda error: self._fail(error, self._show_member))

    def _on_exported(self, export) -> None:
        self._show_member()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Membership Data", "einundzwanzig-membership.json",
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(export.document, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            inform(self, title="Couldn’t save your data", message=str(exc),
                   is_dark=self._is_dark)
            return
        self._show_member(note="Your membership data is saved.")

    def _erase(self) -> None:
        if not confirm_destructive(
                self, title="Delete your membership data?",
                message="EINUNDZWANZIG deletes the personal details of your "
                        "membership. Fees you paid stay on record for bookkeeping. "
                        "This can’t be undone.",
                action="Delete Data", caution=True, is_dark=self._is_dark):
            return
        self._working("MyEditor is asking EINUNDZWANZIG to delete your data.",
                      back=self._show_member)

        def erased(_result):
            self._status = None
            self._handle = None
            self._just_joined = False
            if self._pubkey:
                # What the app knew from the association no longer holds.
                self.data_erased.emit(self._pubkey)
            self._show_overview(note="Your membership data was deleted.")

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
