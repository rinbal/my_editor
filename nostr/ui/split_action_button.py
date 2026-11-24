# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A normal default push button with a separate attached options arrow.

Keeping the push button as the button-box item preserves native order, Return,
Escape and accessibility. The arrow is its own keyboard-focusable menu button;
clicking the main action never opens the menu.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QToolButton

from . import appearance


class SplitActionButton(QPushButton):
    ARROW_WIDTH = 28

    def __init__(self, text, *, dark=False, parent=None):
        super().__init__(text, parent)
        self.setObjectName("split_publish")
        self.options = QToolButton(self)
        self.options.setObjectName("split_publish_options")
        self.options.setArrowType(Qt.ArrowType.DownArrow)
        self.options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.options.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setStyleSheet("QPushButton#split_publish { padding-right: 42px; }")
        self.apply_theme(dark)

    def apply_theme(self, dark):
        roles = appearance.roles(dark)
        self.options.setStyleSheet(
            f"QToolButton#split_publish_options {{ color: {roles.on_accent};"
            " background: transparent; border: none; border-left: 1px solid rgba(255,255,255,0.35);"
            " border-radius: 0; padding: 0; margin: 4px 0; }"
            f"QToolButton#split_publish_options:focus {{ border: 1px solid {roles.on_accent}; }}"
            f"QToolButton#split_publish_options:disabled {{ color: {roles.disabled}; }}"
            "QToolButton#split_publish_options:hover { background: rgba(0,0,0,0.12); }"
            "QToolButton#split_publish_options:pressed { background: rgba(0,0,0,0.20); }"
            "QToolButton#split_publish_options::menu-indicator { image: none; width: 0; }")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.options.setGeometry(self.width() - self.ARROW_WIDTH, 1,
                                 self.ARROW_WIDTH - 1, max(0, self.height() - 2))
