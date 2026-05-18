"""Connect-to-Nostr dialog — three pairing flows on one tabbed surface.

Tabs:

  * **Paste URI**     — paste a ``bunker://`` URI generated in Amber et al.
  * **Scan QR**       — display a ``nostrconnect://`` QR for the signer to
                        scan; the channel opens once the signer's connect
                        event arrives.
  * **Manual**        — build a ``bunker://`` URI from separate fields when
                        you have the pubkey + relays + secret on paper but
                        not as a URL.

All three paths converge on the same ``profile_connected(Profile)`` signal
and persist the new profile to the on-disk store identically.
"""

from __future__ import annotations

import secrets
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import DEFAULT_RELAYS
from ..bunker import (
    BunkerClient,
    build_nostrconnect_uri,
    parse_bunker_uri,
)
from ..profiles import Profile, ProfileStore
from ..qr import make_qr_pixmap
from ..relay import RelayPool


# Subset of curated relays used for the nostrconnect:// QR handshake. The
# URI itself goes through every relay listed here; smaller set = smaller
# QR matrix = easier to scan from across a desk.
_NOSTRCONNECT_RELAYS: tuple[str, ...] = DEFAULT_RELAYS[:3]

# Seconds the QR is valid before we ask the user to regenerate. After this,
# the listener is closed and the "Try Again" affordance appears.
_QR_TTL_SECONDS: int = 90


