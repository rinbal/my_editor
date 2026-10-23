# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pick the inner kind for a Nostr draft (Short note vs. Long-form article).

This is the second-stage dialog in the stash flow. The first stage
(``SaveDestinationDialog``) routes the user here when they choose
"Save as private Nostr draft", or it's skipped via the per-tab
"remember" toggle, in which case the previous binding's identifier
and kind are reused without prompting.

UX:
  - Two cards at the top, mutually exclusive (just like the
    destination dialog). Selecting "Long-form article" expands an
    inline metadata block (slug, title, summary). The slug auto-fills
    from the title via ``publisher.slugify`` until the user types
    something themselves.
  - The dialog never asks for cover image or hashtags: those are
    publish-time concerns; the draft only needs a stable d-tag so a
    later promote-to-publish can carry forward.
  - The default selection follows the caller's preference (typically
    the tab's previous binding, or "Note" for an unbound new tab).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AbstractSet, Optional

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

import announce
import theme
from . import appearance
from alerts import answer_return_as_the_system_does, start_on_default
from i18n import _

from ..drafts import (
    INNER_KIND_LONG_FORM,
    INNER_KIND_SHORT_NOTE,
    new_note_identifier,
)
from ..publisher import link_name, slugify
from .secret_key_check import slug_problem


class StashKind(Enum):
    """Inner-event kind for the draft."""

    NOTE = INNER_KIND_SHORT_NOTE
    ARTICLE = INNER_KIND_LONG_FORM


@dataclass
class StashChoice:
    """Result of a successful ``StashKindDialog.exec()``.

    For a note draft, ``slug`` is auto-generated as a UUID identifier and
    ``title`` / ``summary`` are empty. For an article, ``slug`` becomes
    the addressable ``d``-tag, the same slug the article will use if
    later promoted to a real publish.
    """

    kind: StashKind
    identifier: str
    title: str = ""
    summary: str = ""


# --------------------------------------------------------------------------- #
# Stylesheets, matched to SaveDestinationDialog's vocabulary                  #
# --------------------------------------------------------------------------- #

