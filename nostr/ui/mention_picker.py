# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Floating popup for picking a person to mention.

Usage from a parent widget (e.g. the publish dialog):

    picker = MentionPicker(known_people, search_client, parent=self)
    picker.picked.connect(self._on_mention_picked)
    picker.open_at(button.mapToGlobal(button.rect().bottomLeft()))

The popup auto-dismisses on click-outside (Qt.Popup window flag).
Keyboard navigation: ↑/↓ to move, Enter to pick, Esc to cancel.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..avatar_store import AvatarStore
from ..known_people import KnownPeople, Person
from ..search import Nip50SearchClient
from .avatar import make_avatar_pixmap_from_image, pixmap_for_profile


# How long after the last keystroke we wait before issuing a search.
_DEBOUNCE_MS: int = 220

# If local matches are at or above this count we don't bother hitting
# NIP-50 — the user almost certainly meant someone they already follow.
_LOCAL_HIT_THRESHOLD: int = 5

# Cap on rendered rows.
_MAX_VISIBLE_RESULTS: int = 12


# --------------------------------------------------------------------------- #
# Styles                                                                       #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QWidget#mention_picker {
    background: #252526;
    border: 1px solid #3C3C3C;
    border-radius: 6px;
}
QLineEdit#mention_search {
    background: #1E1E1E;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #264F78;
}
QListWidget#mention_results {
    background: #252526;
    color: #D4D4D4;
    border: none;
    outline: none;
}
QListWidget#mention_results::item { padding: 0; border: none; }
QListWidget#mention_results::item:selected { background: #1E1E1E; }
QLabel#row_name { color: #D4D4D4; font-size: 12px; font-weight: 600; }
QLabel#row_nip05 { color: #858585; font-size: 11px; }
QLabel#row_badge { color: #FFB347; font-size: 10px; }
QLabel#picker_hint { color: #858585; font-size: 11px; padding: 6px 10px; }
"""

_LIGHT_CSS = """
QWidget#mention_picker {
    background: #F8F8F8;
    border: 1px solid #E1E1E1;
    border-radius: 6px;
}
QLineEdit#mention_search {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #0078D4;
}
QListWidget#mention_results {
    background: #F8F8F8;
    color: #333333;
    border: none;
    outline: none;
}
QListWidget#mention_results::item { padding: 0; border: none; }
QListWidget#mention_results::item:selected { background: #ECECEC; }
QLabel#row_name { color: #333333; font-size: 12px; font-weight: 600; }
QLabel#row_nip05 { color: #777777; font-size: 11px; }
QLabel#row_badge { color: #A05000; font-size: 10px; }
QLabel#picker_hint { color: #999999; font-size: 11px; padding: 6px 10px; }
"""


# --------------------------------------------------------------------------- #
# Result row                                                                  #
# --------------------------------------------------------------------------- #

class _ResultRow(QWidget):
    """One row in the picker: avatar + name + nip05 + provenance badge."""

    AVATAR_PX = 28

    def __init__(
        self,
        person: Person,
        *,
        avatar_image: Optional[QPixmap] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(10)

        avatar_pix = (
            make_avatar_pixmap_from_image(avatar_image, size=self.AVATAR_PX)
            if avatar_image is not None and not avatar_image.isNull()
            else pixmap_for_profile(person.display_name, person.pubkey, None, size=self.AVATAR_PX)
        )
        avatar_label = QLabel()
        avatar_label.setPixmap(avatar_pix)
        avatar_label.setFixedSize(self.AVATAR_PX, self.AVATAR_PX)
        layout.addWidget(avatar_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        name_text = person.display_name or _short_pk(person.pubkey)
        name_label = QLabel(name_text)
        name_label.setObjectName("row_name")
        text_col.addWidget(name_label)
        if person.nip05:
            nip05_label = QLabel(person.nip05)
            nip05_label.setObjectName("row_nip05")
            text_col.addWidget(nip05_label)
        layout.addLayout(text_col, 1)

        if person.source == "contact":
            badge = QLabel("you follow")
            badge.setObjectName("row_badge")
            layout.addWidget(badge)


def _short_pk(pubkey_hex: str) -> str:
    return f"{pubkey_hex[:10]}…{pubkey_hex[-4:]}"


# --------------------------------------------------------------------------- #
# Search-aware QLineEdit (forwards nav keys to the list)                       #
# --------------------------------------------------------------------------- #

class _SearchEdit(QLineEdit):
    """LineEdit that re-targets ↑/↓/Enter to a list widget while still
    accepting normal typing input."""

    def __init__(self, list_widget: QListWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._list = list_widget

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Return, Qt.Key_Enter):
            QApplication.sendEvent(self._list, event)
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- #
# MentionPicker                                                               #
# --------------------------------------------------------------------------- #

class MentionPicker(QWidget):
    """Click-outside-to-dismiss popup that resolves a Person via search.

    Signals:
      picked(Person)  — user chose a result.  After this signal fires the
                        picker hides itself.
    """

    picked = Signal(object)  # Person

    def __init__(
        self,
        known_people: KnownPeople,
        search_client: Nip50SearchClient,
        avatars: Optional[AvatarStore] = None,
        parent: Optional[QWidget] = None,
        *,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("mention_picker")
        self.setMinimumWidth(360)
        self.setMaximumHeight(380)
        self._known_people = known_people
        self._search_client = search_client
        self._avatars = avatars
        self._is_dark = is_dark
        # Track which pubkeys are currently rendered so we can repaint just
        # this view when a new avatar lands instead of re-rendering blindly.
        self._visible_pks: set[str] = set()
        self._last_people: List[Person] = []

        self._build_ui()
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_search)

        self._search_client.results.connect(self._on_remote_results)
        if self._avatars is not None:
            self._avatars.avatar_added.connect(self._on_avatar_added)

    # -- UI build ----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self._list = QListWidget()
        self._list.setObjectName("mention_results")
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.itemActivated.connect(self._on_activated)
        self._list.itemClicked.connect(self._on_activated)

        self._search_edit = _SearchEdit(self._list, self)
        self._search_edit.setObjectName("mention_search")
        self._search_edit.setPlaceholderText("Search people…")
        self._search_edit.textChanged.connect(self._on_text_changed)

        self._hint = QLabel("")
        self._hint.setObjectName("picker_hint")
        self._hint.setVisible(False)

        outer.addWidget(self._search_edit)
        outer.addWidget(self._list, 1)
        outer.addWidget(self._hint)

    # -- public API --------------------------------------------------------

    def open_at(self, global_pos: QPoint) -> None:
        """Show the popup with its top-left at ``global_pos``."""
        self.move(global_pos)
        self.show()
        self._search_edit.clear()
        self._refresh_for_query("")
        self._search_edit.setFocus()

    def reject(self) -> None:  # type: ignore[override]
        self._search_client.cancel()
        self.hide()

    # -- search pipeline ---------------------------------------------------

    def _on_text_changed(self, _text: str) -> None:
        self._debounce.start()
        # Show immediate local results while debouncing — feels snappier.
        self._refresh_for_query(self._search_edit.text())

    def _run_search(self) -> None:
        query = self._search_edit.text().strip()
        local = self._known_people.search(query, limit=_MAX_VISIBLE_RESULTS)
        self._render(local, searching=False)
        if query and len(local) < _LOCAL_HIT_THRESHOLD:
            self._render(local, searching=True)
            self._search_client.search(query)
        else:
            self._search_client.cancel()

    def _refresh_for_query(self, query: str) -> None:
        local = self._known_people.search(query.strip(), limit=_MAX_VISIBLE_RESULTS)
        self._render(local, searching=False)

    def _on_remote_results(self, query: str, remote: List[Person]) -> None:
        if query != self._search_edit.text().strip():
            return  # stale response
        local = self._known_people.search(query, limit=_MAX_VISIBLE_RESULTS)
        local_pks = {p.pubkey for p in local}
        combined = local + [p for p in remote if p.pubkey not in local_pks]
        self._render(combined[:_MAX_VISIBLE_RESULTS], searching=False)

    # -- rendering ---------------------------------------------------------

    def _render(self, people: List[Person], *, searching: bool) -> None:
        self._list.clear()
        self._last_people = list(people)
        self._visible_pks = {p.pubkey for p in people}
        for person in people:
            item = QListWidgetItem()
            avatar_pix = self._avatars.get(person.pubkey) if self._avatars else None
            row = _ResultRow(
                person,
                avatar_image=avatar_pix,
                parent=self._list,
            )
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.UserRole, person)
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

        if searching:
            self._hint.setText("Searching relay.nostr.band…")
            self._hint.setVisible(True)
        elif not people:
            query = self._search_edit.text().strip()
            self._hint.setText("No matches yet — keep typing" if query else "Type to search people")
            self._hint.setVisible(True)
        else:
            self._hint.setVisible(False)

    # -- activation --------------------------------------------------------

    def _on_activated(self, item: QListWidgetItem) -> None:
        person = item.data(Qt.UserRole)
        if isinstance(person, Person):
            self.picked.emit(person)
            self.hide()

    def _on_avatar_added(self, pubkey_hex: str, _pixmap) -> None:
        """A new avatar landed; if it belongs to one of our currently-rendered
        rows, repaint the list so the initials disc is replaced live."""
        if pubkey_hex in self._visible_pks and self._last_people:
            self._render(self._last_people, searching=False)
