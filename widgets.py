#!/usr/bin/env python3


from PySide6.QtCore import Qt, QRect, QSize, Signal
from PySide6.QtGui import QPainter, QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QLabel, QPushButton, QFrame, QMenu, QCheckBox
)
from constants import DARK_BG, LIGHT_BG, MONO_FONT


class LineNumberGutter(QWidget):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.setFixedWidth(60)
        self.is_dark = True
        self._update_theme()

        self.editor.verticalScrollBar().valueChanged.connect(self.update)
        self.editor.textChanged.connect(self.update)
        self.editor.cursorPositionChanged.connect(self.update)

    def _update_theme(self):
        if hasattr(self.editor, '_theme_colors'):
            is_dark = self.editor._theme_colors['bg'] == DARK_BG
            self.is_dark = is_dark
            if is_dark:
                self.setStyleSheet("""
                    QWidget {
                        background: #252526;
                        color: #858585;
                        border-right: 1px solid #3C3C3C;
                    }
                """)
            else:
                self.setStyleSheet("""
                    QWidget {
                        background: #F8F8F8;
                        color: #666666;
                        border-right: 1px solid #E1E1E1;
                    }
                """)

    def paintEvent(self, event):
        painter = QPainter(self)

        if self.is_dark:
            painter.fillRect(self.rect(), QColor("#252526"))
            text_color = QColor("#858585")
        else:
            painter.fillRect(self.rect(), QColor("#F8F8F8"))
            text_color = QColor("#666666")

        doc = self.editor.document()
        layout = doc.documentLayout()

        font = QFont(MONO_FONT, 14)
        painter.setFont(font)
        painter.setPen(text_color)

        font_metrics = painter.fontMetrics()
        editor_padding = 8

        first_visible_pos = self.editor.cursorForPosition(self.editor.viewport().rect().topLeft())
        first_visible_block = first_visible_pos.block()
        first_block_rect = layout.blockBoundingRect(first_visible_block)

        block = first_visible_block
        block_number = block.blockNumber() + 1

        while block.isValid():
            block_rect = layout.blockBoundingRect(block)
            y_pos = (block_rect.top() - first_block_rect.top()) + editor_padding + font_metrics.ascent()

            if y_pos - font_metrics.ascent() > self.height():
                break
            if y_pos - font_metrics.ascent() < -editor_padding:
                block = block.next()
                block_number += 1
                continue

            line_text = str(block_number)
            text_width = font_metrics.horizontalAdvance(line_text)
            x_pos = self.width() - text_width - 8
            painter.drawText(x_pos, y_pos, line_text)

            block = block.next()
            block_number += 1


