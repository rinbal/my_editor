"""Choose where to save the current tab: local file or Nostr draft.

Triggered by ``Ctrl+Shift+S`` whenever a Nostr profile is connected.
When *no* profile is connected the editor falls back to its existing
"Save As" disk dialog directly — this dialog never appears, which is
the dormancy rule the rest of the Nostr feature obeys.

UX:
  - Two large radio-style cards. The first is the "safe" choice
    (save to disk); it's pre-selected so a thoughtless ⌘S→Enter still
    saves locally.
  - A "Remember this choice for this tab" checkbox lets users skip
    the dialog on subsequent ⌘⇧S presses in the same tab.
  - One-line hint under each option explains the trade-off so first-
    time users can decide without leaving the dialog.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


class SaveDestination(Enum):
    """Where the current tab's contents should land."""

    LOCAL = "local"
    NOSTR_DRAFT = "nostr_draft"


# --------------------------------------------------------------------------- #
# Stylesheets — paired with the existing dialog palette                       #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#save_dest_title { color: #FFFFFF; font-size: 16px; font-weight: 600; }
QLabel#save_dest_subtitle { color: #858585; font-size: 12px; }
QLabel#save_dest_hint { color: #999999; font-size: 11px; }

QFrame#save_dest_card {
    background: #252526;
    border: 1px solid #3C3C3C;
    border-radius: 6px;
}
QFrame#save_dest_card[selected="true"] {
    background: #2A2D2E;
    border: 1px solid #007ACC;
}

QRadioButton {
    color: #D4D4D4;
    spacing: 8px;
    background: transparent;
    border: none;
    padding: 0;
}
QRadioButton::indicator { width: 14px; height: 14px; }
QLabel#save_dest_option { color: #FFFFFF; font-size: 13px; font-weight: 500; }

