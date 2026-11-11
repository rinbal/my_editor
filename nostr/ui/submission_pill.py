# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pill beside an article's tab (plan 10.4; the prototype's screen
14): the publication's logo and one status word ("Eingereicht", "Du bist
dran", "Wird eingereicht …"). It is there only while the article in the
tab is submitted by the account in use, and opens the submission's
detail. Whose turn it is shows in words and in weight, never in colour
alone; a screen reader hears "Einreichung bei <Publikation>: <Wort>".
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QToolButton, QWidget

from i18n import _

from . import appearance

_LOGO = 18


class SubmissionPill(QToolButton):
    """See the module docstring. ``sid`` is the submission it shows
    (``""`` while hidden)."""

    def __init__(self, *, is_dark: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("submission_tab_pill")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(_LOGO, _LOGO))
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sid = ""
        self.setAccessibleName(_("Submission"))     # named while hidden too
        self._turn = False
        self._is_dark = bool(is_dark)
        self.apply_theme(is_dark)
        self.hide()

    def show_status(self, sid: str, word: str, publication: str, logo: QPixmap, *,
                    turn: bool) -> None:
        """The submission ``sid`` of the article in the tab, in a word."""
        self.sid, self._turn = sid, turn
        self.setText(word)
        self.setIcon(QIcon(logo))
        spoken = _("Submission to {publication}: {status}").format(publication=publication,
                                                                  status=word)
        self.setAccessibleName(spoken)
        self.setToolTip(spoken)
        self.apply_theme(self._is_dark)
        self.setVisible(bool(word))

    def clear(self) -> None:
        self.sid = ""
        self.hide()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        r = appearance.roles(self._is_dark)
        fill, edge, weight = (r.soft, r.focus, 600) if self._turn else (r.control, r.line, 400)
        self.setStyleSheet(f"""
            #submission_tab_pill {{ background: {fill}; color: {r.text};
                border: 1px solid {edge}; border-radius: 11px; padding: 1px 9px 1px 4px;
                font-weight: {weight}; }}
            #submission_tab_pill:hover {{ border-color: {r.ink}; }}
            #submission_tab_pill:focus {{ border: 2px solid {r.focus}; padding: 0px 8px 0px 3px; }}
        """)


__all__ = ["SubmissionPill"]
