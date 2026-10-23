# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connect Signer App: pair a signer app (Amber, nsec.app, …) with MyEditor.

Two ways, one window:

  * **Scan Code** (shown first, the way most signer apps connect): a code
    for the signer app to scan. Behind it is a ``nostrconnect://`` link;
    the channel opens as the window does, and the code works for
    ``_QR_TTL_SECONDS``, after which New Code makes another. Copy Link
    gives the same link as text, for a signer app on this computer.
  * **Paste Link**: a ``bunker://`` link a signer app shows.

The words are the person's, not the protocol's: no URI, pubkey, hex, relay
or NIP number on screen. (Entering a signer's key and servers by hand, the
old Manual tab, is what a ``bunker://`` link holds; a link pasted here
does the same.) The code is always dark on white with a quiet zone, the
way scanners read codes best, whatever the window's theme.

Both ways end in ``profile_connected(Profile)``, and save the profile to
the store, unless the dialog is opened with ``persist=False``: Create
Account checks that the signer holds the new account before anything is
saved, so a signer paired as someone else never replaces a profile that
was already here.

Closing the dialog, for any reason, closes its pairing channel; the
session pool opens its own for the paired profile.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..bunker import (
    NIP46_RELAYS,
    BunkerClient,
    build_nostrconnect_uri,
    humanize_failure,
    parse_bunker_uri,
)
from ..profiles import Profile, ProfileStore
from ..qr import make_qr_pixmap
from ..relay import RelayPool
from .assistant import StepList, link_button, stylesheet, text_label

from . import appearance
from i18n import _, ngettext
from url_safety import is_safe_external_url

log = logging.getLogger(__name__)

# Seconds the code works before New Code makes another; the listener
# closes then.
_QR_TTL_SECONDS: int = 90
_QR_SIZE = 280

# The two ways, in the order shown.
SCAN, PASTE = 0, 1

_EXTRA_CSS = """
QTabWidget::pane { border: none; top: 0px; }
QLabel#qr { padding: 10px; }
"""


