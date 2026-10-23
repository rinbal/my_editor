# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Floating popup for picking a person to mention.

Usage from a parent widget (e.g. a publish sheet):

    picker = MentionPicker(known_people, search_client, parent=self)
    picker.picked.connect(self._on_mention_picked)
    picker.open_at(button.rect(), button)

The popup auto-dismisses on click-outside (Qt.Popup window flag).
Keyboard navigation: ↑/↓ to move, Enter to pick, Esc to cancel.

A person's Nostr address shows under their name only once their domain
has confirmed it (``address_check``, nostr/nip05_check.py): anyone can
write any address into a profile. Searching still finds people by what
their address says.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QRect, Qt, QTimer, Signal
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

import announce
import theme
from . import appearance
from i18n import _
from popup_placement import place_below

from .. import refs
from ..avatar_store import AvatarStore
from ..known_people import KnownPeople, Person
from ..nip05_check import Nip05Check
from ..search import Nip50SearchClient
from .avatar import make_avatar_pixmap_from_image, pixmap_for_profile


# How long after the last keystroke we wait before issuing a search.
_DEBOUNCE_MS: int = 220

# If local matches are at or above this count we don't bother hitting
# NIP-50: the user almost certainly meant someone they already follow.
_LOCAL_HIT_THRESHOLD: int = 5

# Cap on rendered rows.
_MAX_VISIBLE_RESULTS: int = 12


# --------------------------------------------------------------------------- #
# Styles                                                                       #
# --------------------------------------------------------------------------- #

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
        address: str = "",
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
        if address:
            nip05_label = QLabel(address)
            nip05_label.setObjectName("row_nip05")
            text_col.addWidget(nip05_label)
        layout.addLayout(text_col, 1)

        if person.source == "contact":
            badge = QLabel(_("Following"))
            badge.setObjectName("row_badge")
            layout.addWidget(badge)


def _style(is_dark: bool) -> str:
    r = appearance.roles(is_dark)
    return appearance.controls(is_dark) + f"""
    QWidget#mention_picker {{ background: {r.popover}; border: 1px solid {r.edge}; border-radius: 8px; }}
    QListWidget#mention_results {{ background: transparent; border: none; outline: none; }}
    QListWidget#mention_results::item {{ padding: 0; border: none; }}
    QListWidget#mention_results::item:selected {{ background: {r.soft}; border-radius: 6px; }}
    QLabel#row_name {{ color: {r.text}; font-weight: 600; }}
    QLabel#row_nip05, QLabel#picker_hint {{ color: {r.muted}; }}
    QLabel#row_badge {{ color: {r.ink}; }}
    QLabel#picker_problem {{ color: {theme.error_text_color(is_dark)}; padding: 0 2px; }}
    """


def _short_pk(pubkey_hex: str) -> str:
    return f"{pubkey_hex[:10]}…{pubkey_hex[-4:]}"


# --------------------------------------------------------------------------- #
# Search-aware QLineEdit (forwards nav keys to the list)                       #
# --------------------------------------------------------------------------- #

