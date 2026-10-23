# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Insert > Note or Article…: the panel that asks which one.

A small panel at the caret with one field, the way Add Link asks for an
address: paste a link to a note or an article (a ``nostr:`` link, a bare
``nevent1…`` or ``naddr1…``, or the address a reader shows it at, such
as njump.me, primal.net or yakihonne.com). Why it cannot be one is said
right under the field (nostr/refs.py), never in an alert, and Insert
works only once it can. Return inserts, Escape closes the panel, and the
text gets the focus back either way.

The panel only asks: ``chosen(reference)`` hands over what it read.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

import announce
import theme
from . import appearance
from i18n import _
from popup_placement import place_below

from .. import refs


class ReferencePopover(QFrame):
    """One field for a link to a note or an article; ``chosen(Reference)``
    on Insert."""

    chosen = Signal(object)

    WIDTH = 420

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("ReferencePopover")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName(_("Insert Note or Article"))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(8)
        self.label = QLabel(_("Link to a note or an article:"))
        outer.addWidget(self.label)
        self.field = QLineEdit()
        self.field.setPlaceholderText("https://njump.me/nevent1…")
        self.field.setClearButtonEnabled(True)
        self.label.setBuddy(self.field)
        self.field.setAccessibleName(_("Link to a note or an article"))
        outer.addWidget(self.field)
        self.problem = QLabel("")
        self.problem.setObjectName("ReferenceProblem")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        outer.addWidget(self.problem)
        self.hint = QLabel(_("Primal, YakiHonne, Highlighter, Amethyst and Snort show it as a "
                             "card; other apps show a link."))
        self.hint.setObjectName("ReferenceHint")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.cancel_button = QPushButton(_("Cancel"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.close)
        buttons.addWidget(self.cancel_button)
        self.insert_button = QPushButton(_("Insert"))
        self.insert_button.setAutoDefault(False)
        self.insert_button.setDefault(True)
        self.insert_button.clicked.connect(self._insert)
        buttons.addWidget(self.insert_button)
        outer.addLayout(buttons)

        self.field.textChanged.connect(self._check)
        self.field.returnPressed.connect(self._insert)
        self.setMinimumWidth(self.WIDTH)
        self._check()
        self._apply_colors()

    def _apply_colors(self) -> None:
        self.apply_theme(theme.is_dark_active())
        appearance.bind(self)

    def apply_theme(self, dark: bool) -> None:
        r = appearance.roles(dark)
        background, border, field, text = r.popover, r.edge, r.field, r.text
        self.setStyleSheet(
            appearance.dialog_stylesheet(dark)
            + f"#ReferencePopover {{ background: {background}; border: 1px solid {border}; }}"
            + f"QLineEdit {{ background: {field}; color: {text}; border: 1px solid {border};"
              f" border-radius: 4px; padding: 3px 6px; }}"
            + f"#ReferenceProblem {{ color: {theme.error_text_color(dark)}; }}"
            + f"#ReferenceHint {{ color: {r.muted}; }}")
        appearance.surface(self, dark, kind="popover")

    def reference(self) -> Optional[refs.Reference]:
        return refs.normalize_reference(self.field.text())[0]

    def _check(self) -> None:
        raw = self.field.text()
        reference, reason = refs.normalize_reference(raw)
        self.insert_button.setEnabled(reference is not None)
        # No complaint about an empty field: there is nothing wrong yet.
        shown = self.problem.text() if not self.problem.isHidden() else ""
        problem = reason if raw.strip() and reference is None else ""
        announce.field_problem(self.field, self.problem, problem)
        if problem != shown:
            self.adjustSize()

    def _insert(self) -> None:
        reference = self.reference()
        if reference is None:
            self.field.setFocus()
            return
        self.close()
        self.chosen.emit(reference)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Cancel) or event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def show_below(self, anchor: QRect, parent: QWidget) -> None:
        """Open under ``anchor`` (the caret, in ``parent``'s coordinates)."""
        place_below(self, anchor, parent)
        self.show()
        self.field.setFocus()
