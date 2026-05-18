"""Mention chip-row widget used inside the publish dialogs.

Visual:

    Mentions:  [●AL  Alice  ×]  [●BO  Bob  ×]  [+ add person]

Click "+ add person" → ``MentionPicker`` pops below the button. Picking
a person appends a chip; clicking ``×`` on a chip removes it. The row
exposes ``mentions()`` returning the current ``(pubkey, relay_hint)``
tuples for the publisher.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

from ..avatar_store import AvatarStore
from ..known_people import KnownPeople, Person
from ..publisher import Mention
from ..search import Nip50SearchClient
from .avatar import make_avatar_pixmap_from_image, pixmap_for_profile
from .mention_picker import MentionPicker


_CHIP_AVATAR_PX: int = 18


# --------------------------------------------------------------------------- #
# Styles                                                                       #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QLabel#mentions_label { color: #858585; font-size: 11px; }

QWidget#mention_chip {
    background: #2D2D30;
    border: 1px solid #3C3C3C;
    border-radius: 11px;
}
QLabel#chip_name { color: #D4D4D4; font-size: 11px; padding: 0 4px; }
QPushButton#chip_remove {
    background: transparent;
    border: none;
    color: #858585;
    font-size: 12px;
    padding: 0 6px 0 2px;
}
QPushButton#chip_remove:hover { color: #FFFFFF; }

QToolButton#add_mention {
    background: transparent;
    border: 1px dashed #3C3C3C;
    border-radius: 11px;
    color: #858585;
    font-size: 11px;
    padding: 2px 10px;
}
QToolButton#add_mention:hover { border-color: #FF8C00; color: #FFB347; }
"""

_LIGHT_CSS = """
QLabel#mentions_label { color: #777777; font-size: 11px; }

QWidget#mention_chip {
    background: #ECECEC;
    border: 1px solid #CCCCCC;
    border-radius: 11px;
}
QLabel#chip_name { color: #333333; font-size: 11px; padding: 0 4px; }
QPushButton#chip_remove {
    background: transparent;
    border: none;
    color: #777777;
    font-size: 12px;
    padding: 0 6px 0 2px;
}
QPushButton#chip_remove:hover { color: #000000; }

QToolButton#add_mention {
    background: transparent;
    border: 1px dashed #CCCCCC;
    border-radius: 11px;
    color: #777777;
    font-size: 11px;
    padding: 2px 10px;
}
QToolButton#add_mention:hover { border-color: #E88000; color: #A05000; }
"""


# --------------------------------------------------------------------------- #
# Single chip                                                                  #
# --------------------------------------------------------------------------- #

