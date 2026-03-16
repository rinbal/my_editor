#!/usr/bin/env python3
"""
HTML Editor widget with bullet and format logic.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QTextCursor, QTextCharFormat, QColor, QClipboard
from PySide6.QtWidgets import QTextEdit, QMenu, QApplication
from constants import DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, DARK_SELECTION, LIGHT_SELECTION, MONO_FONT, TEXT_COLORS


class HtmlEditor(QTextEdit):
    """QTextEdit with bullet and format logic."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setUndoRedoEnabled(True)

        self.setStyleSheet(f"""
            QTextEdit {{
                background: {DARK_BG};
                color: {DARK_FG};
                border: none;
                selection-background-color: {DARK_SELECTION};
                font-family: {MONO_FONT};
                font-size: 14px;
                line-height: 1.5;
                padding: 8px;
            }}
        """)

        # Track active formatting state for persistent formatting
        self.active_format = {
            'bold': False,
            'italic': False,
            'underline': False,
            'color': None
        }

        # Connect to cursor position changes to update active format
        self.cursorPositionChanged.connect(self._update_active_format)

        # Block cursor (terminal-style): hide Qt's thin cursor, draw our own
        self.setCursorWidth(0)
        self._cursor_visible = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(530)
        self._blink_timer.timeout.connect(self._on_cursor_blink)
        self._blink_timer.start()

    def undo(self):
        self.document().undo()

    def redo(self):
        self.document().redo()

    # -------- Format Toggles --------
    def toggle_bold(self):
        cursor = self.textCursor()
        current = cursor.charFormat() if cursor.hasSelection() else self.currentCharFormat()
        new_weight = 400 if current.fontWeight() > 400 else 700
        fmt = QTextCharFormat()
        fmt.setFontWeight(new_weight)
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            self.mergeCurrentCharFormat(fmt)
        self.active_format['bold'] = (new_weight > 400)

    def toggle_italic(self):
        cursor = self.textCursor()
        current = cursor.charFormat() if cursor.hasSelection() else self.currentCharFormat()
        new_italic = not current.fontItalic()
        fmt = QTextCharFormat()
        fmt.setFontItalic(new_italic)
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            self.mergeCurrentCharFormat(fmt)
        self.active_format['italic'] = new_italic

    def toggle_underline(self):
        cursor = self.textCursor()
        current = cursor.charFormat() if cursor.hasSelection() else self.currentCharFormat()
        new_underline = not current.fontUnderline()
        fmt = QTextCharFormat()
        fmt.setFontUnderline(new_underline)
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            self.mergeCurrentCharFormat(fmt)
        self.active_format['underline'] = new_underline

    def apply_color(self, qcolor: QColor | None):
        cursor = self.textCursor()
        if qcolor is None:
            if cursor.hasSelection():
                # mergeCharFormat can only set properties, not clear them.
                # Must iterate character by character to actually remove the color.
                start, end = cursor.selectionStart(), cursor.selectionEnd()
                c = QTextCursor(self.document())
                c.beginEditBlock()
                c.setPosition(start)
                while c.position() < end:
                    c.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
                    char_fmt = c.charFormat()
                    char_fmt.clearForeground()
                    c.setCharFormat(char_fmt)
                    c.setPosition(c.position())
                c.endEditBlock()
            else:
                char_fmt = self.currentCharFormat()
                char_fmt.clearForeground()
                self.setCurrentCharFormat(char_fmt)
        else:
            fmt = QTextCharFormat()
            fmt.setForeground(qcolor)
            if cursor.hasSelection():
                cursor.mergeCharFormat(fmt)
            else:
                self.mergeCurrentCharFormat(fmt)
        self.active_format['color'] = qcolor
        self.ensureCursorVisible()

    def reset_to_default(self):
        """Reset text formatting to default (no bold, italic, underline, color)."""
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontWeight(400)
        fmt.setFontItalic(False)
        fmt.setFontUnderline(False)
        fmt.clearForeground()

        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            self.mergeCurrentCharFormat(fmt)

        self.active_format['bold'] = False
        self.active_format['italic'] = False
        self.active_format['underline'] = False
        self.active_format['color'] = None
        self.ensureCursorVisible()

    def _update_active_format(self):
        """Update active format state based on current cursor position."""
        fmt = self.currentCharFormat()
        self.active_format['bold'] = (fmt.fontWeight() > 400)
        self.active_format['italic'] = fmt.fontItalic()
        self.active_format['underline'] = fmt.fontUnderline()
        self.active_format['color'] = fmt.foreground().color() if fmt.hasProperty(QTextCharFormat.ForegroundBrush) else None

    # -------- Block cursor --------
    def _block_cursor_rect(self):
        """Return the rect the block cursor occupies (used for painting and invalidation)."""
        rect = self.cursorRect()
        rect.setWidth(max(self.fontMetrics().averageCharWidth(), 10))
        return rect

    def _on_cursor_blink(self):
        self._cursor_visible = not self._cursor_visible
        self.viewport().update(self._block_cursor_rect())

    def paintEvent(self, event):
        super().paintEvent(event)
        cursor = self.textCursor()
        if not self.hasFocus() or not self._cursor_visible or cursor.hasSelection():
            return
        rect = self._block_cursor_rect()
        is_dark = hasattr(self, '_theme_colors') and self._theme_colors['bg'] == DARK_BG
        color = QColor(212, 212, 212, 210) if is_dark else QColor(51, 51, 51, 210)
        painter = QPainter(self.viewport())
        painter.fillRect(rect, color)
        painter.end()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._cursor_visible = True
        self._blink_timer.start()
        self.viewport().update(self._block_cursor_rect())

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._cursor_visible = False
        self._blink_timer.stop()
        self.viewport().update(self._block_cursor_rect())

    # -------- Context menu with colors --------
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        is_dark = hasattr(self, '_theme_colors') and self._theme_colors['bg'] == DARK_BG
        if is_dark:
            menu.setStyleSheet("""
                QMenu {
                    background: #252526;
                    color: #CCCCCC;
                    border: 1px solid #3C3C3C;
                    padding: 4px;
                }
                QMenu::item { padding: 4px 20px 4px 30px; }
                QMenu::item:selected { background: #1E1E1E; color: #FFFFFF; }
                QMenu::item:disabled { color: #6A6A6A; }
                QMenu::separator { height: 1px; background: #3C3C3C; margin: 4px 0px; }
            """)
        else:
            menu.setStyleSheet("""
                QMenu {
                    background: #F8F8F8;
                    color: #333333;
                    border: 1px solid #E1E1E1;
                    padding: 4px;
                }
                QMenu::item { padding: 4px 20px 4px 30px; }
                QMenu::item:selected { background: #F3F3F3; color: #000000; }
                QMenu::item:disabled { color: #999999; }
                QMenu::separator { height: 1px; background: #E1E1E1; margin: 4px 0px; }
            """)

        act_copy = menu.addAction("Copy")
        act_cut = menu.addAction("Cut")
        act_paste = menu.addAction("Paste")
        menu.addSeparator()
        fmt_menu = menu.addMenu("Color")
        for name, col in TEXT_COLORS.items():
            a = fmt_menu.addAction(name)
            a.setData(("color", col))
        fmt_menu.addSeparator()
        a_clear = fmt_menu.addAction("Remove Color")
        a_clear.setData(("color", None))
        menu.addSeparator()
        act_b = menu.addAction("Bold (Ctrl+B)")
        act_i = menu.addAction("Italic (Ctrl+I)")
        act_u = menu.addAction("Underline (Ctrl+U)")
        menu.addSeparator()
        act_reset = menu.addAction("Reset Format (Ctrl+D)")

        chosen = menu.exec(event.globalPos())
        if not chosen:
            return
        if chosen == act_copy:
            self.copy()
        elif chosen == act_cut:
            self.cut()
        elif chosen == act_paste:
            self.paste_normalized()
        elif chosen == act_b:
            self.toggle_bold()
        elif chosen == act_i:
            self.toggle_italic()
        elif chosen == act_u:
            self.toggle_underline()
        elif chosen == act_reset:
            self.reset_to_default()
        else:
            data = chosen.data()
            if isinstance(data, tuple) and data and data[0] == "color":
                self.apply_color(data[1])

    # -------- Bullet Logic (•) --------
    @staticmethod
    def _line_info(cursor: QTextCursor):
        """Get current line text + start position of the line."""
        c = QTextCursor(cursor)
        c.movePosition(QTextCursor.StartOfLine, QTextCursor.MoveAnchor)
        start_pos = c.position()
        c.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
        line_text = c.selectedText()
        return line_text, start_pos

    def _indent_level_and_has_bullet(self, line: str):
        spaces = 0
        i = 0
        while i < len(line) and line[i] == ' ':
            spaces += 1
            i += 1
        has_bullet = line[i:i+2] == "• "
        return spaces, has_bullet

    def _set_line_text(self, start_pos: int, new_text: str, cursor_pos_after=None):
        c = self.textCursor()
        c.setPosition(start_pos)
        c.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
        c.beginEditBlock()
        c.removeSelectedText()
        c.insertText(new_text)
        c.endEditBlock()

        if cursor_pos_after is None:
            cursor_pos_after = start_pos + len(new_text)

        nc = self.textCursor()
        nc.setPosition(cursor_pos_after)
        self.setTextCursor(nc)

    def keyPressEvent(self, e):
        # Keep cursor solid immediately after a keypress; blink restarts from now
        old_cursor_rect = self._block_cursor_rect()
        self._cursor_visible = True
        self._blink_timer.start()
        self.viewport().update(old_cursor_rect)

        # Undo / Redo
        if e.key() == Qt.Key_Z and e.modifiers() == Qt.ControlModifier:
            self.undo()
            return
        if (e.key() == Qt.Key_Z and e.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier)) or \
           (e.key() == Qt.Key_Y and e.modifiers() == Qt.ControlModifier):
            self.redo()
            return

        # Ctrl+V — normalized paste
        if e.key() == Qt.Key_V and e.modifiers() == Qt.ControlModifier:
            self.paste_normalized()
            return

        c = self.textCursor()
        line, start = self._line_info(c)
        spaces, has_bullet = self._indent_level_and_has_bullet(line)

        if e.key() == Qt.Key_Tab:
            if c.atBlockStart() or (c.positionInBlock() <= spaces + (2 if has_bullet else 0)):
                tab_width = 4
                new_spaces = spaces + tab_width
                new_line = " " * new_spaces
                if not has_bullet:
                    new_line += "• "
                    new_line += line.lstrip()
                else:
                    new_line += line[spaces:]
                bullet_pos = new_line.find("• ")
                move_to = start + bullet_pos + 2 if bullet_pos >= 0 else start + len(new_line.rstrip())
                self._set_line_text(start, new_line, move_to)
                self._apply_active_format_to_cursor()
                self.ensureCursorVisible()
                return

        elif e.key() == Qt.Key_Backtab:
            if c.atBlockStart() or (c.positionInBlock() <= spaces + (2 if has_bullet else 0)):
                tab_width = 4
                new_spaces = max(0, spaces - tab_width)
                new_line = " " * new_spaces + line[spaces:]
                bullet_pos = new_line.find("• ")
                move_to = start + bullet_pos + 2 if bullet_pos >= 0 else start + len(new_line.rstrip())
                self._set_line_text(start, new_line, move_to)
                self.ensureCursorVisible()
                return

        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if has_bullet:
                content_after_bullet = line[spaces + 2:].strip()
                cursor_pos_in_block = c.positionInBlock()
                bullet_end_pos = spaces + 2
                is_empty_bullet = (content_after_bullet == "") and (cursor_pos_in_block <= bullet_end_pos)

                if is_empty_bullet:
                    # Double Enter — remove bullet, start plain line
                    c.beginEditBlock()
                    c.movePosition(QTextCursor.StartOfBlock)
                    c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                    c.removeSelectedText()
                    c.insertText("\n")
                    c = self.textCursor()
                    c.movePosition(QTextCursor.StartOfBlock)
                    fmt = QTextCharFormat()
                    fmt.setFontWeight(400)
                    fmt.setFontItalic(False)
                    fmt.setFontUnderline(False)
                    fmt.clearForeground()
                    c.setCharFormat(fmt)
                    self.mergeCurrentCharFormat(fmt)
                    self.active_format.update({'bold': False, 'italic': False, 'underline': False, 'color': None})
                    c.endEditBlock()
                    self.setTextCursor(c)
                    self.ensureCursorVisible()
                    return
                else:
                    # Continue bullet on next line
                    c.beginEditBlock()
                    c.insertText("\n" + " " * spaces + "• ")
                    c = self.textCursor()
                    self._apply_active_format_to_cursor()
                    c.endEditBlock()
                    self.setTextCursor(c)
                    self.ensureCursorVisible()
                    return
            else:
                super().keyPressEvent(e)
                self._apply_active_format_to_cursor()
                self.ensureCursorVisible()
                return

        elif e.key() == Qt.Key_Backspace:
            if not c.hasSelection() and c.positionInBlock() <= spaces + (2 if has_bullet else 0) and (spaces > 0 or has_bullet):
                if has_bullet:
                    if spaces == 0 and c.blockNumber() > 0:
                        # No indentation, not first block: remove bullet and merge with previous line
                        cursor = self.textCursor()
                        cursor.beginEditBlock()
                        cursor.setPosition(start)
                        cursor.setPosition(start + 2, QTextCursor.KeepAnchor)
                        cursor.removeSelectedText()
                        cursor.deletePreviousChar()  # delete preceding newline to merge blocks
                        cursor.endEditBlock()
                        self.setTextCursor(cursor)
                    else:
                        # Indented bullet, or first block: remove bullet only, cursor stays at content start
                        new_line = " " * spaces + line[spaces + 2:]
                        self._set_line_text(start, new_line, start + spaces)
                else:
                    new_spaces = max(0, spaces - 2)
                    new_line = " " * new_spaces + line[spaces:]
                    self._set_line_text(start, new_line, start + new_spaces)
                self.ensureCursorVisible()
                return
            else:
                # Regular backspace — one character per undo step
                cursor = self.textCursor()
                cursor.beginEditBlock()
                if cursor.hasSelection():
                    cursor.removeSelectedText()
                else:
                    cursor.deletePreviousChar()
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                self.ensureCursorVisible()
                return

        elif e.key() == Qt.Key_Delete:
            # Delete key — one character per undo step
            cursor = self.textCursor()
            cursor.beginEditBlock()
            if cursor.hasSelection():
                cursor.removeSelectedText()
            else:
                cursor.deleteChar()
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
            return

        # Regular printable character — one character per undo step.
        # Using beginEditBlock/endEditBlock prevents Qt from merging consecutive insertions.
        if e.text() and not e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            cursor = self.textCursor()
            fmt = self.currentCharFormat()
            cursor.beginEditBlock()
            if cursor.hasSelection():
                cursor.removeSelectedText()
            cursor.insertText(e.text(), fmt)
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
            return

        super().keyPressEvent(e)
        self.ensureCursorVisible()

    def _apply_active_format_to_cursor(self):
        """Apply the active formatting state to the current cursor position."""
        fmt = QTextCharFormat()
        if self.active_format['bold']:
            fmt.setFontWeight(700)
        if self.active_format['italic']:
            fmt.setFontItalic(True)
        if self.active_format['underline']:
            fmt.setFontUnderline(True)
        if self.active_format['color']:
            fmt.setForeground(self.active_format['color'])
        self.mergeCurrentCharFormat(fmt)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            main = self.window()
            if hasattr(main, '_handle_dropped_urls'):
                main._handle_dropped_urls(event.mimeData().urls())
            event.acceptProposedAction()
        elif event.mimeData().hasText():
            cursor = self.cursorForPosition(event.position().toPoint())
            self.setTextCursor(cursor)
            fmt = QTextCharFormat()
            fmt.setFontWeight(400)
            fmt.setFontItalic(False)
            fmt.setFontUnderline(False)
            fmt.clearForeground()
            cursor.insertText(event.mimeData().text(), fmt)
            self.setTextCursor(cursor)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def paste_normalized(self):
        """Paste plain text with default formatting (no external styles, no baked colors)."""
        plain_text = QApplication.clipboard().text()
        if not plain_text:
            return

        cursor = self.textCursor()
        if cursor.hasSelection():
            cursor.removeSelectedText()

        fmt = QTextCharFormat()
        fmt.setFontWeight(400)
        fmt.setFontItalic(False)
        fmt.setFontUnderline(False)
        fmt.clearForeground()

        cursor.insertText(plain_text, fmt)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
