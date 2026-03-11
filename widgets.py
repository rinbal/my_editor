#!/usr/bin/env python3


from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QPainter, QFont, QColor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLineEdit, QLabel, QPushButton, QFrame, QMenu, QCheckBox
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

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self.label = QLabel("Search:")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search text…")
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

        lay.addWidget(self.label)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.btn_prev)
        lay.addWidget(self.btn_next)
        lay.addWidget(self.match_info)
        lay.addWidget(self.btn_close)

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

    def focusIn(self):
        self.edit.setFocus()
        self.edit.selectAll()


class HeaderWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        # Left: theme + line numbers toggles
        self.theme_checkbox = QCheckBox("Dark Theme")
        self.theme_checkbox.setToolTip("Enable Dark Theme (unchecked = Light Theme)")
        self.theme_checkbox.setChecked(True)

        layout.addWidget(self.theme_checkbox)
        layout.addSpacing(8)

        self.line_numbers_checkbox = QCheckBox("Line Numbers")
        self.line_numbers_checkbox.setToolTip("Toggle Line Numbers")

        layout.addWidget(self.line_numbers_checkbox)
        layout.addSpacing(8)

        self.syntax_highlight_checkbox = QCheckBox("Syntax Highlighting")
        self.syntax_highlight_checkbox.setToolTip("Toggle Syntax Highlighting")
        self.syntax_highlight_checkbox.setChecked(True)

        layout.addWidget(self.syntax_highlight_checkbox)

        # Center: undo / redo round arrows
        layout.addStretch(1)

        self.undo_btn = QPushButton("↺")
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.undo_btn.setFixedSize(26, 26)
        self.undo_btn.setEnabled(False)

        self.redo_btn = QPushButton("↻")
        self.redo_btn.setToolTip("Redo (Ctrl+Y / Ctrl+Shift+Z)")
        self.redo_btn.setFixedSize(26, 26)
        self.redo_btn.setEnabled(False)

        layout.addWidget(self.undo_btn)
        layout.addSpacing(4)
        layout.addWidget(self.redo_btn)

        layout.addStretch(1)

        # Right: credit
        self.credit_label = QLabel("created by rinbal")
        layout.addWidget(self.credit_label)

        # Apply initial dark theme
        self.update_theme(True)

    def update_theme(self, is_dark):
        if is_dark:
            self.setStyleSheet("""
                QWidget {
                    background: #252526;
                    border-bottom: 1px solid #3C3C3C;
                }
                QCheckBox { spacing: 6px; color: #CCCCCC; font-size: 12px; }
                QCheckBox::indicator {
                    width: 14px; height: 14px;
                    border: 1px solid #3C3C3C;
                    border-radius: 2px;
                    background: #2D2D30;
                }
                QCheckBox::indicator:checked { background: #FF8C00; border: 1px solid #FF8C00; }
                QCheckBox::indicator:unchecked { background: #2D2D30; border: 1px solid #3C3C3C; }
                QLabel { color: #CCCCCC; font-size: 12px; background: transparent; }
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
                QCheckBox { spacing: 6px; color: #333333; font-size: 12px; }
                QCheckBox::indicator {
                    width: 14px; height: 14px;
                    border: 1px solid #666666;
                    border-radius: 2px;
                    background: #FFFFFF;
                }
                QCheckBox::indicator:checked { background: #FF8C00; border: 1px solid #FF8C00; }
                QCheckBox::indicator:unchecked { background: #FFFFFF; border: 1px solid #666666; }
                QLabel { color: #333333; font-size: 12px; background: transparent; }
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
