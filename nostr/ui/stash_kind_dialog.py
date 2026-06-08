"""Pick the inner kind for a Nostr draft (Short note vs. Long-form article).

This is the second-stage dialog in the stash flow. The first stage
(``SaveDestinationDialog``) routes the user here when they choose
"Save as private Nostr draft" — or it's skipped via the per-tab
"remember" toggle, in which case the previous binding's identifier
and kind are reused without prompting.

UX:
  - Two cards at the top, mutually exclusive (just like the
    destination dialog). Selecting "Long-form article" expands an
    inline metadata block (slug, title, summary). The slug auto-fills
    from the title via ``publisher.slugify`` until the user types
    something themselves.
  - The dialog never asks for cover image or hashtags — those are
    publish-time concerns; the draft only needs a stable d-tag so a
    later promote-to-publish can carry forward.
  - The default selection follows the caller's preference (typically
    the tab's previous binding, or "Note" for an unbound new tab).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..drafts import (
    INNER_KIND_LONG_FORM,
    INNER_KIND_SHORT_NOTE,
    new_note_identifier,
)
from ..publisher import slugify


class StashKind(Enum):
    """Inner-event kind for the draft."""

    NOTE = INNER_KIND_SHORT_NOTE
    ARTICLE = INNER_KIND_LONG_FORM


@dataclass
class StashChoice:
    """Result of a successful ``StashKindDialog.exec()``.

    For a note draft, ``slug`` is auto-generated as a UUID identifier and
    ``title`` / ``summary`` are empty. For an article, ``slug`` becomes
    the addressable ``d``-tag — the same slug the article will use if
    later promoted to a real publish.
    """

    kind: StashKind
    identifier: str
    title: str = ""
    summary: str = ""


# --------------------------------------------------------------------------- #
# Stylesheets — matched to SaveDestinationDialog's vocabulary                 #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#stash_kind_title { color: #FFFFFF; font-size: 16px; font-weight: 600; }
QLabel#stash_kind_subtitle { color: #858585; font-size: 12px; }
QLabel#stash_kind_field_label {
    color: #858585; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.6px;
}
QLabel#stash_kind_hint { color: #999999; font-size: 11px; }
QLabel#stash_kind_option { color: #FFFFFF; font-size: 13px; font-weight: 500; }

QFrame#stash_kind_card {
    background: #252526;
    border: 1px solid #3C3C3C;
    border-radius: 6px;
}
QFrame#stash_kind_card[selected="true"] {
    background: #2A2D2E;
    border: 1px solid #007ACC;
}
QFrame#stash_kind_divider { background: #2D2D30; max-height: 1px; min-height: 1px; }

QRadioButton {
    color: #D4D4D4;
    spacing: 8px;
    background: transparent;
    border: none;
    padding: 0;
}
QRadioButton::indicator { width: 14px; height: 14px; }

QLineEdit {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #264F78;
    font-size: 12px;
}
QLineEdit:focus { border-color: #007ACC; }
QLineEdit:read-only { color: #858585; }

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
QPushButton:disabled { background: #252526; color: #6A6A6A; border-color: #2D2D2D; }
QPushButton:default { background: #007ACC; color: #FFFFFF; border-color: #1177C7; }
QPushButton:default:hover { background: #1177C7; }
QPushButton:default:disabled { background: #2D4F69; color: #8A9BA8; border-color: #2D4F69; }
"""

_LIGHT_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#stash_kind_title { color: #1A1A1A; font-size: 16px; font-weight: 600; }
QLabel#stash_kind_subtitle { color: #777777; font-size: 12px; }
QLabel#stash_kind_field_label {
    color: #999999; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.6px;
}
QLabel#stash_kind_hint { color: #777777; font-size: 11px; }
QLabel#stash_kind_option { color: #1A1A1A; font-size: 13px; font-weight: 500; }

QFrame#stash_kind_card {
    background: #F8F8F8;
    border: 1px solid #E1E1E1;
    border-radius: 6px;
}
QFrame#stash_kind_card[selected="true"] {
    background: #EAF3FB;
    border: 1px solid #0078D4;
}
QFrame#stash_kind_divider { background: #ECECEC; max-height: 1px; min-height: 1px; }

