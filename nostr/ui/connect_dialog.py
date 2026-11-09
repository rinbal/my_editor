# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connect Signer App: pair a signer app (Amber, Primal, Amethyst,
Bunker46, ...) with MyEditor, on one page (the M1 layout the product
owner chose on 2026-11-09; research/signer-pairing.md, section 9).

    Connect Signer App
    Your key stays in your signer app. ...
    ┌──────────────────────────────────────┐
    │               [ code ]               │
    │        Scan with your phone          │
    │ Open Amber, Primal or Amethyst and … │
    └──────────────────────────────────────┘
    [ Connect with Bunker46 ] [ Open Signer App on This Computer ]
    ▸ Details      (Copy Link; Connection address: [ … ] Connect)
    (?)                                                  [Cancel]

* **The code first**, for a phone. Behind it is a ``nostrconnect://``
  link whose channel opens with the window. It renews by itself before
  it runs out, with no timer on screen: each code keeps listening a
  while after the next is shown (``_GRACE_S``), so a phone that scanned
  it just before still pairs. After ``_RENEWALS`` renewals it stops and
  offers New Code, so a window left open does not listen for ever.
* **Connect with Bunker46** asks once for the address of the person's
  Bunker46 (https only, kept in the settings), then opens
  ``<address>/connections?nostrconnect=<link>`` in the browser, where the
  person picks the key and confirms. Bunker46 has no scanner.
* **Open Signer App on This Computer** hands the link to the system
  (default_apps.open_link), which opens the app set up for such links
  (Amber Desktop on macOS and Linux). When none is, one line says so
  (expected on Windows: no registration was found there).