class ConnectDialog(QDialog):
    """Pair a signer app: scan a code, or paste a link (module docstring).

    All paths emit ``profile_connected(Profile)`` on success. Cancel and
    closing tear down any in-flight ``BunkerClient`` so no subscription is
    left behind after the dialog is gone.
    """

    profile_connected = Signal(object)  # Profile
    # The person has no signer app: offer the other ways into an account.
    create_requested = Signal()
    restore_requested = Signal()

    def __init__(
        self,
        pool: RelayPool,
        store: ProfileStore,
        parent=None,
        *,
        is_dark: bool = True,
        persist: bool = True,
        offer_alternatives: bool = True,
    ) -> None:
        """``persist`` False emits the paired profile without saving it.
        ``offer_alternatives`` False leaves out the links to Create Account
        and Restore Account, for when the dialog is part of one of those."""
        super().__init__(parent)
        self.setWindowTitle(_("Connect Signer App"))
        self.setModal(True)

        self._pool = pool
        self._store = store
        self._is_dark = is_dark
        self._persist = persist
        self._offer_alternatives = offer_alternatives

        # Exactly one in-flight client at a time; switching ways or closing
        # tears it down so no orphan subscriptions pile up.
        self._client: Optional[BunkerClient] = None
        self._qr_countdown_timer: Optional[QTimer] = None
        self._qr_seconds_left: int = 0

        self._build_ui()
        self.apply_theme(is_dark)
        appearance.bind(self)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        # The code is shown first, and its channel opens with the window.
        self._start_qr_listener()
        self._update_paste_button()

    # -- UI build ----------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        r = appearance.roles(is_dark)
        self.setStyleSheet(stylesheet(is_dark) + _EXTRA_CSS + f"""
            QTabBar::tab {{ background: {r.tab}; color: {r.muted}; border: 1px solid {r.line};
                padding: 6px 12px; min-width: 72px; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
            QTabBar::tab:selected {{ background: {r.soft}; color: {r.ink}; border-color: {r.edge}; }}
            QTabBar::tab:hover {{ color: {r.ink}; }}
        """)
        appearance.surface(self, is_dark)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        layout.addWidget(text_label(_("Connect Your Signer App"), "title"))
        layout.addWidget(text_label(
            _("A signer app keeps your private key and asks you before anything is "
              "signed. MyEditor never sees the key.")))

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(self._build_scan_tab(), _("Scan Code"))
        self._tabs.addTab(self._build_paste_tab(), _("Paste Link"))
        self._tabs.tabBar().setAccessibleName(_("How to connect"))
        layout.addWidget(self._tabs, 1)

        # What is happening now: waiting, connecting, or what went wrong.
        self._status = text_label("", "muted")
        self._status.setMinimumHeight(18)
        layout.addWidget(self._status)

        # The other ways into an account, on a line of their own so longer
        # words never squeeze them.
        if self._offer_alternatives:
            others = QHBoxLayout()
            others.setSpacing(16)
            others.addWidget(text_label(_("No signer app yet?"), "muted", wrap=False))
            for label, signal in ((_("Create an Account"), self.create_requested),
                                  (_("Restore from Backup"), self.restore_requested)):
                others.addWidget(link_button(
                    label, lambda sig=signal: (self._on_cancel(), sig.emit())))
            others.addStretch(1)
            layout.addLayout(others)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._cancel_btn = QPushButton(_("Cancel"))
        self._cancel_btn.setAutoDefault(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        footer.addWidget(self._cancel_btn)
        self._paste_btn = QPushButton(_("Connect"))
        self._paste_btn.setAutoDefault(False)
        self._paste_btn.clicked.connect(self._on_paste_connect)
        footer.addWidget(self._paste_btn)
        layout.addLayout(footer)
        # At least this wide, so the code and the steps beside each other
        # never squeeze; longer words widen the window instead.
        layout.addStrut(460)

    def _build_scan_tab(self) -> QWidget:
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(10)
        column.addWidget(StepList([
            ("open", _("In your signer app, choose to connect an app (in Amber: "
                       "Connect app, the scan icon).")),
            ("scan", _("Scan this code, then approve the request in the app.")),
        ]))

        self._qr_label = QLabel()
        self._qr_label.setObjectName("qr")
        self._qr_label.setAlignment(Qt.AlignCenter)
        self._qr_label.setAccessibleName(_("Code to scan with your signer app"))
        column.addWidget(self._qr_label, 0, Qt.AlignHCenter)

        self._countdown_label = text_label("", "muted")
        self._countdown_label.setAlignment(Qt.AlignCenter)
        column.addWidget(self._countdown_label)

        row = QHBoxLayout()
        row.addStretch(1)
        # A new code, once this one has expired or failed.
        self._regen_btn = QPushButton(_("New Code"))
        self._regen_btn.setAutoDefault(False)
        self._regen_btn.clicked.connect(self._start_qr_listener)
        self._regen_btn.hide()
        row.addWidget(self._regen_btn)
        # The same link as text, for a signer app on this computer.
        self._copy_btn = link_button(_("Copy Link"), self._copy_qr_uri)
        self._copy_btn.setToolTip(_("For a signer app on this computer"))
        row.addWidget(self._copy_btn)
        row.addStretch(1)
        column.addLayout(row)
        column.addStretch(1)
        self._qr_uri = ""
        return tab

    def _build_paste_tab(self) -> QWidget:
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        label = text_label(_("Connection link:"), "item_title")
        self._paste_edit = QLineEdit()
        self._paste_edit.setPlaceholderText("bunker://…")
        label.setBuddy(self._paste_edit)
        self._paste_edit.setAccessibleName(_("Connection link"))
        self._paste_edit.textChanged.connect(self._update_paste_button)
        self._paste_edit.returnPressed.connect(self._on_paste_connect)
        column.addWidget(label)
        column.addWidget(self._paste_edit)
        column.addWidget(text_label(
            _("Some signer apps show a link instead of scanning a code. Copy it there "
              "and paste it here; it starts with bunker://."), "help"))
        column.addStretch(1)
        return tab

    # -- paste ---------------------------------------------------------------

    def _update_paste_button(self) -> None:
        on_paste = self._tabs.currentIndex() == PASTE
        self._paste_btn.setVisible(on_paste)
        self._paste_btn.setDefault(on_paste)
        text = self._paste_edit.text().strip()
        self._paste_btn.setEnabled(self._client is None and text.startswith("bunker://"))

    def _on_paste_connect(self) -> None:
        uri = self._paste_edit.text().strip()
        if self._client is not None or not uri:
            return
        try:
            parse_bunker_uri(uri)
        except ValueError as exc:
            log.info("connection link refused: %s", exc)
            self._set_status(_("That link can’t be used. Copy it again from your signer "
                               "app: it starts with bunker://."), error=True)
            return
        self._begin_bunker_connect(uri)

    # -- scan ----------------------------------------------------------------

    def _start_qr_listener(self) -> None:
        """A new pairing secret and channel, its code shown, and the clock."""
        self._teardown_client(reason="restarting QR")
        self._set_status("")
        self._regen_btn.hide()

        secret = secrets.token_hex(8)
        client = BunkerClient(self._pool, parent=self)
        self._watch_client_diagnostics(client)
        local_pk = client.listen_for_nostrconnect(
            relays=list(NIP46_RELAYS),
            secret=secret,
            on_success=self._on_pair_success,
            on_failure=self._on_pair_failure,
            timeout_ms=_QR_TTL_SECONDS * 1000,
        )
        self._qr_uri = build_nostrconnect_uri(local_pk, list(NIP46_RELAYS), secret)
        self._client = client
        # Dark on white, whatever the theme: scanners read that best.
        self._qr_label.setPixmap(make_qr_pixmap(self._qr_uri, size=_QR_SIZE,
                                                dark="#000000", light="#FFFFFF"))
        self._qr_label.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._set_status(_("Waiting for your signer app…"))
        self._start_qr_countdown()

    def _start_qr_countdown(self) -> None:
        self._qr_seconds_left = _QR_TTL_SECONDS
        self._update_countdown_label()
        self._stop_qr_listener()
        self._qr_countdown_timer = QTimer(self)
        self._qr_countdown_timer.setInterval(1000)
        self._qr_countdown_timer.timeout.connect(self._tick_countdown)
        self._qr_countdown_timer.start()

    def _tick_countdown(self) -> None:
        self._qr_seconds_left = max(0, self._qr_seconds_left - 1)
        self._update_countdown_label()
        if self._qr_seconds_left == 0:
            self._retire_code(_("This code has expired."))

    def _update_countdown_label(self) -> None:
        if self._qr_seconds_left > 0:
            self._countdown_label.setText(ngettext(
                "This code works for {seconds} more second.",
                "This code works for {seconds} more seconds.",
                self._qr_seconds_left).format(seconds=self._qr_seconds_left))

    def _retire_code(self, why: str) -> None:
        """The code no longer pairs: dim it, say ``why``, offer a new one."""
        self._stop_qr_listener()
        self._teardown_client(reason="code retired")
        self._qr_label.setEnabled(False)
        self._copy_btn.setEnabled(False)
        self._countdown_label.setText(why)
        self._set_status("")
        self._regen_btn.show()

    def _copy_qr_uri(self) -> None:
        if self._qr_uri:
            QApplication.clipboard().setText(self._qr_uri)
            self._set_status(_("Link copied. Paste it in your signer app."))

    def _stop_qr_listener(self) -> None:
        if self._qr_countdown_timer is not None:
            self._qr_countdown_timer.stop()
            self._qr_countdown_timer = None

    # -- shared connect flow -----------------------------------------------

    def _begin_bunker_connect(self, uri: str) -> None:
        self._teardown_client(reason="starting new attempt")
        self._client = BunkerClient(self._pool, parent=self)
        self._watch_client_diagnostics(self._client)
        self._set_status(_("Contacting your signer app. Approve the request there…"))
        self._update_paste_button()
        self._client.connect_to_bunker(
            uri,
            on_success=self._on_pair_success,
            on_failure=self._on_pair_failure,
        )

    def _watch_client_diagnostics(self, client: BunkerClient) -> None:
        """Surface the two things that otherwise present as a bare timeout."""
        client.auth_challenge.connect(self._on_auth_challenge)
        client.unreadable_reply.connect(self._on_unreadable_reply)

    def _on_auth_challenge(self, url: str) -> None:
        """The signer wants the user to authenticate before it will answer."""
        if is_safe_external_url(url):
            QDesktopServices.openUrl(QUrl(url))
            self._set_status(_(
                "Your signer app needs you to approve this in the browser window "
                "that just opened. Waiting…"
            ))
        else:
            self._set_status(
                _("Your signer app asked for approval at an address that cannot be "
                  "opened safely, so the connection was not continued."),
                error=True,
            )

    def _on_unreadable_reply(self, _signer_pubkey: str) -> None:
        """A signer answered but the payload was not readable.

        Almost always an encryption mismatch. Saying so beats a timeout that
        looks the same as the signer never having replied.
        """
        self._set_status(
            _("Your signer app answered in a form MyEditor can’t read. An update "
              "of the signer app may help."),
            error=True,
        )

    def _on_pair_success(self, user_pubkey_hex: str) -> None:
        assert self._client is not None
        profile = Profile(
            user_pubkey=user_pubkey_hex,
            bunker_pubkey=self._client.bunker_pubkey or "",
            bunker_relays=list(self._client.relays),
            local_secret_hex=self._client.local_secret_hex or "",
            display_name="",
            picture="",
        )
        if self._persist:
            self._store.upsert(profile)
        self._stop_qr_listener()
        self.profile_connected.emit(profile)
        self.accept()

    def _on_pair_failure(self, reason: str) -> None:
        log.info("signer pairing failed: %s", reason)
        self._teardown_client(reason=reason)
        if self._tabs.currentIndex() == SCAN:
            if self._qr_seconds_left == 0:
                return                    # the code ran out: said already, not an error
            self._retire_code("")
        self._set_status(_("Couldn’t connect: {reason}").format(
            reason=humanize_failure(reason)), error=True)
        self._update_paste_button()

    # -- bookkeeping -------------------------------------------------------

    def _teardown_client(self, *, reason: str) -> None:
        if self._client is not None:
            client = self._client
            self._client = None
            client.close(reason=reason)

    def _on_tab_changed(self, index: int) -> None:
        self._teardown_client(reason="switched way")
        self._stop_qr_listener()
        self._set_status("")
        if index == SCAN:
            self._start_qr_listener()
        else:
            self._countdown_label.setText("")
            self._paste_edit.setFocus()
        self._update_paste_button()

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setObjectName("error" if error else "muted")
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    # -- cancel / close ----------------------------------------------------

    def _on_cancel(self) -> None:
        self.reject()

    def done(self, result: int) -> None:  # type: ignore[override]
        # Accepted, canceled or closed: the pairing channel goes with the
        # dialog, so nothing keeps listening once it is gone.
        self._teardown_client(reason="dialog dismissed")
        self._stop_qr_listener()
        super().done(result)
