# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Modal dialog for publishing a kind 1 short note.

Lifecycle:
  - Opens with the editor's plain-text content already populated.
  - User can edit the text, see a live character count, and switch the
    signing profile inline before pressing Publish.
  - Publish kicks off a NotePublishJob; the dialog shows live status until
    completion. On success the dialog closes and the result lands in the
    main window's status bar.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
)

from ..avatar_store import AvatarStore
from ..bunker import BunkerSessionPool
from ..known_people import KnownPeople
from ..outbox import RelayListCache
from ..profiles import Profile, ProfileStore
from ..publisher import PublishJob, PublishResult, build_note
from ..relay import RelayPool
from ..search import Nip50SearchClient
from .avatar import (
    AVATAR_SIZE,
    CHIP_TOTAL_WIDTH,
    compose_chip_icon,
    pixmap_for_profile,
)
from .mention_chips import MentionChipRow


# Length above which we softly suggest using long-form (kind 30023) instead.
_LONGFORM_HINT_CHARS = 1_000


# --------------------------------------------------------------------------- #
# Styles — pulled from the editor's existing palette                          #
# --------------------------------------------------------------------------- #

_DARK_DIALOG_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#publish_hint { color: #858585; }
QLabel#publish_status { color: #FFB347; }
QLabel#publish_count { color: #858585; font-size: 11px; }
QTextEdit {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 8px;
    font-family: "Menlo", "Consolas", "Noto Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: #264F78;
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
QToolButton#profile_switch {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px 6px;
    color: #CCCCCC;
}
QToolButton#profile_switch:hover { background: #3C3C3C; }
QToolButton#profile_switch::menu-indicator { image: none; width: 0; }
"""

_LIGHT_DIALOG_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#publish_hint { color: #777777; }
QLabel#publish_status { color: #A05000; }
QLabel#publish_count { color: #999999; font-size: 11px; }
QTextEdit {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 8px;
    font-family: "Menlo", "Consolas", "Noto Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: #0078D4;
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
QToolButton#profile_switch {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px 6px;
    color: #555555;
}
QToolButton#profile_switch:hover { background: #E1E1E1; }
QToolButton#profile_switch::menu-indicator { image: none; width: 0; }
"""

# Match the styled QMenu used by the chip so the in-dialog switch looks native.
_DARK_MENU_CSS = """
QMenu { background: #252526; color: #CCCCCC; border: 1px solid #3C3C3C; padding: 4px; }
QMenu::item { padding: 4px 20px 4px 30px; }
QMenu::item:selected { background: #1E1E1E; color: #FFFFFF; }
QMenu::separator { height: 1px; background: #3C3C3C; margin: 4px 0px; }
"""

_LIGHT_MENU_CSS = """
QMenu { background: #F8F8F8; color: #333333; border: 1px solid #E1E1E1; padding: 4px; }
QMenu::item { padding: 4px 20px 4px 30px; }
QMenu::item:selected { background: #F3F3F3; color: #000000; }
QMenu::separator { height: 1px; background: #E1E1E1; margin: 4px 0px; }
"""


# --------------------------------------------------------------------------- #
# Dialog                                                                       #
# --------------------------------------------------------------------------- #