* **Details**, collapsed: Copy Link, and the connection address a signer
  app shows (``bunker://``), for the apps that show one.

Opening an app waits for it (``_APP_WAIT_S``) with the code it was given,
and says so; Cancel, or opening it again, is always there (lesson 12).

The words are the person's, not the protocol's: no link scheme, key,
hex, relay or NIP number in the primary text (lesson 26); the technical
facts are in the help button's tooltip. The code is always dark on
white with a quiet zone, the way scanners read codes best, whatever the
window's theme.

Every way ends in ``profile_connected(Profile)``, and saves the profile
to the store, unless the dialog is opened with ``persist=False``: Create
Account checks that the signer holds the new account before anything is
saved, so a signer paired as someone else never replaces a profile that
was already here.

Closing the dialog, for any reason, closes every pairing channel; the
session pool opens its own for the paired profile.
"""

from __future__ import annotations

import logging
import secrets
from typing import Callable, List, Optional
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import settings
from default_apps import open_link
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
from .assistant import link_button, stylesheet, text_label
from .tips import HelpButton, help_button_stylesheet, tip_on_focus

from . import appearance
from announce import announce
from i18n import _
from url_safety import is_safe_external_url

log = logging.getLogger(__name__)

# How long a code is shown before the next one takes its place, and how
# long it keeps listening after that; after this many renewals the code
# stops (about ten minutes) and New Code makes another.
_CODE_LIFE_S: int = 90
_GRACE_S: int = 45
_RENEWALS: int = 6
# How long a code opened in an app (Bunker46, a signer app on this
# computer) waits for that app: the person picks a key and confirms there.
_APP_WAIT_S: int = 300
_QR_SIZE = 280

# Where the address of the person's Bunker46 is kept (settings.json).
BUNKER46_SETTING = "bunker46_address"

_EXTRA_CSS = """
QLabel#qr {{ padding: 6px; }}
QFrame#scan_card {{ background: {card}; border: 1px solid {line}; border-radius: 10px; }}
QLabel#scan_caption {{ font-size: 15px; font-weight: 600; }}
QToolButton#disclosure {{ border: 1px solid transparent; padding: 2px 0; }}
QToolButton#disclosure:focus {{ border-color: {ring}; }}
"""


def bunker46_address(text: str) -> Optional[str]:
    """The address of a Bunker46 as typed: https only, a host, no user
    name, query or fragment, without a trailing slash; a bare host gets
    https. None when it is not one. A Bunker46 runs where the person runs
    it, also on their own network, so a local name is fine."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        parts.port                                  # raises for a port that is no number
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not (parts.hostname or "").strip():
        return None
    if "@" in parts.netloc or parts.query or parts.fragment or any(c.isspace() for c in text):
        return None
    address = f"https://{parts.netloc}{parts.path}".rstrip("/")
    return address if is_safe_external_url(address) else None


def bunker46_link(address: str, uri: str) -> QUrl:
    """The page of ``address`` (a Bunker46) that takes the connection
    link ``uri``: ``/connections?nostrconnect=<uri, encoded>`` (Bunker46's
    router, research/signer-pairing.md section 6)."""
    encoded = f"{address}/connections?nostrconnect={quote(uri, safe='')}"
    return QUrl.fromEncoded(QByteArray(encoded.encode("ascii")), QUrl.ParsingMode.StrictMode)


def remembered_bunker46() -> Optional[str]:
    """The Bunker46 address kept in the settings, if it is still one."""
    return bunker46_address(str(settings.load_settings().get(BUNKER46_SETTING) or ""))


class ConnectDialog(QDialog):
    """Pair a signer app (module docstring).

    Every way emits ``profile_connected(Profile)`` on success. Cancel and
    closing tear down every ``BunkerClient`` in flight, so no
    subscription is left behind after the dialog is gone.
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

        # The codes listening now, the one shown last; each times out by
        # itself (_CODE_LIFE_S + _GRACE_S). A pasted connection address
        # has its own client, and only one way is in flight at a time.
        self._listeners: List[BunkerClient] = []
        self._client: Optional[BunkerClient] = None
        self._qr_uri = ""
        self._renewals = 0
        # The app a code was opened in, while it waits for it.
        self._waiting_for = ""
        self._renew_timer = QTimer(self)
        self._renew_timer.setSingleShot(True)
        self._renew_timer.timeout.connect(self._renew)

        self._build_ui()
        self.apply_theme(is_dark)
        appearance.bind(self)
        # The code is shown first, and its channel opens with the window.
        self._start_code()
        self._update_paste_button()

    # -- UI build ----------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        r = appearance.roles(is_dark)
        self.setStyleSheet(stylesheet(is_dark) + help_button_stylesheet(r)
                           + _EXTRA_CSS.format(card=r.field, line=r.line, ring=r.focus))
        appearance.surface(self, is_dark)
        self._fit_buttons()

    def _fit_buttons(self) -> None:
        """The style sheet gives a button a small minimum width, which a
        layout keeps to over the button's own: the two side by side would
        cut longer words ("Signer-App auf diesem Computer öffnen"). Measured
        again once the window shows, in the font it shows with."""
        for button in (self._bunker46_btn, self._open_here_btn):
            button.ensurePolished()
            button.setMinimumWidth(button.sizeHint().width())

    def showEvent(self, event) -> None:  # noqa: N802, Qt's name
        self._fit_buttons()
        super().showEvent(event)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        layout.addWidget(text_label(_("Connect Signer App"), "title"))
        layout.addWidget(text_label(
            _("Your key stays in your signer app. MyEditor asks it only when something "
              "has to be signed.")))

        layout.addWidget(self._build_scan_card())
        layout.addLayout(self._build_computer_row())
        layout.addWidget(self._build_bunker46_ask())
        layout.addWidget(self._build_details())

        # What is happening now: waiting, connecting, or what went wrong.
        self._status = text_label("", "muted")
        self._status.setMinimumHeight(18)
        layout.addWidget(self._status)
        layout.addStretch(1)

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
        self.help_button = HelpButton(_(
            "Technical: a Nostr Connect connection. The code is a nostrconnect link and "
            "renews by itself. Connect with Bunker46 opens it on your Bunker46's connections "
            "page."))
        footer.addWidget(self.help_button, 0, Qt.AlignmentFlag.AlignVCenter)
        footer.addStretch(1)
        self._cancel_btn = QPushButton(_("Cancel"))
        self._cancel_btn.setAutoDefault(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        footer.addWidget(self._cancel_btn)
        layout.addLayout(footer)
        # At least this wide, so the two buttons side by side never
        # squeeze; longer words widen the window instead.
        layout.addStrut(500)

    def _build_scan_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("scan_card")
        column = QVBoxLayout(card)
        column.setContentsMargins(20, 16, 20, 16)
        column.setSpacing(6)
        self._qr_label = QLabel()
        self._qr_label.setObjectName("qr")
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setAccessibleName(_("Code to scan with your phone"))
        column.addWidget(self._qr_label, 0, Qt.AlignmentFlag.AlignHCenter)
        caption = text_label(_("Scan with your phone"), "scan_caption")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(caption)
        steps = text_label(_("Open Amber, Primal or Amethyst and scan the code."), "muted")
        steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(steps)
        # Why the code stopped, and a new one (rare: it renews by itself).
        self._code_note = text_label("", "muted")
        self._code_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._code_note.hide()
        column.addWidget(self._code_note)
        self._regen_btn = QPushButton(_("New Code"))
        self._regen_btn.setAutoDefault(False)
        self._regen_btn.clicked.connect(self._new_code)
        self._regen_btn.hide()
        column.addWidget(self._regen_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        return card

    def _build_computer_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self._bunker46_btn = QPushButton(_("Connect with Bunker46"))
        self._bunker46_btn.setAutoDefault(False)
        self._bunker46_btn.clicked.connect(self._on_bunker46)
        self._open_here_btn = QPushButton(_("Open Signer App on This Computer"))
        self._open_here_btn.setAutoDefault(False)
        tip_on_focus(self._open_here_btn, _(
            "Opens a signer app set up on this computer for connection links, for example "
            "Amber Desktop."))
        self._open_here_btn.clicked.connect(self._on_open_here)
        for button in (self._bunker46_btn, self._open_here_btn):
            row.addWidget(button, 1)
        return row

    def _build_bunker46_ask(self) -> QWidget:
        """Where the address of the person's Bunker46 is asked, once."""
        box = self._bunker46_box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        label = text_label(_("Your Bunker46 address"), "item_title")
        self._bunker46_edit = QLineEdit()
        self._bunker46_edit.setPlaceholderText("https://bunker46.example.com")
        self._bunker46_edit.setAccessibleName(_("Your Bunker46 address"))
        label.setBuddy(self._bunker46_edit)
        self._bunker46_edit.textChanged.connect(self._update_bunker46_button)
        self._bunker46_edit.returnPressed.connect(self._on_bunker46_address)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._bunker46_edit, 1)
        self._bunker46_go = QPushButton(_("Open"))
        self._bunker46_go.setAutoDefault(False)
        self._bunker46_go.clicked.connect(self._on_bunker46_address)
        row.addWidget(self._bunker46_go)
        self._bunker46_back = link_button(_("Cancel"), self._close_bunker46_ask)
        self._bunker46_back.setAccessibleName(_("Cancel the Bunker46 address"))
        row.addWidget(self._bunker46_back)
        self._bunker46_problem = text_label("", "error")
        self._bunker46_problem.hide()
        column.addWidget(label)
        column.addLayout(row)
        column.addWidget(self._bunker46_problem)
        column.addWidget(text_label(_("MyEditor keeps it, so it asks only this once."), "help"))
        box.hide()
        return box

    def _build_details(self) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.details_toggle = QToolButton()
        self.details_toggle.setObjectName("disclosure")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setText(_("Details"))
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setAccessibleName(_("Details"))
        self.details_toggle.toggled.connect(self._show_details)
        column.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        panel = self.details = QWidget()
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(18, 2, 0, 0)
        inner.setSpacing(6)
        copy_row = QHBoxLayout()
        self._copy_btn = link_button(_("Copy Link"), self._copy_qr_uri)
        tip_on_focus(self._copy_btn, _("For a signer app that takes a pasted link"))
        copy_row.addWidget(self._copy_btn)
        copy_row.addStretch(1)
        inner.addLayout(copy_row)
        label = text_label(_("Connection address:"), "item_title")
        self._paste_edit = QLineEdit()
        self._paste_edit.setPlaceholderText("bunker://…")
        label.setBuddy(self._paste_edit)
        self._paste_edit.setAccessibleName(_("Connection address"))
        self._paste_edit.textChanged.connect(self._update_paste_button)
        self._paste_edit.returnPressed.connect(self._on_paste_connect)
        paste_row = QHBoxLayout()
        paste_row.setSpacing(8)
        paste_row.addWidget(self._paste_edit, 1)
        self._paste_btn = QPushButton(_("Connect"))
        self._paste_btn.setAutoDefault(False)
        self._paste_btn.clicked.connect(self._on_paste_connect)
        paste_row.addWidget(self._paste_btn)
        inner.addWidget(label)
        inner.addLayout(paste_row)
        inner.addWidget(text_label(
            _("Some signer apps show a connection address instead of scanning a code. "
              "Copy it there and paste it here; it starts with bunker://."), "help"))
        # The Bunker46 kept, and a way to change it.
        self._bunker46_row = QWidget()
        kept = QHBoxLayout(self._bunker46_row)
        kept.setContentsMargins(0, 4, 0, 0)
        self._bunker46_kept = text_label("", "muted", wrap=False)
        kept.addWidget(self._bunker46_kept)
        change = link_button(_("Change"), self._change_bunker46)
        change.setAccessibleName(_("Change the Bunker46 address"))
        kept.addWidget(change)
        kept.addStretch(1)
        inner.addWidget(self._bunker46_row)
        panel.hide()
        column.addWidget(panel)
        self._show_bunker46_kept()
        return box

    def _show_details(self, shown: bool) -> None:
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                         else Qt.ArrowType.RightArrow)
        self.details.setVisible(shown)
        self._update_paste_button()

    def _show_bunker46_kept(self) -> None:
        address = remembered_bunker46()
        self._bunker46_row.setVisible(address is not None)
        self._bunker46_kept.setText(_("Bunker46: {address}").format(
            address=urlsplit(address).netloc if address else ""))

    # -- the code ----------------------------------------------------------

    def _listen(self, life_s: int) -> BunkerClient:
        """A new code: a pairing secret, its channel listening ``life_s``
        seconds, and its link; shown, and the one the buttons open."""
        secret = secrets.token_hex(8)
        client = BunkerClient(self._pool, parent=self)
        self._watch_client_diagnostics(client)
        local_pk = client.listen_for_nostrconnect(
            relays=list(NIP46_RELAYS),
            secret=secret,
            on_success=lambda pubkey, c=client: self._on_pair_success(pubkey, c),
            on_failure=lambda reason, c=client: self._on_pair_failure(reason, c),
            timeout_ms=life_s * 1000,
        )
        self._listeners.append(client)
        self._client = client
        self._qr_uri = build_nostrconnect_uri(local_pk, list(NIP46_RELAYS), secret)
        # Dark on white, whatever the theme: scanners read that best.
        self._qr_label.setPixmap(make_qr_pixmap(self._qr_uri, size=_QR_SIZE,
                                                dark="#000000", light="#FFFFFF"))
        self._qr_label.setEnabled(True)
        self._code_note.hide()
        self._regen_btn.hide()
        for button in (self._copy_btn, self._bunker46_btn, self._open_here_btn):
            button.setEnabled(True)
        return client

    def _start_code(self) -> None:
        """The code shown while nobody opened it in an app: renewed by
        itself every ``_CODE_LIFE_S`` seconds."""
        self._waiting_for = ""
        self._listen(_CODE_LIFE_S + _GRACE_S)
        self._renew_timer.start(_CODE_LIFE_S * 1000)

    def _renew(self) -> None:
        """The next code, before this one runs out. The one shown keeps
        listening for ``_GRACE_S`` seconds more, so a phone that scanned it
        just now still pairs."""
        if self._renewals >= _RENEWALS:
            self._retire_code(_("This code has expired."))
            return
        self._renewals += 1
        self._start_code()

    def _new_code(self) -> None:
        self._renewals = 0
        self._set_status("")
        self._start_code()
        self._qr_label.setFocus()

    def _retire_code(self, why: str) -> None:
        """No code pairs any more: dim it, say ``why``, offer a new one."""
        self._renew_timer.stop()
        self._close_listeners()
        self._waiting_for = ""
        self._qr_label.setEnabled(False)
        for button in (self._copy_btn, self._bunker46_btn, self._open_here_btn):
            button.setEnabled(False)
        self._code_note.setText(why)
        self._code_note.setVisible(bool(why))
        self._regen_btn.show()

    def _copy_qr_uri(self) -> None:
        if self._qr_uri:
            QApplication.clipboard().setText(self._qr_uri)
            self._set_status(_("Link copied. Paste it in your signer app."))

    # -- opening the code in an app ----------------------------------------

    def _open_in(self, app: str, opener: Callable[[str], bool]) -> bool:
        """Open a fresh code in ``app``, through ``opener(link)``, and wait
        for that app; False when the system opened nothing."""
        self._renew_timer.stop()
        self._close_listeners()
        self._listen(_APP_WAIT_S)
        if not opener(self._qr_uri):
            self._close_listeners()
            self._start_code()
            return False
        self._waiting_for = app
        self._set_status(_("Waiting for {app}. Approve the connection there.").format(app=app))
        return True

    def _on_open_here(self) -> None:
        if not self._open_in(_("your signer app"), open_link):
            self._set_status(_("No signer app for connection links is set up on this "
                               "computer."))

    def _on_bunker46(self) -> None:
        address = remembered_bunker46()
        if address is None:
            self._bunker46_box.show()
            self._bunker46_edit.setFocus()
            self._update_bunker46_button()
            return
        self._open_bunker46(address)

    def _open_bunker46(self, address: str) -> None:
        if not self._open_in("Bunker46", lambda uri: QDesktopServices.openUrl(
                bunker46_link(address, uri))):
            self._set_status(_("Your browser couldn’t be opened."), error=True)

    def _update_bunker46_button(self) -> None:
        self._bunker46_go.setEnabled(bool(self._bunker46_edit.text().strip()))
        self._bunker46_go.setDefault(self._bunker46_box.isVisibleTo(self))
        self._bunker46_problem.hide()

    def _on_bunker46_address(self) -> None:
        address = bunker46_address(self._bunker46_edit.text())
        if address is None:
            problem = _("That isn’t an address MyEditor can open. Use the one your Bunker46 "
                        "runs at; it starts with https://.")
            self._bunker46_problem.setText(problem)
            self._bunker46_problem.show()
            self._bunker46_edit.setAccessibleDescription(problem)
            announce(self._bunker46_edit, problem)
            return
        settings.save_setting(BUNKER46_SETTING, address)
        self._show_bunker46_kept()
        self._close_bunker46_ask()
        self._open_bunker46(address)

    def _close_bunker46_ask(self) -> None:
        self._bunker46_box.hide()
        self._bunker46_go.setDefault(False)
        self._bunker46_edit.clear()
        self._bunker46_edit.setAccessibleDescription("")
        self._bunker46_btn.setFocus()               # back where it was asked from

    def _change_bunker46(self) -> None:
        self._bunker46_box.show()
        self._bunker46_edit.setText(remembered_bunker46() or "")
        self._bunker46_edit.selectAll()
        self._bunker46_edit.setFocus()
        self._update_bunker46_button()

    # -- paste -------------------------------------------------------------

    def _update_paste_button(self) -> None:
        text = self._paste_edit.text().strip()
        usable = self._client is None or self._client in self._listeners
        self._paste_btn.setEnabled(usable and bool(text))
        self._paste_btn.setDefault(self.details.isVisibleTo(self) and bool(text))

    def _on_paste_connect(self) -> None:
        uri = self._paste_edit.text().strip()
        if not uri or (self._client is not None and self._client not in self._listeners):
            return
        try:
            parse_bunker_uri(uri)
        except ValueError as exc:
            log.info("connection address refused: %s", exc)
            self._set_status(_("That address can’t be used. Copy it again from your signer "
                               "app: it starts with bunker://."), error=True)
            return
        self._begin_bunker_connect(uri)

    # -- shared connect flow -----------------------------------------------

    def _begin_bunker_connect(self, uri: str) -> None:
        self._renew_timer.stop()
        self._close_listeners()
        client = self._client = BunkerClient(self._pool, parent=self)
        self._watch_client_diagnostics(client)
        self._set_status(_("Contacting your signer app. Approve the request there…"))
        self._update_paste_button()
        client.connect_to_bunker(
            uri,
            on_success=lambda pubkey, c=client: self._on_pair_success(pubkey, c),
            on_failure=lambda reason, c=client: self._on_pair_failure(reason, c),
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

    def _on_pair_success(self, user_pubkey_hex: str, client=None) -> None:
        client = client if client is not None else self._client
        assert client is not None
        profile = Profile(
            user_pubkey=user_pubkey_hex,
            bunker_pubkey=client.bunker_pubkey or "",
            bunker_relays=list(client.relays),
            local_secret_hex=client.local_secret_hex or "",
            display_name="",
            picture="",
        )
        if self._persist:
            self._store.upsert(profile)
        self._renew_timer.stop()
        self.profile_connected.emit(profile)
        self.accept()

    def _on_pair_failure(self, reason: str, client=None) -> None:
        client = client if client is not None else self._client
        log.info("signer pairing failed: %s", reason)
        if client in self._listeners:
            self._listeners.remove(client)
            client.close(reason=reason)
            if client is not self._client:
                return                  # an earlier code ran out: the next one is shown
            self._client = None
            if "timed out" in reason:
                # Running out is not an error: an app opened with the code
                # did not answer in time (a new code; opening it again is
                # there), or the code ran out before it was renewed.
                self._set_status("")
                if self._waiting_for:
                    self._start_code()
                else:
                    self._retire_code(_("This code has expired."))
                self._update_paste_button()
                return
            self._retire_code("")
        else:
            self._teardown_client(reason=reason)
            self._start_code()                      # the code again, for another try
        self._set_status(_("Couldn’t connect: {reason}").format(
            reason=humanize_failure(reason)), error=True)
        self._update_paste_button()

    # -- bookkeeping -------------------------------------------------------

    def _close_listeners(self) -> None:
        for client in self._listeners:
            client.close(reason="code replaced")
        if self._client in self._listeners:
            self._client = None
        self._listeners = []

    def _teardown_client(self, *, reason: str) -> None:
        self._renew_timer.stop()
        for client in self._listeners:
            client.close(reason=reason)
        self._listeners = []
        if self._client is not None:
            client = self._client
            self._client = None
            client.close(reason=reason)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setObjectName("error" if error else "muted")
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
        announce(self._status, text)

    # -- cancel / close ----------------------------------------------------

    def _on_cancel(self) -> None:
        self.reject()

    def done(self, result: int) -> None:  # type: ignore[override]
        # Accepted, canceled or closed: every pairing channel goes with the
        # dialog, so nothing keeps listening once it is gone.
        self._teardown_client(reason="dialog dismissed")
        super().done(result)