# --------------------------------------------------------------------------- #
# Stylesheets — same palette as the rest of the editor                        #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#connect_hint { color: #858585; }
QLabel#connect_status { color: #FFB347; }
QLabel#qr_label { background: #1E1E1E; padding: 8px; }
QLabel#countdown { color: #858585; font-size: 11px; }
QLineEdit, QTextEdit {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #264F78;
    font-family: "Noto Sans Mono", monospace;
    font-size: 11px;
}
QPushButton {
    background: #2D2D30;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    padding: 6px 14px;
    border-radius: 4px;
}
QPushButton:hover { background: #3C3C3C; }
QPushButton:pressed { background: #1E1E1E; }
QPushButton:disabled { background: #252526; color: #6A6A6A; border-color: #2D2D2D; }

QTabWidget::pane { border: 1px solid #3C3C3C; border-radius: 4px; top: -1px; }
QTabBar::tab {
    background: #252526;
    color: #CCCCCC;
    border: 1px solid #3C3C3C;
    padding: 6px 14px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #1E1E1E; color: #FFFFFF; }
QTabBar::tab:hover { background: #3C3C3C; }
"""

_LIGHT_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#connect_hint { color: #777777; }
QLabel#connect_status { color: #A05000; }
QLabel#qr_label { background: #FFFFFF; padding: 8px; }
QLabel#countdown { color: #999999; font-size: 11px; }
QLineEdit, QTextEdit {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #0078D4;
    font-family: "Noto Sans Mono", monospace;
    font-size: 11px;
}
QPushButton {
    background: #ECECEC;
    color: #333333;
    border: 1px solid #CCCCCC;
    padding: 6px 14px;
    border-radius: 4px;
}
QPushButton:hover { background: #E1E1E1; }
QPushButton:pressed { background: #D0D0D0; }
QPushButton:disabled { background: #F8F8F8; color: #BBBBBB; border-color: #EBEBEB; }

QTabWidget::pane { border: 1px solid #E1E1E1; border-radius: 4px; top: -1px; }
QTabBar::tab {
    background: #F3F3F3;
    color: #555555;
    border: 1px solid #E1E1E1;
    padding: 6px 14px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #FFFFFF; color: #000000; }
QTabBar::tab:hover { background: #ECECEC; }
"""


# --------------------------------------------------------------------------- #
# Dialog                                                                       #
# --------------------------------------------------------------------------- #

class ConnectDialog(QDialog):
    """One dialog, three pairing tabs.

    All paths emit ``profile_connected(Profile)`` on success.  Cancel /
    close tears down any in-flight ``BunkerClient`` so we never leave a
    relay subscription dangling after the dialog is dismissed.
    """

    profile_connected = Signal(object)  # Profile

    def __init__(
        self,
        pool: RelayPool,
        store: ProfileStore,
        parent=None,
        *,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect Nostr Signer")
        self.setModal(True)
        self.setMinimumSize(560, 480)

        self._pool = pool
        self._store = store
        self._is_dark = is_dark

        # Exactly one in-flight client at a time; switching tabs / cancel
        # tears it down so we don't accumulate orphan subscriptions.
        self._client: Optional[BunkerClient] = None
        # QR countdown UI plumbing.
        self._qr_countdown_timer: Optional[QTimer] = None
        self._qr_seconds_left: int = 0

        self._build_ui()
        self._apply_theme()
        # Auto-start the QR listener whenever the QR tab is the active one.
        self._tabs.currentChanged.connect(self._on_tab_changed)

    # -- UI build ----------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_paste_tab(), "Paste URI")
        self._tabs.addTab(self._build_qr_tab(), "Scan QR")
        self._tabs.addTab(self._build_manual_tab(), "Manual")
        layout.addWidget(self._tabs, 1)

        # Shared status line + close-only footer. Each tab has its own
        # action button; the dialog footer is just a uniform escape hatch.
        self._status = QLabel("")
        self._status.setObjectName("connect_status")
        self._status.setWordWrap(True)
        self._status.setMinimumHeight(20)
        layout.addWidget(self._status)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._cancel_btn = QPushButton("Close")
        self._cancel_btn.clicked.connect(self._on_cancel)
        footer.addWidget(self._cancel_btn)
        layout.addLayout(footer)

    # -- Paste tab ---------------------------------------------------------

    def _build_paste_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QLabel("Paste a <b>bunker://</b> URI from your signer.")
        header.setWordWrap(True)
        layout.addWidget(header)

        hint = QLabel(
            "Your signer (Amber, nsec.app, nsec.bunker, …) has an option "
            "to generate a bunker URL. Copy it and paste it below. You'll "
            "be asked to approve the connection on the signer side."
        )
        hint.setObjectName("connect_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._paste_edit = QTextEdit()
        self._paste_edit.setPlaceholderText("bunker://<pubkey>?relay=wss://...&secret=...")
        self._paste_edit.setAcceptRichText(False)
        self._paste_edit.setFixedHeight(80)
        self._paste_edit.textChanged.connect(self._update_paste_button)
        layout.addWidget(self._paste_edit)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._paste_btn = QPushButton("Connect")
        self._paste_btn.clicked.connect(self._on_paste_connect)
        button_row.addWidget(self._paste_btn)
        layout.addLayout(button_row)

        self._update_paste_button()
        return tab

    def _update_paste_button(self) -> None:
        text = self._paste_edit.toPlainText().strip()
        self._paste_btn.setEnabled(
            self._client is None and text.startswith("bunker://")
        )

    def _on_paste_connect(self) -> None:
        uri = self._paste_edit.toPlainText().strip()
        try:
            parse_bunker_uri(uri)
        except ValueError as exc:
            self._set_status(f"Invalid URI: {exc}", error=True)
            return
        self._begin_bunker_connect(uri)

    # -- QR tab ------------------------------------------------------------

    def _build_qr_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        header = QLabel("Scan this QR with your signer.")
        header.setWordWrap(True)
        layout.addWidget(header)

        hint = QLabel(
            "Open Amber (or any NIP-46 compatible signer), choose "
            "<b>Add account</b> → <b>Scan QR</b>, and approve the connection."
        )
        hint.setObjectName("connect_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Centered QR pixmap.
        qr_row = QHBoxLayout()
        qr_row.addStretch(1)
        self._qr_label = QLabel()
        self._qr_label.setObjectName("qr_label")
        self._qr_label.setAlignment(Qt.AlignCenter)
        self._qr_label.setMinimumSize(280, 280)
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch(1)
        layout.addLayout(qr_row)

        # Countdown + copy + try-again controls
        self._countdown_label = QLabel("")
        self._countdown_label.setObjectName("countdown")
        self._countdown_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._countdown_label)

        self._qr_uri_field = QLineEdit()
        self._qr_uri_field.setReadOnly(True)
        self._qr_uri_field.setPlaceholderText("nostrconnect://…")
        layout.addWidget(self._qr_uri_field)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._copy_btn = QPushButton("Copy URI")
        self._copy_btn.clicked.connect(self._copy_qr_uri)
        button_row.addWidget(self._copy_btn)
        self._regen_btn = QPushButton("Try Again")
        self._regen_btn.clicked.connect(self._start_qr_listener)
        button_row.addWidget(self._regen_btn)
        layout.addLayout(button_row)

        return tab

    def _start_qr_listener(self) -> None:
        """Mint a fresh keypair + secret, render the QR, begin listening."""
        self._teardown_client(reason="restarting QR")
        self._set_status("")

        secret = secrets.token_hex(8)
        client = BunkerClient(self._pool, parent=self)
        local_pk = client.listen_for_nostrconnect(
            relays=list(_NOSTRCONNECT_RELAYS),
            secret=secret,
            on_success=self._on_pair_success,
            on_failure=self._on_pair_failure,
            timeout_ms=_QR_TTL_SECONDS * 1000,
        )
        uri = build_nostrconnect_uri(
            local_pk, list(_NOSTRCONNECT_RELAYS), secret,
        )
        self._client = client
        self._qr_uri_field.setText(uri)

        # Render QR with the same palette as the dialog.
        dark = "#FFFFFF" if self._is_dark else "#000000"
        light = "#1E1E1E" if self._is_dark else "#FFFFFF"
        self._qr_label.setPixmap(make_qr_pixmap(uri, size=280, dark=dark, light=light))

        self._set_status("Waiting for your signer to scan…")
        self._start_qr_countdown()

    def _start_qr_countdown(self) -> None:
        self._qr_seconds_left = _QR_TTL_SECONDS
        self._update_countdown_label()
        if self._qr_countdown_timer is not None:
            self._qr_countdown_timer.stop()
        self._qr_countdown_timer = QTimer(self)
        self._qr_countdown_timer.setInterval(1000)
        self._qr_countdown_timer.timeout.connect(self._tick_countdown)
        self._qr_countdown_timer.start()

    def _tick_countdown(self) -> None:
        self._qr_seconds_left = max(0, self._qr_seconds_left - 1)
        self._update_countdown_label()
        if self._qr_seconds_left == 0 and self._qr_countdown_timer is not None:
            self._qr_countdown_timer.stop()

    def _update_countdown_label(self) -> None:
        if self._qr_seconds_left > 0:
            self._countdown_label.setText(f"Code expires in {self._qr_seconds_left}s")
        else:
            self._countdown_label.setText("Code expired — press Try Again")

    def _copy_qr_uri(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._qr_uri_field.text())

    def _stop_qr_listener(self) -> None:
        if self._qr_countdown_timer is not None:
            self._qr_countdown_timer.stop()
            self._qr_countdown_timer = None

    # -- Manual tab --------------------------------------------------------

    def _build_manual_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        header = QLabel("Enter the signer's pubkey and its relays.")
        header.setWordWrap(True)
        layout.addWidget(header)

        hint = QLabel(
            "Useful when you have a long-lived bunker setup whose pieces "
            "you keep separately rather than as one URL."
        )
        hint.setObjectName("connect_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(QLabel("Bunker pubkey (hex):"))
        self._manual_pk = QLineEdit()
        self._manual_pk.setPlaceholderText("64 hex chars")
        self._manual_pk.textChanged.connect(self._update_manual_button)
        layout.addWidget(self._manual_pk)

        layout.addWidget(QLabel("Relays (one per line):"))
        self._manual_relays = QTextEdit()
        self._manual_relays.setPlaceholderText(
            "wss://relay.example\nwss://another.example"
        )
        self._manual_relays.setAcceptRichText(False)
        self._manual_relays.setFixedHeight(80)
        self._manual_relays.textChanged.connect(self._update_manual_button)
        layout.addWidget(self._manual_relays)

        layout.addWidget(QLabel("Secret (optional):"))
        self._manual_secret = QLineEdit()
        self._manual_secret.setPlaceholderText("Pairing token if your signer gave you one")
        layout.addWidget(self._manual_secret)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._manual_btn = QPushButton("Connect")
        self._manual_btn.clicked.connect(self._on_manual_connect)
        button_row.addWidget(self._manual_btn)
        layout.addLayout(button_row)

        self._update_manual_button()
        return tab

    def _update_manual_button(self) -> None:
        pk = self._manual_pk.text().strip().lower()
        relays = [r for r in self._manual_relays.toPlainText().splitlines() if r.strip()]
        valid_pk = len(pk) == 64 and all(c in "0123456789abcdef" for c in pk)
        self._manual_btn.setEnabled(
            self._client is None and valid_pk and bool(relays)
        )

    def _on_manual_connect(self) -> None:
        from urllib.parse import quote
        pk = self._manual_pk.text().strip().lower()
        relays = [r.strip() for r in self._manual_relays.toPlainText().splitlines() if r.strip()]
        secret = self._manual_secret.text().strip()
        # Synthesise a bunker:// URI and route through the same code path
        # as the Paste tab — keeps the parsing/validation in one place.
        parts = [f"relay={quote(r, safe='')}" for r in relays]
        if secret:
            parts.append(f"secret={quote(secret, safe='')}")
        uri = f"bunker://{pk}?" + "&".join(parts)
        try:
            parse_bunker_uri(uri)
        except ValueError as exc:
            self._set_status(f"Invalid input: {exc}", error=True)
            return
        self._begin_bunker_connect(uri)

    # -- shared connect flow -----------------------------------------------

    def _begin_bunker_connect(self, uri: str) -> None:
        self._teardown_client(reason="starting new attempt")
        self._client = BunkerClient(self._pool, parent=self)
        self._set_status("Contacting signer. Approve the request on your phone…")
        self._refresh_action_buttons()
        self._client.connect_to_bunker(
            uri,
            on_success=self._on_pair_success,
            on_failure=self._on_pair_failure,
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
        self._store.upsert(profile)
        self._set_status(f"Connected as {profile.npub_short()}.")
        self._stop_qr_listener()
        self.profile_connected.emit(profile)
        self.accept()

    def _on_pair_failure(self, reason: str) -> None:
        self._set_status(f"Connect failed: {reason}", error=True)
        self._teardown_client(reason=reason)
        self._refresh_action_buttons()

    # -- bookkeeping -------------------------------------------------------

    def _teardown_client(self, *, reason: str) -> None:
        if self._client is not None:
            self._client.close(reason=reason)
            self._client = None

    def _refresh_action_buttons(self) -> None:
        self._update_paste_button()
        self._update_manual_button()

    def _on_tab_changed(self, index: int) -> None:
        # Tabs: 0 = Paste, 1 = QR, 2 = Manual.
        self._teardown_client(reason="switched tab")
        self._stop_qr_listener()
        self._set_status("")
        self._refresh_action_buttons()
        if index == 1:
            # Entering the QR tab — kick off the listener right away.
            self._start_qr_listener()

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        if error:
            color = "#FF6B6B" if self._is_dark else "#C0392B"
        else:
            color = "#FFB347" if self._is_dark else "#A05000"
        self._status.setStyleSheet(f"color: {color};")

    def _apply_theme(self) -> None:
        self.setStyleSheet(_DARK_CSS if self._is_dark else _LIGHT_CSS)

    # -- cancel / close ----------------------------------------------------

    def _on_cancel(self) -> None:
        self.reject()

    def reject(self) -> None:  # type: ignore[override]
        self._teardown_client(reason="dialog dismissed")
        self._stop_qr_listener()
        super().reject()