QCheckBox { color: #B5B5B5; font-size: 11px; spacing: 6px; }

QPushButton {
    background: #2D2D30;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    padding: 6px 14px;
    border-radius: 4px;
    min-width: 86px;
}
QPushButton:hover { background: #3C3C3C; }
QPushButton:pressed { background: #1E1E1E; }
QPushButton:default { background: #007ACC; color: #FFFFFF; border-color: #1177C7; }
QPushButton:default:hover { background: #1177C7; }
"""

_LIGHT_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#save_dest_title { color: #1A1A1A; font-size: 16px; font-weight: 600; }
QLabel#save_dest_subtitle { color: #777777; font-size: 12px; }
QLabel#save_dest_hint { color: #777777; font-size: 11px; }

QFrame#save_dest_card {
    background: #F8F8F8;
    border: 1px solid #E1E1E1;
    border-radius: 6px;
}
QFrame#save_dest_card[selected="true"] {
    background: #EAF3FB;
    border: 1px solid #0078D4;
}

QRadioButton {
    color: #333333;
    spacing: 8px;
    background: transparent;
    border: none;
    padding: 0;
}
QRadioButton::indicator { width: 14px; height: 14px; }
QLabel#save_dest_option { color: #1A1A1A; font-size: 13px; font-weight: 500; }

QCheckBox { color: #555555; font-size: 11px; spacing: 6px; }

QPushButton {
    background: #ECECEC;
    color: #333333;
    border: 1px solid #CCCCCC;
    padding: 6px 14px;
    border-radius: 4px;
    min-width: 86px;
}
QPushButton:hover { background: #E1E1E1; }
QPushButton:pressed { background: #D0D0D0; }
QPushButton:default { background: #0078D4; color: #FFFFFF; border-color: #1066B4; }
QPushButton:default:hover { background: #1066B4; }
"""


# --------------------------------------------------------------------------- #
# Card                                                                        #
# --------------------------------------------------------------------------- #

class _DestinationCard(QFrame):
    """One selectable card representing a save destination.

    Composed as a frame around a radio button + heading + hint. The
    frame is the click target — clicking anywhere on the card selects
    the inner radio. Selection state is exposed via the ``selected``
    QSS property so the stylesheet can paint the chosen card.
    """

    clicked = Signal()

    def __init__(
        self,
        *,
        title: str,
        hint: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("save_dest_card")
        self.setProperty("selected", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setFrameShape(QFrame.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.radio = QRadioButton()
        self.radio.setFocusPolicy(Qt.NoFocus)  # selection follows card click
        head.addWidget(self.radio, 0, Qt.AlignVCenter)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("save_dest_option")
        head.addWidget(self.title_label, 1, Qt.AlignVCenter)
        layout.addLayout(head)

        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("save_dest_hint")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

    def set_selected(self, selected: bool) -> None:
        # Toggle the QSS property and ask Qt to re-evaluate the
        # stylesheet so the card-frame border updates immediately.
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.radio.setChecked(selected)

    def mousePressEvent(self, event):  # noqa: D401 — Qt override
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- #
# Dialog                                                                      #
# --------------------------------------------------------------------------- #

class SaveDestinationDialog(QDialog):
    """Modal: pick a destination for the current tab and (optionally)
    remember the choice for the rest of this tab's lifetime.

    Returns:
      ``self.destination`` — ``SaveDestination`` enum
      ``self.remember``    — bool

    Access these after ``exec()`` returns ``QDialog.Accepted``.
    """

    def __init__(
        self,
        *,
        default: SaveDestination = SaveDestination.LOCAL,
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save")
        self.setModal(True)
        self.setMinimumWidth(440)
        self._is_dark = is_dark
        self._destination: SaveDestination = default
        self._remember: bool = False
        self._build_ui()
        self._apply_theme()
        self._select(default)

    # -- public properties --------------------------------------------------

    @property
    def destination(self) -> SaveDestination:
        return self._destination

    @property
    def remember(self) -> bool:
        return self._remember

    # -- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(14)

        title = QLabel("Where do you want to save?")
        title.setObjectName("save_dest_title")
        root.addWidget(title)

        subtitle = QLabel(
            "Local files live only on this device. Nostr drafts are encrypted "
            "to your key and synced across your other devices."
        )
        subtitle.setObjectName("save_dest_subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # Single button group ensures mutual exclusion across cards.
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._local_card = _DestinationCard(
            title="Save to disk",
            hint="A regular local file. Same as your usual Save As.",
        )
        self._draft_card = _DestinationCard(
            title="Save as private Nostr draft",
            hint=(
                "End-to-end encrypted to your Nostr key. Only you can decrypt it. "
                "Will appear on other devices signed in with the same profile."
            ),
        )
        self._group.addButton(self._local_card.radio, 0)
        self._group.addButton(self._draft_card.radio, 1)

        self._local_card.clicked.connect(lambda: self._select(SaveDestination.LOCAL))
        self._draft_card.clicked.connect(lambda: self._select(SaveDestination.NOSTR_DRAFT))
        self._local_card.radio.toggled.connect(
            lambda on: on and self._select(SaveDestination.LOCAL)
        )
        self._draft_card.radio.toggled.connect(
            lambda on: on and self._select(SaveDestination.NOSTR_DRAFT)
        )

        root.addWidget(self._local_card)
        root.addWidget(self._draft_card)

        self._remember_checkbox = QCheckBox(
            "Remember this choice for this tab"
        )
        self._remember_checkbox.setToolTip(
            "Skip this dialog on the next Ctrl+Shift+S for the current "
            "document. You can change it again by closing and reopening "
            "the tab."
        )
        root.addWidget(self._remember_checkbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        ok_button.setText("Save")
        ok_button.setDefault(True)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _apply_theme(self) -> None:
        self.setStyleSheet(_DARK_CSS if self._is_dark else _LIGHT_CSS)

    # -- behaviour ---------------------------------------------------------

    def _select(self, destination: SaveDestination) -> None:
        self._destination = destination
        is_local = destination is SaveDestination.LOCAL
        self._local_card.set_selected(is_local)
        self._draft_card.set_selected(not is_local)

    def _on_accept(self) -> None:
        self._remember = self._remember_checkbox.isChecked()
        self.accept()

    # -- keyboard navigation -----------------------------------------------

    def keyPressEvent(self, event):  # noqa: D401 — Qt override
        # Up/Down moves between cards; Enter accepts. Matches macOS sheet
        # conventions and Windows native dialog behaviour.
        if event.key() in (Qt.Key_Up, Qt.Key_Left):
            self._select(SaveDestination.LOCAL)
            return
        if event.key() in (Qt.Key_Down, Qt.Key_Right):
            self._select(SaveDestination.NOSTR_DRAFT)
            return
        super().keyPressEvent(event)