class PublishNoteDialog(QDialog):
    """Compose, switch profile, publish a kind 1 note.

    Emits ``published(results)`` after a successful publish (results = list
    of ``(url, ok, message)`` tuples). The dialog closes itself on success.
    On failure the status line shows the reason and the user can retry.
    """

    # Args: (signed_event_id_hex, list[PublishResult])
    published = Signal(str, list)

    def __init__(
        self,
        *,
        content: str,
        active_profile: Profile,
        store: ProfileStore,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        known_people: KnownPeople,
        search_client: Nip50SearchClient,
        avatars: AvatarStore,
        parent=None,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish as Note")
        self.setModal(True)
        self.setMinimumSize(560, 380)

        self._store = store
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._known_people = known_people
        self._search_client = search_client
        self._avatars = avatars
        self._is_dark = is_dark
        self._current_profile = active_profile
        self._job: Optional[PublishJob] = None
        self._signed_event_id: Optional[str] = None

        self._build_ui(initial_content=content)
        self._apply_theme()
        self._refresh_profile_chip()
        self._refresh_char_count()

        # When the active profile's avatar lands after the dialog opens,
        # repaint the "Publishing as ●…" switcher so it stops showing initials.
        self._avatars.avatar_added.connect(self._on_avatar_added)

    def _on_avatar_added(self, pubkey_hex: str, _pixmap) -> None:
        if pubkey_hex == self._current_profile.user_pubkey:
            self._refresh_profile_chip()

    # -- UI ----------------------------------------------------------------

    def _build_ui(self, *, initial_content: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        header = QLabel("Publish a short note (kind 1) to Nostr.")
        layout.addWidget(header)

        hint = QLabel(
            "Formatting is stripped. Short notes are plain text. "
            "For richer documents, use Publish as Article."
        )
        hint.setObjectName("publish_hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._text_edit = QTextEdit()
        self._text_edit.setAcceptRichText(False)
        self._text_edit.setPlainText(initial_content)
        self._text_edit.textChanged.connect(self._refresh_char_count)
        layout.addWidget(self._text_edit, 1)

        self._char_count = QLabel("")
        self._char_count.setObjectName("publish_count")
        layout.addWidget(self._char_count)

        # Mentions chip row — picks become ["p", hex, relay-hint] tags + URI
        # lines appended to the body on publish.
        self._mention_row = MentionChipRow(
            self._known_people,
            self._search_client,
            avatars=self._avatars,
            parent=self,
            is_dark=self._is_dark,
        )
        layout.addWidget(self._mention_row)

        # Bottom row: "Publishing as [chip] · switch ▾"  +  Cancel / Publish.
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 4, 0, 0)
        bottom.setSpacing(6)

        publishing_as = QLabel("Publishing as")
        bottom.addWidget(publishing_as)

        self._profile_switch = QToolButton()
        self._profile_switch.setObjectName("profile_switch")
        self._profile_switch.setPopupMode(QToolButton.InstantPopup)
        self._profile_switch.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._profile_switch.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        self._profile_switch.setCursor(Qt.PointingHandCursor)
        self._switch_menu = QMenu(self._profile_switch)
        self._profile_switch.setMenu(self._switch_menu)
        bottom.addWidget(self._profile_switch)
        bottom.addStretch(1)

        self._status = QLabel("")
        self._status.setObjectName("publish_status")
        self._status.setWordWrap(True)
        bottom.addWidget(self._status, 2)

        buttons = QDialogButtonBox()
        self._cancel_btn = buttons.addButton(QDialogButtonBox.Cancel)
        self._publish_btn = buttons.addButton("Publish", QDialogButtonBox.AcceptRole)
        self._publish_btn.setDefault(True)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._publish_btn.clicked.connect(self._on_publish)
        bottom.addWidget(buttons)

        layout.addLayout(bottom)

    def _apply_theme(self) -> None:
        self.setStyleSheet(_DARK_DIALOG_CSS if self._is_dark else _LIGHT_DIALOG_CSS)
        self._switch_menu.setStyleSheet(
            _DARK_MENU_CSS if self._is_dark else _LIGHT_MENU_CSS
        )

    # -- profile switcher --------------------------------------------------

    def _refresh_profile_chip(self) -> None:
        profile = self._current_profile
        avatar_pix = self._avatars.get(profile.user_pubkey)
        avatar = pixmap_for_profile(
            profile.display_name, profile.user_pubkey, avatar_pix, size=AVATAR_SIZE
        )
        self._profile_switch.setIcon(compose_chip_icon(avatar, self._chevron_color()))
        # Force the icon onto a fixed footprint so width never twitches as
        # the user switches between profiles with very different names.
        from PySide6.QtCore import QSize
        self._profile_switch.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        label = profile.display_name or profile.npub_short()
        self._profile_switch.setText(f"  {label}")
        self._profile_switch.setToolTip(profile.npub_short())

        self._rebuild_switch_menu()

    def _chevron_color(self) -> QColor:
        return QColor("#CCCCCC") if self._is_dark else QColor("#555555")

    def _rebuild_switch_menu(self) -> None:
        menu = self._switch_menu
        menu.clear()
        profiles = self._store.list()
        if not profiles:
            act = menu.addAction("(no profiles)")
            act.setEnabled(False)
            return
        for profile in profiles:
            label = profile.display_name or profile.npub_short()
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(profile.user_pubkey == self._current_profile.user_pubkey)
            act.triggered.connect(
                lambda _checked=False, p=profile: self._on_profile_switched(p)
            )

    def _on_profile_switched(self, profile: Profile) -> None:
        if profile.user_pubkey == self._current_profile.user_pubkey:
            return
        self._current_profile = profile
        # Keep the main chip in sync so the user's "default" reflects their
        # latest publish choice.
        self._store.set_default(profile.user_pubkey)
        self._refresh_profile_chip()

    # -- content / status --------------------------------------------------

    def _content(self) -> str:
        return self._text_edit.toPlainText().strip()

    def _refresh_char_count(self) -> None:
        n = len(self._content())
        msg = f"{n} characters"
        if n >= _LONGFORM_HINT_CHARS:
            msg += " · long note, consider Publish as Article instead"
        self._char_count.setText(msg)
        self._publish_btn.setEnabled(n > 0 and self._job is None)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        if error:
            color = "#FF6B6B" if self._is_dark else "#C0392B"
        else:
            color = "#FFB347" if self._is_dark else "#A05000"
        self._status.setStyleSheet(f"color: {color};")

    def _set_busy(self, busy: bool) -> None:
        self._text_edit.setReadOnly(busy)
        self._profile_switch.setEnabled(not busy)
        self._publish_btn.setEnabled(not busy and bool(self._content()))

    # -- publish flow ------------------------------------------------------

    def _on_publish(self) -> None:
        content = self._content()
        if not content:
            return
        if self._job is not None:
            return  # already running

        self._set_busy(True)
        self._set_status("")

        unsigned = build_note(
            content,
            self._current_profile.user_pubkey,
            mentions=self._mention_row.mentions(),
        )
        self._job = PublishJob(
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            profile=self._current_profile,
            unsigned_event=unsigned,
            parent=self,
        )
        self._job.status_changed.connect(self._set_status)
        self._job.signed.connect(self._on_signed)
        self._job.completed.connect(self._on_completed)
        self._job.failed.connect(self._on_failed)
        self._job.start()

    def _on_signed(self, event_id_hex: str) -> None:
        self._signed_event_id = event_id_hex

    def _on_completed(self, results: List[PublishResult]) -> None:
        self._job = None
        accepted = sum(1 for _, ok, _ in results if ok)
        if accepted == 0:
            self._set_status("No relay accepted the note. See log for details.", error=True)
            self._set_busy(False)
            return
        self.published.emit(self._signed_event_id or "", results)
        self.accept()

    def _on_failed(self, reason: str) -> None:
        self._job = None
        self._set_status(f"Publish failed: {reason}", error=True)
        self._set_busy(False)

    def _on_cancel(self) -> None:
        # Note: an in-flight signer request can't be revoked once sent. We
        # just stop reacting to it and let the user dismiss the dialog.
        if self._job is not None:
            self._job = None
        self.reject()
