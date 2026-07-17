# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-tab banner: "A newer version of this draft is on relays."

Shown above the editor when ``DraftSync`` notices that the draft
currently bound to a tab has been updated from another device (same
``d``-tag, newer ``created_at`` than the one the tab was opened with).
The user picks one of three resolutions:

  - **View** — open the remote version in a new tab without touching
    the current one. Lets the user diff visually.
  - **Reload** — replace the current tab's contents with the remote
    version. The local unsaved edits are lost; we warn if dirty.
  - **Keep mine** — dismiss the banner. The next stash from this tab
    will become the newest ``created_at`` and win.

Cross-platform notes:
  - All colours / borders use the existing theme palette.
  - No platform-specific assets — the icon is a Unicode glyph so the
    banner renders identically on macOS, Windows, and Linux.
  - The Close button uses a Unicode × (U+00D7) for the same reason —
    Qt's standard icons differ subtly across themes.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)


# --------------------------------------------------------------------------- #
# Stylesheets                                                                 #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QFrame#draft_conflict_banner {
    background: #2D2613;
    border: none;
    border-bottom: 1px solid #5C4520;
}
QLabel#draft_conflict_icon {
    color: #FFB347;
    font-size: 14px;
    padding-left: 4px;
}
QLabel#draft_conflict_text {
    color: #E0CFA5;
    font-size: 12px;
}
QPushButton#draft_conflict_action {
    background: transparent;
    color: #FFB347;
    border: 1px solid #5C4520;
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 12px;
    min-width: 64px;
}
QPushButton#draft_conflict_action:hover { background: #3D331B; }
QPushButton#draft_conflict_action:pressed { background: #2D2613; }
QPushButton#draft_conflict_action[primary="true"] {
    background: #5C4520;
    color: #FFFFFF;
    border-color: #5C4520;
}
QPushButton#draft_conflict_action[primary="true"]:hover { background: #6E5527; }

QToolButton#draft_conflict_close {
    background: transparent;
    color: #8A7E5C;
    border: none;
    padding: 2px 6px;
    font-size: 14px;
}
QToolButton#draft_conflict_close:hover { color: #FFB347; }
"""

_LIGHT_CSS = """
QFrame#draft_conflict_banner {
    background: #FFF4D9;
    border: none;
    border-bottom: 1px solid #D9B36C;
}
QLabel#draft_conflict_icon {
    color: #A05000;
    font-size: 14px;
    padding-left: 4px;
}
QLabel#draft_conflict_text {
    color: #5C3D14;
    font-size: 12px;
}
QPushButton#draft_conflict_action {
    background: transparent;
    color: #A05000;
    border: 1px solid #D9B36C;
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 12px;
    min-width: 64px;
}
QPushButton#draft_conflict_action:hover { background: #FCE9B8; }
QPushButton#draft_conflict_action:pressed { background: #F5DDA0; }
QPushButton#draft_conflict_action[primary="true"] {
    background: #A05000;
    color: #FFFFFF;
    border-color: #A05000;
}
QPushButton#draft_conflict_action[primary="true"]:hover { background: #B85B00; }

QToolButton#draft_conflict_close {
    background: transparent;
    color: #A88B5F;
    border: none;
    padding: 2px 6px;
    font-size: 14px;
}
QToolButton#draft_conflict_close:hover { color: #A05000; }
"""


# --------------------------------------------------------------------------- #
# Widget                                                                      #
# --------------------------------------------------------------------------- #

class DraftConflictBanner(QFrame):
    """Slim banner with View / Reload / Keep mine actions.

    Signals:
      view_remote()   — open the newer draft in a *new* tab.
      reload()        — replace this tab's contents with the newer draft.
      keep_mine()     — dismiss; the next stash supersedes the remote.
      dismissed()     — banner is closing (fired whenever the user
                        clicks any action or the close button), so the
                        caller can detach it from the tab.
    """

    view_remote = Signal()
    reload = Signal()
    keep_mine = Signal()
    dismissed = Signal()

    def __init__(
        self,
        *,
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("draft_conflict_banner")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._is_dark = is_dark
        self._build_ui()
        self.apply_theme(is_dark)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 6, 6)
        layout.setSpacing(8)

        icon = QLabel("⚠")
        icon.setObjectName("draft_conflict_icon")
        icon.setFixedWidth(20)
        layout.addWidget(icon)

        self._text = QLabel(
            "A newer version of this draft was published from another device."
        )
        self._text.setObjectName("draft_conflict_text")
        self._text.setWordWrap(True)
        layout.addWidget(self._text, 1)

        self._view_btn = QPushButton("View")
        self._view_btn.setObjectName("draft_conflict_action")
        self._view_btn.setToolTip("Open the newer version in a new tab")
        self._view_btn.clicked.connect(self._on_view)
        layout.addWidget(self._view_btn)

        self._reload_btn = QPushButton("Reload")
        self._reload_btn.setObjectName("draft_conflict_action")
        self._reload_btn.setProperty("primary", "true")
        self._reload_btn.setToolTip(
            "Replace this tab's contents with the newer version. "
            "Unsaved local edits will be lost."
        )
        self._reload_btn.clicked.connect(self._on_reload)
        layout.addWidget(self._reload_btn)

        self._keep_btn = QPushButton("Keep mine")
        self._keep_btn.setObjectName("draft_conflict_action")
        self._keep_btn.setToolTip(
            "Dismiss and let the next save from this tab win."
        )
        self._keep_btn.clicked.connect(self._on_keep)
        layout.addWidget(self._keep_btn)

        self._close_btn = QToolButton()
        self._close_btn.setObjectName("draft_conflict_close")
        self._close_btn.setText("×")
        self._close_btn.setToolTip("Dismiss")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(self._on_close)
        layout.addWidget(self._close_btn)

    # -- public API --------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)
        # Re-polish primary button so the property-based selector
        # re-applies after a theme switch.
        for btn in (self._view_btn, self._reload_btn, self._keep_btn):
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def set_message(self, text: str) -> None:
        """Override the banner copy — e.g. include the timestamp of the
        remote version once we know it."""
        self._text.setText(text)

    # -- signal handlers ---------------------------------------------------

    def _on_view(self) -> None:
        self.view_remote.emit()
        self._finish()

    def _on_reload(self) -> None:
        self.reload.emit()
        self._finish()

    def _on_keep(self) -> None:
        self.keep_mine.emit()
        self._finish()

    def _on_close(self) -> None:
        # Closing without a positive action is equivalent to "keep mine"
        # in terms of data outcome (no change), but we keep the signal
        # distinct so the caller can disambiguate explicit intent.
        self._finish()

    def _finish(self) -> None:
        self.dismissed.emit()
        # Caller is responsible for actually removing the banner widget
        # from its layout; emitting and hiding leaves the widget alive
        # so a tooltip-trail or animation can complete first.
        self.hide()