class _SearchEdit(QLineEdit):
    """LineEdit that moves through a list widget with ↑/↓ and picks its
    current row with Return or Enter, while still taking typing."""

    def __init__(self, list_widget: QListWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._list = list_widget

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            QApplication.sendEvent(self._list, event)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            # Picked here, as Mail's address completion does: a list view
            # activates on Return only on some systems (on a Mac it means
            # "edit the row").
            item = self._list.currentItem()
            if item is not None:
                self._list.itemActivated.emit(item)
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- #
# MentionPicker                                                               #
# --------------------------------------------------------------------------- #

class MentionPicker(QWidget):
    """Click-outside-to-dismiss popup that resolves a Person via search.

    Signals:
      picked(Person)  user chose a result.  After this signal fires the
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
        address_check: Optional[Nip05Check] = None,
    ) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("mention_picker")
        self.setAccessibleName(_("Mention"))
        # Confirms the Nostr addresses shown; without one, none is shown.
        self._address_check = address_check
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
        appearance.bind(self, "set_dark")
        self.setStyleSheet(_style(is_dark))
        appearance.surface(self, is_dark, kind="popover")

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_search)

        self._search_client.results.connect(self._on_remote_results)
        if self._avatars is not None:
            self._avatars.avatar_added.connect(self._on_avatar_added)
        if self._address_check is not None:
            self._address_check.checked.connect(self._on_address_checked)

    # -- UI build ----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self._list = QListWidget()
        self._list.setObjectName("mention_results")
        self._list.setAccessibleName(_("People"))
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.itemActivated.connect(self._on_activated)
        self._list.itemClicked.connect(self._on_activated)

        self._search_edit = _SearchEdit(self._list, self)
        self._search_edit.setObjectName("mention_search")
        self._search_edit.setPlaceholderText(_("Search people…"))
        self._search_edit.setAccessibleName(_("Search People"))
        self._search_edit.textChanged.connect(self._on_text_changed)
        # What can't be searched for, under the field.
        self._problem = QLabel("")
        self._problem.setObjectName("picker_problem")
        self._problem.setWordWrap(True)
        self._problem.hide()

        self._hint = QLabel("")
        self._hint.setObjectName("picker_hint")
        self._hint.setVisible(False)

        outer.addWidget(self._search_edit)
        outer.addWidget(self._problem)
        outer.addWidget(self._list, 1)
        outer.addWidget(self._hint)

    # -- public API --------------------------------------------------------

    def open_at(self, anchor: QRect, parent: QWidget) -> None:
        """Show the popup under ``anchor`` (in ``parent``'s coordinates: the
        caret, or the button that asked), or above it when the screen has
        no room below, as every popup at the caret does."""
        self._search_edit.clear()
        self._refresh_for_query("")
        place_below(self, anchor, parent)
        self.show()
        self._search_edit.setFocus()

    def set_dark(self, is_dark: bool) -> None:
        """Follow the window's appearance (light or dark)."""
        self._is_dark = is_dark
        appearance.bind(self, "set_dark")
        self.setStyleSheet(_style(is_dark))
        appearance.surface(self, is_dark, kind="popover")

    def reject(self) -> None:  # type: ignore[override]
        self._search_client.cancel()
        self.hide()

    # -- search pipeline ---------------------------------------------------

    def _on_text_changed(self, text: str) -> None:
        # A secret key pasted here (where the public key was meant) is never
        # sent to a search relay (Nip50SearchClient.search); this says why.
        announce.field_problem(self._search_edit, self._problem, _(
            "This is a secret key, so it isn’t searched for. Search for the person’s "
            "name or public key instead.") if refs.secret_keys_in(text) else "")
        self._debounce.start()
        # Show immediate local results while debouncing; feels snappier.
        self._refresh_for_query(self._search_edit.text())

    def _run_search(self) -> None:
        query = self._search_edit.text().strip()
        local = self._known_people.search(query, limit=_MAX_VISIBLE_RESULTS)
        self._render(local, searching=False)
        if query and len(local) < _LOCAL_HIT_THRESHOLD and not refs.secret_keys_in(query):
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

    def confirmed_address(self, person: Person) -> str:
        """The person's Nostr address when their domain confirmed it, else
        "" (asking the domain, the first time)."""
        if not person.nip05 or self._address_check is None:
            return ""
        return person.nip05 if self._address_check.confirmed(
            person.pubkey, person.nip05) is True else ""

    def _render(self, people: List[Person], *, searching: bool) -> None:
        self._list.clear()
        self._last_people = list(people)
        self._visible_pks = {p.pubkey for p in people}
        for person in people:
            item = QListWidgetItem()
            avatar_pix = self._avatars.get(person.pubkey) if self._avatars else None
            address = self.confirmed_address(person)
            row = _ResultRow(
                person,
                avatar_image=avatar_pix,
                address=address,
                parent=self._list,
            )
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.UserRole, person)
            # What a screen reader says for the row: the name, the address
            # when confirmed, and "Following".
            spoken = [person.display_name or _short_pk(person.pubkey)]
            if address:
                spoken.append(address)
            if person.source == "contact":
                spoken.append(_("Following"))
            item.setData(Qt.AccessibleTextRole, ", ".join(spoken))
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

        if searching:
            self._hint.setText(_("Searching Nostr…"))
            self._hint.setVisible(True)
        elif not self._problem.isHidden():
            # Why it isn't searched is said under the field; nothing more
            # to wait or type for.
            self._hint.setVisible(False)
        elif not people:
            query = self._search_edit.text().strip()
            self._hint.setText(_("No matches yet. Keep typing") if query
                               else _("Type to search people"))
            self._hint.setVisible(True)
        else:
            self._hint.setVisible(False)

    # -- activation --------------------------------------------------------

    def _on_activated(self, item: QListWidgetItem) -> None:
        person = item.data(Qt.UserRole)
        if isinstance(person, Person):
            self.picked.emit(person)
            self.hide()

    def _on_address_checked(self, pubkey_hex: str, _address: str) -> None:
        """A domain answered: show the address now if it was confirmed."""
        if pubkey_hex in self._visible_pks and self._last_people and self.isVisible():
            self._render(self._last_people, searching=False)

    def _on_avatar_added(self, pubkey_hex: str, _pixmap) -> None:
        """A new avatar landed; if it belongs to one of our currently-rendered
        rows, repaint the list so the initials disc is replaced live."""
        if pubkey_hex in self._visible_pks and self._last_people:
            self._render(self._last_people, searching=False)