def _stylesheet(dark: bool) -> str:
    r = appearance.roles(dark)
    return appearance.dialog_stylesheet(dark) + f"""
    QLabel#stash_kind_title {{ font-size: 16px; font-weight: 600; }}
    QLabel#stash_kind_option {{ font-weight: 600; }}
    QLabel#stash_kind_subtitle, QLabel#stash_kind_field_label, QLabel#stash_kind_hint {{ color: {r.muted}; }}
    QFrame#stash_kind_card {{ background: {r.control}; border: 1px solid {r.edge}; border-radius: 8px; }}
    QFrame#stash_kind_card[selected="true"] {{ background: {r.soft}; border-color: {r.focus}; }}
    QFrame#stash_kind_divider {{ background: {r.line}; max-height: 1px; min-height: 1px; }}
    QRadioButton {{ color: {r.text}; background: transparent; spacing: 8px; }}
    QLabel#stash_kind_error {{ color: {theme.error_text_color(dark)}; }}
    """


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
    label.setBuddy(edit)            # a screen reader names the field by it
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
        # The card's radio takes the focus, so the kinds are reached with
        # Tab and chosen with the arrow keys, as in any radio group.
        self.radio = QRadioButton()
        self.radio.setAccessibleName(title)
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

    def mousePressEvent(self, event):  # noqa: D401 (Qt override)
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
        kept: AbstractSet[str] = frozenset(),
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        # The details the article's file keeps as written (the fields of
        # ArticleDetails.kept_fields): read-only here too, saying so.
        self._kept = frozenset(kept)
        self.setWindowTitle(_("Save as Nostr draft"))
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
        self._slug_is_auto = not bool(suggested_slug) and "identifier" not in self._kept
        # The link name the article has already, kept letter for letter
        # (link_name).
        self._kept_slug = suggested_slug
        self._choice: Optional[StashChoice] = None

        self._build_ui(
            suggested_title=suggested_title,
            suggested_slug=suggested_slug,
            suggested_summary=suggested_summary,
        )
        self._apply_theme()
        self._select(default)

    def _kept_if(self, field: QWidget, name: str, edit: QLineEdit) -> QWidget:
        """``field``, read-only with the line Article Details shows under
        it when the article's file keeps the detail ``name`` as written."""
        if name in self._kept:
            edit.setReadOnly(True)
            note = QLabel(_("Kept as written in the file."))
            note.setObjectName("stash_kind_subtitle")
            note.setWordWrap(True)
            field.layout().addWidget(note)
        return field

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
        # The window takes the height its wrapped hints need at its width,
        # so no hint is ever cut off, in any language (as Settings does).
        root.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        title = QLabel(_("Save as Nostr draft"))
        title.setObjectName("stash_kind_title")
        root.addWidget(title)

        subtitle = QLabel(
            _("Drafts are end-to-end encrypted to your key. Pick the kind "
              "so the draft can be promoted to a publish later.")
        )
        subtitle.setObjectName("stash_kind_subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # Kind cards
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._note_card = _KindCard(
            title=_("Short note"),
            hint=_(
                "A quick thought, as plain text. Published as a note. "
                "No link name: the draft gets a private identifier."
            ),
        )
        self._article_card = _KindCard(
            title=_("Long-form article"),
            hint=_(
                "A Markdown post with a fixed link name. Published as an "
                "article. The link name ties the draft to the article it "
                "becomes, so saving it again replaces it."
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
        self._title_edit.setPlaceholderText(_("Article title"))
        self._title_edit.setText(suggested_title)
        self._title_edit.textChanged.connect(self._on_title_changed)
        article_layout.addWidget(self._kept_if(_field(_("Title"), self._title_edit),
                                               "title", self._title_edit))

        self._slug_edit = QLineEdit()
        self._slug_edit.setPlaceholderText(_("my-article"))
        seed_slug = suggested_slug or (slugify(suggested_title) if suggested_title else "")
        self._slug_edit.setText(seed_slug)
        self._slug_edit.textEdited.connect(self._on_slug_edited)
        slug_field = _field(_("Link name"), self._slug_edit)
        # Why the slug can't be saved, under it (the draft's slug is not
        # encrypted: relays and readers see it).
        self._slug_problem = QLabel()
        self._slug_problem.setObjectName("stash_kind_error")
        self._slug_problem.setWordWrap(True)
        self._slug_problem.hide()
        slug_field.layout().addWidget(self._slug_problem)
        article_layout.addWidget(self._kept_if(slug_field, "identifier", self._slug_edit))

        self._summary_edit = QLineEdit()
        self._summary_edit.setPlaceholderText(_("Optional one-sentence summary"))
        self._summary_edit.setText(suggested_summary)
        article_layout.addWidget(self._kept_if(_field(_("Summary (optional)"),
                                                      self._summary_edit),
                                               "summary", self._summary_edit))

        root.addWidget(self._article_panel)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        self._save_btn = buttons.button(QDialogButtonBox.Ok)
        self._save_btn.setText(_("Save draft"))
        answer_return_as_the_system_does(buttons.buttons())
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_save_enabled()
        # It opens on its action, so Return saves and Space does too; an
        # article without a title yet opens on its title, which Save waits
        # for (the focus would otherwise fall to Cancel, which on Windows
        # and Linux then takes the default).
        if self._save_btn.isEnabled():
            start_on_default(self._save_btn)
        else:
            self._title_edit.setFocus()

    def _apply_theme(self) -> None:
        self.apply_theme(self._is_dark)
        appearance.bind(self)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_stylesheet(is_dark))
        appearance.surface(self, is_dark)

    # -- behaviour ---------------------------------------------------------

    def _select(self, kind: StashKind) -> None:
        self._kind = kind
        is_note = kind is StashKind.NOTE
        self._note_card.set_selected(is_note)
        self._article_card.set_selected(not is_note)
        self._article_panel.setVisible(not is_note)
        self._refresh_save_enabled()
        self._fit()

    def _fit(self) -> None:
        """Fit the dialog to what it shows. The size hint makes it shrink
        when the article panel collapses (Qt won't shrink it otherwise on
        macOS); a wrapped message under the slug needs the height its
        lines take at this width, which a size hint does not count (and a
        translation can add lines)."""
        self.adjustSize()
        needed = self.layout().totalHeightForWidth(self.width())
        if needed > self.height():
            self.resize(self.width(), needed)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._fit()                  # opened with a message under the slug
        if not self._slug_problem.isHidden():
            # Said before the window was there to say it from: again now.
            announce.announce(self._slug_edit, self._slug_problem.text())

    def _on_title_changed(self, text: str) -> None:
        if self._slug_is_auto:
            self._slug_edit.blockSignals(True)
            self._slug_edit.setText(slugify(text) if text else "")
            self._slug_edit.blockSignals(False)
        self._refresh_save_enabled()

    def _on_slug_edited(self, _text: str) -> None:
        # The user took manual control, so stop auto-syncing.
        self._slug_is_auto = False
        self._refresh_save_enabled()

    def _refresh_save_enabled(self) -> None:
        if self._kind is StashKind.NOTE:
            self._save_btn.setEnabled(True)
            return
        # Articles need a non-empty slug to be addressable. Title is
        # nice-to-have but not strictly required by the protocol.
        slug = link_name(self._slug_edit.text(), self._kept_slug)
        problem = slug_problem(slug) or ""
        said = self._slug_problem.text() if not self._slug_problem.isHidden() else ""
        announce.field_problem(self._slug_edit, self._slug_problem, problem)
        self._save_btn.setEnabled(bool(slug) and not problem)
        if problem != said and self.isVisible():
            self._fit()

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
            slug = link_name(self._slug_edit.text(), self._kept_slug)
            if not slug or slug_problem(slug):
                # Defensive: the OK button shouldn't have been enabled.
                return
            self._choice = StashChoice(
                kind=StashKind.ARTICLE,
                identifier=slug,
                title=self._title_edit.text().strip(),
                summary=self._summary_edit.text().strip(),
            )
        self.accept()