QRadioButton {
    color: #333333;
    spacing: 8px;
    background: transparent;
    border: none;
    padding: 0;
}
QRadioButton::indicator { width: 14px; height: 14px; }

QLineEdit {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #0078D4;
    font-size: 12px;
}
QLineEdit:focus { border-color: #0078D4; }
QLineEdit:read-only { color: #777777; }

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
QPushButton:disabled { background: #F8F8F8; color: #BBBBBB; border-color: #EBEBEB; }
QPushButton:default { background: #0078D4; color: #FFFFFF; border-color: #1066B4; }
QPushButton:default:hover { background: #1066B4; }
QPushButton:default:disabled { background: #BDD9F0; color: #FFFFFF; border-color: #BDD9F0; }
"""


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("stash_kind_divider")
    line.setFrameShape(QFrame.NoFrame)
    return line


def _field(label_text: str, edit: QLineEdit) -> QWidget:
    """Compact label-above-input column."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(label_text)
    label.setObjectName("stash_kind_field_label")
    layout.addWidget(label)
    layout.addWidget(edit)
    return container


class _KindCard(QFrame):
    """Selectable card for a kind. Click anywhere to pick."""

    clicked = Signal()

    def __init__(
        self,
        *,
        title: str,
        hint: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("stash_kind_card")
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
        self.radio.setFocusPolicy(Qt.NoFocus)
        head.addWidget(self.radio, 0, Qt.AlignVCenter)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("stash_kind_option")
        head.addWidget(self.title_label, 1, Qt.AlignVCenter)
        layout.addLayout(head)

        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("stash_kind_hint")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

    def set_selected(self, selected: bool) -> None:
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

class StashKindDialog(QDialog):
    """Modal: pick the inner kind + (for articles) slug/title/summary.

    Access ``self.choice`` after ``exec()`` returns ``QDialog.Accepted``.
    """

    def __init__(
        self,
        *,
        default: StashKind = StashKind.NOTE,
        suggested_title: str = "",
        suggested_slug: str = "",
        suggested_summary: str = "",
        existing_note_identifier: str = "",
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save as Nostr draft")
        self.setModal(True)
        self.setMinimumWidth(500)
        self._is_dark = is_dark
        self._kind: StashKind = default
        # When the tab already has a stable note identifier, reuse it so
        # repeat stashes update the addressable wrap rather than minting
        # a new one. Otherwise a fresh UUID is generated on accept.
        self._existing_note_identifier = existing_note_identifier
        # Tracks whether the user has touched the slug manually; until
        # they do, the slug mirrors slugify(title) in real time.
        self._slug_is_auto = not bool(suggested_slug)
        self._choice: Optional[StashChoice] = None

        self._build_ui(
            suggested_title=suggested_title,
            suggested_slug=suggested_slug,
            suggested_summary=suggested_summary,
        )
        self._apply_theme()
        self._select(default)

    # -- public properties -------------------------------------------------

    @property
    def choice(self) -> Optional[StashChoice]:
        return self._choice

    # -- construction ------------------------------------------------------

    def _build_ui(
        self,
        *,
        suggested_title: str,
        suggested_slug: str,
        suggested_summary: str,
    ) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 18)
        root.setSpacing(14)

        title = QLabel("Save as Nostr draft")
        title.setObjectName("stash_kind_title")
        root.addWidget(title)

        subtitle = QLabel(
            "Drafts are end-to-end encrypted to your key. Pick the kind "
            "so the draft can be promoted to a publish later."
        )
        subtitle.setObjectName("stash_kind_subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # Kind cards
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._note_card = _KindCard(
            title="Short note",
            hint=(
                "Quick thought, plain text. Kind 1 when published. "
                "No slug — the draft is identified by a private UUID."
            ),
        )
        self._article_card = _KindCard(
            title="Long-form article",
            hint=(
                "Markdown post with a stable slug. Kind 30023 when "
                "published; the slug ties the draft to the eventual "
                "article so re-stashing replaces in place."
            ),
        )
        self._group.addButton(self._note_card.radio, 0)
        self._group.addButton(self._article_card.radio, 1)
        self._note_card.clicked.connect(lambda: self._select(StashKind.NOTE))
        self._article_card.clicked.connect(lambda: self._select(StashKind.ARTICLE))
        self._note_card.radio.toggled.connect(
            lambda on: on and self._select(StashKind.NOTE)
        )
        self._article_card.radio.toggled.connect(
            lambda on: on and self._select(StashKind.ARTICLE)
        )

        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)
        cards_row.addWidget(self._note_card, 1)
        cards_row.addWidget(self._article_card, 1)
        root.addLayout(cards_row)

        # Article-only metadata panel
        self._article_panel = QWidget()
        article_layout = QVBoxLayout(self._article_panel)
        article_layout.setContentsMargins(0, 4, 0, 0)
        article_layout.setSpacing(10)
        article_layout.addWidget(_divider())

        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("Article title (used to derive the slug)")
        self._title_edit.setText(suggested_title)
        self._title_edit.textChanged.connect(self._on_title_changed)
        article_layout.addWidget(_field("Title", self._title_edit))

        self._slug_edit = QLineEdit()
        self._slug_edit.setPlaceholderText("article-slug")
        seed_slug = suggested_slug or (slugify(suggested_title) if suggested_title else "")
        self._slug_edit.setText(seed_slug)
        self._slug_edit.textEdited.connect(self._on_slug_edited)
        article_layout.addWidget(_field("Slug (the draft's d-tag)", self._slug_edit))

        self._summary_edit = QLineEdit()
        self._summary_edit.setPlaceholderText("Optional one-sentence summary")
        self._summary_edit.setText(suggested_summary)
        article_layout.addWidget(_field("Summary (optional)", self._summary_edit))

        root.addWidget(self._article_panel)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self._save_btn = buttons.button(QDialogButtonBox.Ok)
        self._save_btn.setText("Save draft")
        self._save_btn.setDefault(True)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_save_enabled()

    def _apply_theme(self) -> None:
        self.setStyleSheet(_DARK_CSS if self._is_dark else _LIGHT_CSS)

    # -- behaviour ---------------------------------------------------------

    def _select(self, kind: StashKind) -> None:
        self._kind = kind
        is_note = kind is StashKind.NOTE
        self._note_card.set_selected(is_note)
        self._article_card.set_selected(not is_note)
        self._article_panel.setVisible(not is_note)
        # Resize the dialog to its sizeHint when the panel collapses or
        # expands — Qt won't shrink the dialog otherwise on macOS.
        self.adjustSize()
        self._refresh_save_enabled()

    def _on_title_changed(self, text: str) -> None:
        if self._slug_is_auto:
            self._slug_edit.blockSignals(True)
            self._slug_edit.setText(slugify(text) if text else "")
            self._slug_edit.blockSignals(False)
        self._refresh_save_enabled()

    def _on_slug_edited(self, _text: str) -> None:
        # The user took manual control — stop auto-syncing.
        self._slug_is_auto = False
        self._refresh_save_enabled()

    def _refresh_save_enabled(self) -> None:
        if self._kind is StashKind.NOTE:
            self._save_btn.setEnabled(True)
            return
        # Articles need a non-empty slug to be addressable. Title is
        # nice-to-have but not strictly required by the protocol.
        slug_ok = bool(self._slug_edit.text().strip())
        self._save_btn.setEnabled(slug_ok)

    def _on_accept(self) -> None:
        if self._kind is StashKind.NOTE:
            # Reuse the existing note identifier if the tab already has
            # one bound; otherwise mint a fresh UUID-tagged d-value.
            identifier = self._existing_note_identifier or new_note_identifier()
            self._choice = StashChoice(
                kind=StashKind.NOTE,
                identifier=identifier,
                title="",
                summary="",
            )
        else:
            slug = self._slug_edit.text().strip()
            if not slug:
                # Defensive — the OK button shouldn't have been enabled.
                return
            self._choice = StashChoice(
                kind=StashKind.ARTICLE,
                identifier=slug,
                title=self._title_edit.text().strip(),
                summary=self._summary_edit.text().strip(),
            )
        self.accept()