class FindBar(QFrame):
    def __init__(self, on_find_next, on_find_prev, on_close, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("FindBar")
        self.is_dark = True
        self._update_theme()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel("Search:")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search…")
        self.btn_prev = QPushButton("←")
        self.btn_next = QPushButton("→")
        self.btn_close = QPushButton("×")
        self.match_info = QLabel("")
        self.match_info.setMinimumWidth(120)

        self.btn_prev.clicked.connect(on_find_prev)
        self.btn_next.clicked.connect(on_find_next)
        self.btn_close.clicked.connect(on_close)
        self.edit.returnPressed.connect(on_find_next)

        self.edit.installEventFilter(self)

        row.addWidget(self.label)
        row.addWidget(self.edit, 1)
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        row.addWidget(self.match_info)
        row.addWidget(self.btn_close)

        self.hint_label = QLabel("Enter: next  |  Shift+Enter: prev  |  Esc: close & edit here")
        hint_font = self.hint_label.font()
        hint_font.setPointSize(hint_font.pointSize() - 1)
        hint_font.setItalic(True)
        self.hint_label.setFont(hint_font)

        outer.addLayout(row)
        outer.addWidget(self.hint_label)

    def eventFilter(self, obj, event):
        if obj == self.edit and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Return:
                if event.modifiers() == Qt.ShiftModifier:
                    self.btn_prev.clicked.emit()
                else:
                    self.btn_next.clicked.emit()
                return True
            elif event.key() == Qt.Key_Escape:
                self.btn_close.clicked.emit()
                return True
        return super().eventFilter(obj, event)

    def set_match_info(self, text):
        self.match_info.setText(text)

    def text(self):
        return self.edit.text()

    def _update_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
            #FindBar {
                background: #252526;
                border: 1px solid #3C3C3C;
                border-radius: 6px;
            }
            QLineEdit {
                background: #1E1E1E;
                color: #D4D4D4;
                border: 1px solid #3C3C3C;
                padding: 6px 8px;
                border-radius: 4px;
                selection-background-color: #264F78;
            }
            QPushButton {
                background: #2D2D30;
                color: #D4D4D4;
                border: 1px solid #3C3C3C;
                padding: 6px 10px;
                border-radius: 4px;
            }
            QPushButton:hover { background: #3C3C3C; }
            QLabel { color: #CCCCCC; }
            """)
            if hasattr(self, 'hint_label'):
                self.hint_label.setStyleSheet("color: #777777;")
        else:
            self.setStyleSheet("""
            #FindBar {
                background: #F8F8F8;
                border: 1px solid #E1E1E1;
                border-radius: 6px;
            }
            QLineEdit {
                background: #FFFFFF;
                color: #333333;
                border: 1px solid #E1E1E1;
                padding: 6px 8px;
                border-radius: 4px;
                selection-background-color: #0078D4;
            }
            QPushButton {
                background: #F3F3F3;
                color: #333333;
                border: 1px solid #E1E1E1;
                padding: 6px 10px;
                border-radius: 4px;
            }
            QPushButton:hover { background: #E1E1E1; }
            QLabel { color: #666666; }
            """)
            if hasattr(self, 'hint_label'):
                self.hint_label.setStyleSheet("color: #999999;")

    def focusIn(self):
        self.edit.setFocus()
        self.edit.selectAll()


class HeaderWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(0)

        # Left: checkboxes in an expanding widget, left-aligned
        left = QWidget()
        left.setObjectName("HeaderLeft")
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.theme_checkbox = QCheckBox("Dark Theme")
        self.theme_checkbox.setToolTip("Toggle Theme (Ctrl+Shift+T)")
        self.theme_checkbox.setChecked(True)

        self.line_numbers_checkbox = QCheckBox("Line Numbers")
        self.line_numbers_checkbox.setToolTip("Toggle Line Numbers (Ctrl+Shift+L)")

        self.syntax_highlight_checkbox = QCheckBox("Syntax Highlighting")
        self.syntax_highlight_checkbox.setToolTip("Toggle Syntax Highlighting (Ctrl+Shift+H)")
        self.syntax_highlight_checkbox.setChecked(True)

        left_layout.addWidget(self.theme_checkbox)
        left_layout.addWidget(self.line_numbers_checkbox)
        left_layout.addWidget(self.syntax_highlight_checkbox)
        left_layout.addStretch()

        # Center: undo / redo round arrows
        self.undo_btn = QPushButton("↺")
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.undo_btn.setFixedSize(26, 26)
        self.undo_btn.setEnabled(False)

        self.redo_btn = QPushButton("↻")
        self.redo_btn.setToolTip("Redo (Ctrl+Y / Ctrl+Shift+Z)")
        self.redo_btn.setFixedSize(26, 26)
        self.redo_btn.setEnabled(False)

        # Right: credit in an expanding widget, right-aligned
        right = QWidget()
        right.setObjectName("HeaderRight")
        right_layout = QHBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.credit_label = QLabel("built by rinbal")
        right_layout.addStretch()
        right_layout.addWidget(self.credit_label)

        layout.addWidget(left, 1)
        layout.addSpacing(8)
        layout.addWidget(self.undo_btn)
        layout.addSpacing(4)
        layout.addWidget(self.redo_btn)
        layout.addSpacing(8)
        layout.addWidget(right, 1)

        # Apply initial dark theme
        self.update_theme(True)

    def update_theme(self, is_dark):
        if is_dark:
            self.setStyleSheet("""
                QWidget {
                    background: #252526;
                    border-bottom: 1px solid #3C3C3C;
                }
                QCheckBox {
                    spacing: 6px; color: #CCCCCC; font-size: 12px;
                    background: transparent; border-radius: 4px; padding: 2px 6px;
                }
                QCheckBox:hover { background: rgba(255, 255, 255, 0.07); color: #FFFFFF; }
                QCheckBox::indicator {
                    width: 11px; height: 11px;
                    border: 1px solid #3C3C3C;
                    border-radius: 2px;
                    background: #2D2D30;
                }
                QCheckBox::indicator:checked { background: #FF8C00; border: 1px solid #FF8C00; }
                QCheckBox::indicator:unchecked { background: #2D2D30; border: 1px solid #3C3C3C; }
                QLabel { color: #CCCCCC; font-size: 12px; background: transparent; }
                #HeaderLeft, #HeaderRight { background: transparent; }
                QPushButton {
                    background: #2D2D30;
                    color: #D4D4D4;
                    border: 1px solid #3C3C3C;
                    border-radius: 13px;
                    font-size: 15px;
                }
                QPushButton:hover { background: #3C3C3C; }
                QPushButton:pressed { background: #1E1E1E; }
                QPushButton:disabled { background: #252526; color: #3C3C3C; border-color: #2D2D2D; }
            """)
            self.theme_checkbox.setChecked(True)
            self.credit_label.setStyleSheet("QLabel { font-size: 12px; font-style: italic; color: #858585; }")
        else:
            self.setStyleSheet("""
                QWidget {
                    background: #F8F8F8;
                    border-bottom: 1px solid #E1E1E1;
                }
                QCheckBox {
                    spacing: 6px; color: #333333; font-size: 12px;
                    background: transparent; border-radius: 4px; padding: 2px 6px;
                }
                QCheckBox:hover { background: rgba(0, 0, 0, 0.06); color: #000000; }
                QCheckBox::indicator {
                    width: 11px; height: 11px;
                    border: 1px solid #666666;
                    border-radius: 2px;
                    background: #FFFFFF;
                }
                QCheckBox::indicator:checked { background: #FF8C00; border: 1px solid #FF8C00; }
                QCheckBox::indicator:unchecked { background: #FFFFFF; border: 1px solid #666666; }
                QLabel { color: #333333; font-size: 12px; background: transparent; }
                #HeaderLeft, #HeaderRight { background: transparent; }
                QPushButton {
                    background: #ECECEC;
                    color: #333333;
                    border: 1px solid #CCCCCC;
                    border-radius: 13px;
                    font-size: 15px;
                }
                QPushButton:hover { background: #E1E1E1; }
                QPushButton:pressed { background: #D0D0D0; }
                QPushButton:disabled { background: #F8F8F8; color: #CCCCCC; border-color: #EBEBEB; }
            """)
            self.theme_checkbox.setChecked(False)
            self.credit_label.setStyleSheet("QLabel { font-size: 12px; font-style: italic; color: #555555; }")


class FileChangedBar(QWidget):
    """Notification bar shown at the top of a tab when the file was changed externally."""

    reload_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FileChangedBar")
        self.is_dark = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(10)

        self._icon = QLabel("⚠")
        self._icon.setFixedWidth(18)
        layout.addWidget(self._icon)

        self._text = QLabel()
        self._reload_btn = QPushButton("Reload")
        self._reload_btn.setFixedHeight(26)
        self._reload_btn.clicked.connect(self.reload_requested)

        self._dismiss_btn = QPushButton("×")
        self._dismiss_btn.setFixedSize(26, 26)
        self._dismiss_btn.setToolTip("Dismiss")
        self._dismiss_btn.clicked.connect(self._on_dismiss)

        layout.addWidget(self._text, 1)
        layout.addWidget(self._reload_btn)
        layout.addWidget(self._dismiss_btn)

        self._update_theme()
        self.hide()

    def show_changed(self, has_unsaved: bool):
        self._text.setText("File was changed externally.")
        self._reload_btn.setText("Discard my changes and reload" if has_unsaved else "Reload")
        self._reload_btn.show()
        self.show()

    def show_deleted(self):
        self._text.setText("File was deleted - save to recreate it.")
        self._reload_btn.hide()
        self.show()

    def show_already_open(self):
        self._text.setText("This file is already open in this tab.")
        self._reload_btn.hide()
        self.show()

    def _on_dismiss(self):
        self.hide()
        self.dismissed.emit()

    def update_theme(self, is_dark: bool):
        self.is_dark = is_dark
        self._update_theme()

    def _update_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
                #FileChangedBar {
                    background: #3C3000;
                    border-bottom: 1px solid #5C4A00;
                }
                QLabel { color: #FFD080; background: transparent; font-size: 12px; font-weight: bold; }
                QPushButton {
                    background: #5C4A00;
                    color: #FFD080;
                    border: 1px solid #7A6200;
                    padding: 3px 10px;
                    border-radius: 4px;
                    font-size: 12px;
                }
                QPushButton:hover { background: #7A6200; }
            """)
        else:
            self.setStyleSheet("""
                #FileChangedBar {
                    background: #FFF8DC;
                    border-bottom: 1px solid #D4A800;
                }
                QLabel { color: #7A5000; background: transparent; font-size: 12px; font-weight: bold; }
                QPushButton {
                    background: #F0D060;
                    color: #4A3000;
                    border: 1px solid #C0A000;
                    padding: 3px 10px;
                    border-radius: 4px;
                    font-size: 12px;
                }
                QPushButton:hover { background: #D4B840; }
            """)