class _MentionChip(QWidget):
    """One pill: avatar + name + close button."""

    removed = Signal(str)  # pubkey_hex

    def __init__(
        self,
        person: Person,
        avatar_image: Optional[QPixmap] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("mention_chip")
        self._pubkey = person.pubkey
        self._person = person

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(_CHIP_AVATAR_PX, _CHIP_AVATAR_PX)
        self.set_avatar(avatar_image)
        layout.addWidget(self._avatar_label)

        name = person.display_name or f"{person.pubkey[:8]}…"
        name_label = QLabel(name)
        name_label.setObjectName("chip_name")
        layout.addWidget(name_label)

        remove_btn = QPushButton("×")
        remove_btn.setObjectName("chip_remove")
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setFixedSize(QSize(18, 22))
        remove_btn.clicked.connect(lambda: self.removed.emit(self._pubkey))
        layout.addWidget(remove_btn)

    def set_avatar(self, avatar_image: Optional[QPixmap]) -> None:
        """Swap the rendered avatar (initials → real picture when it arrives)."""
        pix = (
            make_avatar_pixmap_from_image(avatar_image, size=_CHIP_AVATAR_PX)
            if avatar_image is not None and not avatar_image.isNull()
            else pixmap_for_profile(
                self._person.display_name, self._person.pubkey, None, size=_CHIP_AVATAR_PX
            )
        )
        self._avatar_label.setPixmap(pix)


# --------------------------------------------------------------------------- #
# Chip row                                                                     #
# --------------------------------------------------------------------------- #

class MentionChipRow(QWidget):
    """Horizontal row of mention chips + "+ add person" trigger.

    Signals:
      mentions_changed(list)  — list[Person] reflecting current selection
    """

    mentions_changed = Signal(list)

    def __init__(
        self,
        known_people: KnownPeople,
        search_client: Nip50SearchClient,
        avatars: Optional[AvatarStore] = None,
        parent: Optional[QWidget] = None,
        *,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self._known_people = known_people
        self._search_client = search_client
        self._avatars = avatars
        self._is_dark = is_dark
        self._picked: list[Person] = []
        self._picker: Optional[MentionPicker] = None

        self._build_ui()
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

        if self._avatars is not None:
            self._avatars.avatar_added.connect(self._on_avatar_added)

    # -- UI build ----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        label = QLabel("Mentions")
        label.setObjectName("mentions_label")
        outer.addWidget(label)

        self._add_btn = QToolButton()
        self._add_btn.setObjectName("add_mention")
        self._add_btn.setText("+ add person")
        self._add_btn.setCursor(Qt.PointingHandCursor)
        self._add_btn.clicked.connect(self._open_picker)

        # Chips go between the label and the add button; we keep track of
        # the layout so we can insert and remove chip widgets.
        self._outer = outer
        outer.addWidget(self._add_btn)
        outer.addStretch(1)

    # -- public API --------------------------------------------------------

    def mentions(self) -> List[Mention]:
        """Return the (pubkey, relay_hint) tuples for the publisher."""
        return [(p.pubkey, p.relay_hint) for p in self._picked]

    def people(self) -> List[Person]:
        return list(self._picked)

    def clear(self) -> None:
        for person in list(self._picked):
            self._remove_chip(person.pubkey, emit=False)
        self.mentions_changed.emit(self.people())

    # -- internals ---------------------------------------------------------

    def _open_picker(self) -> None:
        # Build the picker lazily — first open spawns the widget; subsequent
        # opens reuse it so its event filters / debounce timer survive.
        if self._picker is None:
            self._picker = MentionPicker(
                self._known_people,
                self._search_client,
                avatars=self._avatars,
                parent=self.window(),
                is_dark=self._is_dark,
            )
            self._picker.picked.connect(self._on_picked)
        global_pos = self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft())
        self._picker.open_at(global_pos)

    def _on_picked(self, person: Person) -> None:
        if any(p.pubkey == person.pubkey for p in self._picked):
            return  # already in the row
        self._picked.append(person)
        avatar = self._avatars.get(person.pubkey) if self._avatars else None
        chip = _MentionChip(person, avatar_image=avatar, parent=self)
        chip.removed.connect(self._remove_chip)
        # Insert just before the add button so chips stay left-aligned.
        # outer layout items: [label, chips..., add_btn, stretch]
        insert_index = self._outer.indexOf(self._add_btn)
        self._outer.insertWidget(insert_index, chip)
        self.mentions_changed.emit(self.people())

    def _on_avatar_added(self, pubkey_hex: str, pixmap: QPixmap) -> None:
        """Live-refresh the matching chip when its avatar lands."""
        for i in range(self._outer.count()):
            item = self._outer.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, _MentionChip) and widget._pubkey == pubkey_hex:
                widget.set_avatar(pixmap)
                return

    def _remove_chip(self, pubkey_hex: str, *, emit: bool = True) -> None:
        # Drop from internal list
        self._picked = [p for p in self._picked if p.pubkey != pubkey_hex]
        # Drop the chip widget — it's the only _MentionChip with this pubkey
        for i in range(self._outer.count()):
            item = self._outer.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, _MentionChip) and widget._pubkey == pubkey_hex:
                widget.setParent(None)
                widget.deleteLater()
                break
        if emit:
            self.mentions_changed.emit(self.people())
