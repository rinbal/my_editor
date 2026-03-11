#!/usr/bin/env python3


import os
from PySide6.QtCore import Qt, QMarginsF
from PySide6.QtGui import (
    QAction, QKeySequence, QTextCursor, QTextDocument, QTextCharFormat, QColor,
    QPageLayout, QPageSize
)
from PySide6.QtWidgets import (
    QMainWindow, QFileDialog, QMessageBox, QWidget, QVBoxLayout,
    QTextEdit, QTabWidget, QToolButton, QHBoxLayout, QStatusBar, QPushButton, QTabBar
)

from constants import (
    DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, DARK_SELECTION, LIGHT_SELECTION,
    DARK_MENU_BG, DARK_MENU_FG, LIGHT_MENU_BG, LIGHT_MENU_FG,
    DARK_BORDER, LIGHT_BORDER, MONO_FONT
)
from widgets import FindBar, HeaderWidget, LineNumberGutter
from editor import HtmlEditor
from recovery import EditorBackup, find_all_backups


class MainWindow(QMainWindow):
    def __init__(self, initial_path: str | None = None):
        super().__init__()
        self.setWindowTitle("")
        self.resize(1100, 720)

        self.is_dark_theme = True
        self.show_line_numbers = False

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(False)
        self.tabs.tabBar().setExpanding(False)

        self.plus_btn = QToolButton()
        self.plus_btn.setText("+")
        self.plus_btn.setAutoRaise(True)
        self.plus_btn.clicked.connect(self.new_tab)
        self.tabs.setCornerWidget(self.plus_btn, Qt.TopRightCorner)

        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self._apply_theme()

        self.header_widget = HeaderWidget()
        self.header_widget.theme_checkbox.toggled.connect(self._toggle_theme)
        self.header_widget.line_numbers_checkbox.toggled.connect(self._toggle_line_numbers)
        self.header_widget.undo_btn.clicked.connect(self._undo)
        self.header_widget.redo_btn.clicked.connect(self._redo)

        self._build_actions()
        self._build_menu()
        self._build_findbar()

        if initial_path and os.path.isfile(initial_path):
            self.open_path(initial_path)

        restored = self._restore_backups()
        if not initial_path and not restored:
            self.new_tab()

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._update_undo_redo_buttons()


    # ----------------------------------------------------------------------
    # THEME APPLICATION
    # ----------------------------------------------------------------------
    def _apply_theme(self):
        bg = DARK_BG if self.is_dark_theme else LIGHT_BG
        fg = DARK_FG if self.is_dark_theme else LIGHT_FG
        menu_bg = DARK_MENU_BG if self.is_dark_theme else LIGHT_MENU_BG
        menu_fg = DARK_MENU_FG if self.is_dark_theme else LIGHT_MENU_FG
        border = DARK_BORDER if self.is_dark_theme else LIGHT_BORDER

        self.setStyleSheet(f"""
            QMainWindow {{ background: {bg}; }}
            QTabWidget::pane {{
                border-top: 1px solid {border};
                background: {bg};
            }}
            QTabBar {{
                background: {menu_bg};
            }}
            QTabBar::tab {{
                background: {menu_bg};
                color: {menu_fg};
                padding: 8px 16px;
                border: 1px solid {border};
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {bg};
                border-bottom: 1px solid {bg};
            }}
            QTabBar::tab:hover {{ background: {menu_bg}; }}
            QStatusBar {{
                background: {menu_bg};
                color: {menu_fg};
                border-top: 1px solid {border};
            }}
            QMenuBar {{
                background: {menu_bg};
                color: {menu_fg};
                border-bottom: 1px solid {border};
            }}
            QMenuBar::item:selected {{ background: {bg}; }}
            QMenu {{
                background: {menu_bg};
                color: {menu_fg};
                border: 1px solid {border};
            }}
            QMenu::item:selected {{ background: {bg}; }}
            QMenu::item:disabled {{ color: {border}; }}
            QScrollBar:vertical {{
                background: {menu_bg};
                width: 12px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {fg}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar:horizontal {{
                background: {menu_bg};
                height: 12px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {border};
                min-width: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: {fg}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        """)

        if self.is_dark_theme:
            self.plus_btn.setStyleSheet("""
                QToolButton {
                    background: #2D2D30;
                    color: #CCCCCC;
                    border: 1px solid #3C3C3C;
                    border-radius: 4px;
                    font-size: 14px;
                    font-weight: bold;
                    min-width: 20px;
                    min-height: 20px;
                }
                QToolButton:hover { background: #3C3C3C; }
            """)
        else:
            self.plus_btn.setStyleSheet("""
                QToolButton {
                    background: #DCDCDC;
                    color: #222222;
                    border: 1px solid #BBBBBB;
                    border-radius: 4px;
                    font-size: 14px;
                    font-weight: bold;
                    min-width: 20px;
                    min-height: 20px;
                }
                QToolButton:hover { background: #C8C8C8; }
            """)

        for i in range(self.tabs.count()):
            container = self.tabs.widget(i)
            if isinstance(container, QWidget):
                for child in container.children():
                    if isinstance(child, HtmlEditor):
                        self._update_editor_theme(child)
                        break


    def _update_editor_theme(self, editor):
        bg = DARK_BG if self.is_dark_theme else LIGHT_BG
        fg = DARK_FG if self.is_dark_theme else LIGHT_FG
        selection = DARK_SELECTION if self.is_dark_theme else LIGHT_SELECTION
        menu_bg = DARK_MENU_BG if self.is_dark_theme else LIGHT_MENU_BG
        border = DARK_BORDER if self.is_dark_theme else LIGHT_BORDER

        editor.setStyleSheet(f"""
            QTextEdit {{
                background: {bg};
                color: {fg};
                border: none;
                selection-background-color: {selection};
                selection-color: {fg};
                font-family: {MONO_FONT};
                font-size: 14px;
                line-height: 1.5;
                padding: 8px;
            }}
            QScrollBar:vertical {{
                background: {menu_bg};
                width: 12px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {fg}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar:horizontal {{
                background: {menu_bg};
                height: 12px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {border};
                min-width: 20px;
                border-radius: 6px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: {fg}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        """)

        editor._theme_colors = {'bg': bg, 'fg': fg}

        if hasattr(editor, '_line_gutter'):
            editor._line_gutter._update_theme()


    # ----------------------------------------------------------------------
    # EDITOR / TAB HELPERS
    # ----------------------------------------------------------------------
    def current_editor(self) -> HtmlEditor | None:
        w = self.tabs.currentWidget()
        if isinstance(w, HtmlEditor):
            return w
        elif isinstance(w, QWidget):
            for child in w.children():
                if isinstance(child, HtmlEditor):
                    return child
        return None

    def current_path(self) -> str | None:
        ed = self.current_editor()
        return getattr(ed, "_file_path", None) if ed else None

    def set_current_path(self, path: str | None):
        ed = self.current_editor()
        if ed is not None:
            ed._file_path = path
            self._update_tab_title()

    def _update_tab_title(self):
        ed = self.current_editor()
        if not ed:
            return
        path = getattr(ed, "_file_path", None)
        dirty = "*" if ed.document().isModified() else ""
        title = (os.path.basename(path) if path else "Untitled") + dirty
        self.tabs.setTabText(self.tabs.currentIndex(), title)
        self._update_status_bar()

    def _update_status_bar(self):
        ed = self.current_editor()
        if not ed:
            return
        path = getattr(ed, "_file_path", None)
        file_info = path if path else "(Untitled)"
        page_count = ed.document().pageCount()
        if page_count > 1:
            self.status.showMessage(f"{file_info} | Page {page_count}")
        else:
            self.status.showMessage(file_info)

    def _update_window_title(self, *_):
        self.setWindowTitle("")

    def _on_tab_changed(self, index=None):
        self._update_window_title()
        self._update_undo_redo_buttons()

    def _update_undo_redo_buttons(self):
        ed = self.current_editor()
        can_undo = ed.document().isUndoAvailable() if ed else False
        can_redo = ed.document().isRedoAvailable() if ed else False
        self.header_widget.undo_btn.setEnabled(can_undo)
        self.header_widget.redo_btn.setEnabled(can_redo)


    # ----------------------------------------------------------------------
    # ACTIONS / MENU
    # ----------------------------------------------------------------------
    def _build_actions(self):
        self.act_new = QAction("New", self)
        self.act_new.setShortcut(QKeySequence.New)
        self.act_new.triggered.connect(self.new_tab)

        self.act_open = QAction("Open…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.open_dialog)

        self.act_save = QAction("Save", self)
        self.act_save.setShortcut(QKeySequence.Save)
        self.act_save.triggered.connect(self.save)

        self.act_save_as = QAction("Save As…", self)
        self.act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.act_save_as.triggered.connect(self.save_as)

        self.act_close_tab = QAction("Close Tab", self)
        self.act_close_tab.setShortcut(QKeySequence("Ctrl+W"))
        self.act_close_tab.triggered.connect(self._close_current_tab)

        self.act_quit = QAction("Quit", self)
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self._quit_application)

        # Formatting
        self.act_bold = QAction("Bold", self)
        self.act_bold.setShortcut(QKeySequence("Ctrl+B"))
        self.act_bold.triggered.connect(self._fmt_bold)

        self.act_italic = QAction("Italic", self)
        self.act_italic.setShortcut(QKeySequence("Ctrl+I"))
        self.act_italic.triggered.connect(self._fmt_italic)

        self.act_underline = QAction("Underline", self)
        self.act_underline.setShortcut(QKeySequence("Ctrl+U"))
        self.act_underline.triggered.connect(self._fmt_underline)

        self.act_reset_format = QAction("Reset Format", self)
        self.act_reset_format.setShortcut(QKeySequence("Ctrl+D"))
        self.act_reset_format.triggered.connect(self._reset_format)

        # Search
        self.act_find = QAction("Find", self)
        self.act_find.setShortcut(QKeySequence("Ctrl+F"))
        self.act_find.triggered.connect(self._toggle_findbar)

        self.act_find_next = QAction("Find Next", self)
        self.act_find_next.setShortcut(QKeySequence("F3"))
        self.act_find_next.triggered.connect(self._find_next)

        self.act_find_prev = QAction("Find Previous", self)
        self.act_find_prev.setShortcut(QKeySequence("Shift+F3"))
        self.act_find_prev.triggered.connect(self._find_prev)

        self._search_matches = []
        self._current_match_index = -1
        self._last_search_text = ""
        self._search_extra_selections = []

        # Theme + line numbers
        self.act_toggle_theme = QAction("Toggle Dark/Light Theme", self)
        self.act_toggle_theme.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.act_toggle_theme.triggered.connect(self._toggle_theme)

        self.act_toggle_line_numbers = QAction("Show Line Numbers", self)
        self.act_toggle_line_numbers.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.act_toggle_line_numbers.triggered.connect(self._toggle_line_numbers)
        self.act_toggle_line_numbers.setCheckable(True)

        self.addAction(self.act_bold)
        self.addAction(self.act_italic)
        self.addAction(self.act_underline)
        self.addAction(self.act_reset_format)

    def _build_menu(self):
        m_file = self.menuBar().addMenu("&File")
        m_file.addAction(self.act_new)
        m_file.addAction(self.act_open)
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        m_file.addSeparator()
        m_file.addAction(self.act_close_tab)
        m_file.addAction(self.act_quit)

        m_find = self.menuBar().addMenu("&Search")
        m_find.addAction(self.act_find)
        m_find.addAction(self.act_find_next)
        m_find.addAction(self.act_find_prev)

        help_menu = self.menuBar().addMenu("&Help")
        shortcuts_action = QAction("Keyboard Shortcuts", self)
        shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_action)

    def _build_findbar(self):
        self.findbar = FindBar(self._find_next, self._find_prev, self._toggle_findbar, self)
        self.findbar.setVisible(False)
        self.findbar.edit.textChanged.connect(self._on_search_text_changed)
        wrapper = QWidget()
        v = QVBoxLayout(wrapper)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.header_widget, 0)
        v.addWidget(self.findbar, 0)
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(wrapper)


    # ----------------------------------------------------------------------
    # CRASH RECOVERY
    # ----------------------------------------------------------------------
    def _restore_backups(self) -> bool:
        """Silently restore any backup files left over from a previous crash.

        Returns True if at least one backup was restored.
        """
        backups = find_all_backups()
        for backup in backups:
            original_path = backup.get("original_path")
            content = backup.get("content", "")
            backup_file = backup["_backup_file"]

            ed = HtmlEditor()
            ed.setPlainText(content)
            ed._file_path = original_path
            ed.document().setModified(True)
            ed.document().contentsChanged.connect(self._update_tab_title)
            ed.document().undoAvailable.connect(self._update_undo_redo_buttons)
            ed.document().redoAvailable.connect(self._update_undo_redo_buttons)
            self._update_editor_theme(ed)

            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            if self.show_line_numbers:
                gutter = LineNumberGutter(ed)
                layout.addWidget(gutter)
                ed._line_gutter = gutter

            layout.addWidget(ed)

            base_name = os.path.basename(original_path) if original_path else "Untitled"
            idx = self.tabs.addTab(container, f"{base_name} (recovered)*")
            self._attach_close_button(idx, container)

            # Replace the old backup file with a fresh one for the restored content
            os.remove(backup_file)
            ed._backup = EditorBackup(ed, original_path)
            ed._backup.write_now()

        return len(backups) > 0

    # ----------------------------------------------------------------------
    # FILE I/O
    # ----------------------------------------------------------------------
    def new_tab(self):
        ed = HtmlEditor()
        ed.document().contentsChanged.connect(self._update_tab_title)
        ed.document().undoAvailable.connect(self._update_undo_redo_buttons)
        ed.document().redoAvailable.connect(self._update_undo_redo_buttons)
        self._update_editor_theme(ed)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self.show_line_numbers:
            gutter = LineNumberGutter(ed)
            layout.addWidget(gutter)
            ed._line_gutter = gutter

        layout.addWidget(ed)

        idx = self.tabs.addTab(container, "Untitled*")
        self.tabs.setCurrentIndex(idx)

        self._attach_close_button(idx, container)

        ed._file_path = None
        ed.setHtml("<div></div>")
        ed._backup = EditorBackup(ed, None)
        ed.setFocus()
        self._update_window_title()

    def _attach_close_button(self, idx: int, container: QWidget):
        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet("""
            QPushButton {
                color: #A84444;
                background: transparent;
                border: none;
                border-radius: 4px;
                font-size: 18px;
                padding: 0px;
            }
            QPushButton:hover {
                color: #D06060;
                background: rgba(168, 68, 68, 0.15);
            }
            QPushButton:pressed {
                background: rgba(168, 68, 68, 0.30);
            }
        """)
        close_btn.setToolTip("Close tab")
        close_btn.clicked.connect(lambda: self.close_tab(self.tabs.indexOf(container)))
        self.tabs.tabBar().setTabButton(idx, QTabBar.RightSide, close_btn)

    def close_tab(self, index: int):
        w = self.tabs.widget(index)
        editor = self._editor_from_widget(w)

        if editor and editor.document().isModified():
            file_name = os.path.basename(editor._file_path) if getattr(editor, '_file_path', None) else "Untitled"
            r = QMessageBox.question(
                self, "Save Changes?",
                f"The document '{file_name}' has unsaved changes.\n\nDo you want to save before closing?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes
            )
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                self.tabs.setCurrentIndex(index)
                if not self.save():
                    return
        if editor and hasattr(editor, '_backup'):
            editor._backup.delete()
        self.tabs.removeTab(index)

    def _close_current_tab(self):
        idx = self.tabs.currentIndex()
        if idx >= 0:
            self.close_tab(idx)

    def _quit_application(self):
        unsaved = []
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and ed.document().isModified():
                unsaved.append(os.path.basename(ed._file_path) if getattr(ed, '_file_path', None) else "Untitled")

        if unsaved:
            msg = (f"The document '{unsaved[0]}' has unsaved changes.\n\nDo you want to save before quitting?"
                   if len(unsaved) == 1
                   else f"You have {len(unsaved)} documents with unsaved changes.\n\nDo you want to save all before quitting?")
            r = QMessageBox.question(self, "Save Changes?", msg,
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                                     QMessageBox.Yes)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                for i in range(self.tabs.count()):
                    ed = self._editor_from_widget(self.tabs.widget(i))
                    if ed and ed.document().isModified():
                        self.tabs.setCurrentIndex(i)
                        if not self.save():
                            return
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and hasattr(ed, '_backup'):
                ed._backup.delete()
        self.close()

    def _editor_from_widget(self, w) -> HtmlEditor | None:
        if isinstance(w, HtmlEditor):
            return w
        if isinstance(w, QWidget):
            for child in w.children():
                if isinstance(child, HtmlEditor):
                    return child
        return None

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open", "",
            "Note files (*.md *.html *.txt);;All files (*.*)"
        )
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"File could not be read:\n{e}")
            return

        ed = HtmlEditor()
        ext = path.lower()
        if ext.endswith(('.html', '.htm')):
            ed.setHtml(content)
        elif ext.endswith('.md'):
            ed.document().setMarkdown(content)
        else:
            ed.setPlainText(content)

        ed._file_path = path
        ed.document().setModified(False)
        ed.document().contentsChanged.connect(self._update_tab_title)
        ed.document().undoAvailable.connect(self._update_undo_redo_buttons)
        ed.document().redoAvailable.connect(self._update_undo_redo_buttons)
        self._update_editor_theme(ed)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self.show_line_numbers:
            gutter = LineNumberGutter(ed)
            layout.addWidget(gutter)
            ed._line_gutter = gutter

        layout.addWidget(ed)

        idx = self.tabs.addTab(container, os.path.basename(path))
        self.tabs.setCurrentIndex(idx)
        self._attach_close_button(idx, container)
        ed._backup = EditorBackup(ed, path)
        ed.setFocus()
        self._update_window_title()

    def save(self) -> bool:
        path = self.current_path()
        if not path:
            return self.save_as()
        return self._save_to(path)

    def save_as(self) -> bool:
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "Save As", "",
            ".txt (*.txt);; .pdf (*.pdf);; .md (*.md);; .rtf (*.rtf)"
        )
        if not path:
            return False

        # Auto-add extension if the user didn't type one
        ext_map = {
            ".txt": ".txt",
            ".md":  ".md",
            ".rtf": ".rtf",
            ".pdf": ".pdf",
        }
        for keyword, ext in ext_map.items():
            if keyword in selected_filter and not path.lower().endswith(ext):
                path += ext
                break

        ok = self._save_to(path)
        if ok:
            self.set_current_path(path)
            ed = self.current_editor()
            if ed and hasattr(ed, '_backup'):
                ed._backup.update_file_path(path)
        return ok

    def _save_to(self, path: str) -> bool:
        ed = self.current_editor()
        if not ed:
            return False

        ext = path.lower()
        try:
            if ext.endswith('.pdf'):
                return self._save_as_pdf(ed, path)
            elif ext.endswith('.rtf'):
                return self._save_as_rtf(ed, path)

            if ext.endswith('.md'):
                content = ed.document().toMarkdown()
            else:
                content = ed.toPlainText()

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            ed.document().setModified(False)
            self._update_tab_title()
            self.status.showMessage(f"Saved: {path}")
            return True

        except Exception as e:
            QMessageBox.critical(self, "Error", f"File could not be saved:\n{e}")
            return False

    def _save_as_rtf(self, editor, path: str) -> bool:
        try:
            content = self._to_rtf(editor)
            with open(path, "w", encoding="ascii") as f:
                f.write(content)
            editor.document().setModified(False)
            self._update_tab_title()
            self.status.showMessage(f"Saved: {path}")
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"RTF could not be saved:\n{e}")
            return False

    def _to_rtf(self, editor) -> str:
        """Convert QTextDocument to an RTF string.

        Walks the document block by block, fragment by fragment.
        Each fragment becomes an RTF group carrying its formatting codes.
        Non-ASCII characters are escaped as \\uN? (RTF Unicode escape).
        """
        doc = editor.document()

        # --- Pass 1: collect all unique foreground colors used in the document ---
        colors: list[tuple[int, int, int]] = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if fmt.hasProperty(QTextCharFormat.ForegroundBrush):
                    c = fmt.foreground().color()
                    rgb = (c.red(), c.green(), c.blue())
                    if rgb not in colors:
                        colors.append(rgb)
                it += 1
            block = block.next()

        # --- RTF header ---
        # \colortbl: entry 0 is implicit default; custom colors start at index 1.
        color_table = "{\\colortbl;" + "".join(
            f"\\red{r}\\green{g}\\blue{b};" for r, g, b in colors
        ) + "}"

        parts = [
            "{\\rtf1\\ansi\\deff0\n",
            "{\\fonttbl{\\f0\\fmodern\\fcharset0 Courier New;}}\n",
            color_table + "\n",
            "\\f0\\fs28\n",  # Courier New 14 pt  (RTF uses half-points)
        ]

        # --- Pass 2: emit content ---
        block = doc.begin()
        first_block = True
        while block.isValid():
            if not first_block:
                parts.append("\\par\n")
            first_block = False

            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()

                # Escape RTF special chars and non-ASCII
                escaped = []
                for ch in frag.text():
                    if ch == "\\":
                        escaped.append("\\\\")
                    elif ch == "{":
                        escaped.append("\\{")
                    elif ch == "}":
                        escaped.append("\\}")
                    elif ord(ch) > 127:
                        escaped.append(f"\\u{ord(ch)}?")
                    else:
                        escaped.append(ch)
                text = "".join(escaped)

                # Build format prefix (all codes go inside a single RTF group
                # so they reset automatically at the closing brace)
                codes = ""
                if fmt.fontWeight() > 400:
                    codes += "\\b "
                if fmt.fontItalic():
                    codes += "\\i "
                if fmt.fontUnderline():
                    codes += "\\ul "
                if fmt.hasProperty(QTextCharFormat.ForegroundBrush):
                    c = fmt.foreground().color()
                    idx = colors.index((c.red(), c.green(), c.blue())) + 1
                    codes += f"\\cf{idx} "

                parts.append(f"{{{codes}{text}}}" if codes else text)
                it += 1

            block = block.next()

        parts.append("\n}")
        return "".join(parts)

    def _save_as_pdf(self, editor, path: str) -> bool:
        try:
            from PySide6.QtPrintSupport import QPrinter
            from PySide6.QtGui import QFont

            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(path)
            printer.setPageSize(QPageSize(QPageSize.A4))
            printer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Millimeter)

            # Clone document and force white background + black default text for print.
            # Custom text colors (red, blue, etc.) are mid-range and print well on white.
            doc = editor.document().clone()
            frame_fmt = doc.rootFrame().frameFormat()
            frame_fmt.setBackground(QColor("white"))
            doc.rootFrame().setFrameFormat(frame_fmt)
            doc.setDefaultStyleSheet("body { color: #000000; background: #ffffff; }")

            # Use point size (device-independent) so text scales correctly at 1200 DPI.
            # The editor uses "font-size: 14px" (screen pixels) which would appear tiny
            # at printer resolution without this conversion. 11pt ≈ standard document size.
            font = QFont(MONO_FONT, 11)
            doc.setDefaultFont(font)

            doc.print_(printer)

            editor.document().setModified(False)
            self._update_tab_title()
            self.status.showMessage(f"Saved: {path}")
            return True

        except Exception as e:
            QMessageBox.critical(self, "Error", f"PDF could not be saved:\n{e}")
            return False


    # ----------------------------------------------------------------------
    # SEARCH
    # ----------------------------------------------------------------------
    def _toggle_findbar(self):
        vis = not self.findbar.isVisible()
        self.findbar.setVisible(vis)
        if vis:
            self.findbar.focusIn()
            self._on_search_text_changed()
        else:
            self.findbar.set_match_info("")
            self._clear_search_highlights()

    def _show_shortcuts(self):
        shortcuts_text = """Keyboard Shortcuts:

File Operations:
Ctrl+N          - New document
Ctrl+O          - Open file
Ctrl+S          - Save file
Ctrl+Shift+S    - Save As
Ctrl+W          - Close tab
Ctrl+Q          - Quit

Text Formatting:
Ctrl+B          - Bold
Ctrl+I          - Italic
Ctrl+U          - Underline
Ctrl+D          - Reset to default format

Undo / Redo:
Ctrl+Z          - Undo
Ctrl+Y          - Redo
Ctrl+Shift+Z    - Redo

Navigation & Search:
Ctrl+F          - Find
F3              - Find next
Shift+F3        - Find previous
Escape          - Close find bar

Editor Features:
Tab             - Indent / Create bullet
Shift+Tab       - Outdent / Remove bullet
Enter           - New line (continues bullets)
Ctrl+Shift+L    - Toggle line numbers
Ctrl+Shift+T    - Toggle dark/light theme"""

        msg = QMessageBox(self)
        msg.setWindowTitle("Keyboard Shortcuts")
        msg.setText(shortcuts_text)
        msg.setIcon(QMessageBox.Information)
        msg.exec()

    def _on_search_text_changed(self):
        needle = self.findbar.text()
        if needle != self._last_search_text:
            self._update_search_matches(needle)
            self._last_search_text = needle
        if needle:
            self._highlight_all_matches()
            self._update_match_display()
        else:
            self._clear_search_highlights()
            self.findbar.set_match_info("")

    def _update_search_matches(self, needle):
        ed = self.current_editor()
        if not ed:
            return
        self._search_matches = []
        if not needle:
            self._current_match_index = -1
            return
        cursor = QTextCursor(ed.document())
        cursor.movePosition(QTextCursor.Start)
        while True:
            cursor = ed.document().find(needle, cursor, QTextDocument.FindFlags())
            if cursor.isNull():
                break
            self._search_matches.append((cursor.selectionStart(), cursor.selectionEnd()))
        self._current_match_index = -1

    def _highlight_all_matches(self):
        ed = self.current_editor()
        if not ed:
            return
        if not self._search_matches:
            ed.setExtraSelections([])
            return

        highlight_fmt = QTextCharFormat()
        if self.is_dark_theme:
            highlight_fmt.setBackground(QColor("#3C3C3C"))
            highlight_fmt.setForeground(QColor("#D4D4D4"))
        else:
            highlight_fmt.setBackground(QColor("#FFFF00"))
            highlight_fmt.setForeground(QColor("#000000"))

        selections = []
        for start_pos, end_pos in self._search_matches:
            cursor = QTextCursor(ed.document())
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = highlight_fmt
            selections.append(sel)

        if 0 <= self._current_match_index < len(self._search_matches):
            start_pos, end_pos = self._search_matches[self._current_match_index]
            cursor = QTextCursor(ed.document())
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            cur_sel = QTextEdit.ExtraSelection()
            cur_sel.cursor = cursor
            cur_fmt = QTextCharFormat()
            if self.is_dark_theme:
                cur_fmt.setBackground(QColor("#264F78"))
                cur_fmt.setForeground(QColor("#FFFFFF"))
            else:
                cur_fmt.setBackground(QColor("#0078D4"))
                cur_fmt.setForeground(QColor("#FFFFFF"))
            cur_sel.format = cur_fmt
            for i, sel in enumerate(selections):
                if sel.cursor.selectionStart() == start_pos:
                    selections[i] = cur_sel
                    break

        ed.setExtraSelections(selections)
        self._search_extra_selections = selections

    def _clear_search_highlights(self):
        ed = self.current_editor()
        if ed:
            ed.setExtraSelections([])
            self._search_extra_selections = []

    def _find_once(self, forward=True):
        ed = self.current_editor()
        if not ed:
            return
        needle = self.findbar.text()
        if not needle:
            return
        if needle != self._last_search_text:
            self._update_search_matches(needle)
            self._last_search_text = needle
        if not self._search_matches:
            self.findbar.set_match_info("No matches")
            return

        current_pos = ed.textCursor().position()
        if forward:
            for i, (start, end) in enumerate(self._search_matches):
                if start > current_pos:
                    self._current_match_index = i
                    break
            else:
                self._current_match_index = 0
        else:
            for i in range(len(self._search_matches) - 1, -1, -1):
                start, end = self._search_matches[i]
                if end < current_pos:
                    self._current_match_index = i
                    break
            else:
                self._current_match_index = len(self._search_matches) - 1

        if 0 <= self._current_match_index < len(self._search_matches):
            start_pos, end_pos = self._search_matches[self._current_match_index]
            cursor = QTextCursor(ed.document())
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            ed.setTextCursor(cursor)
            ed.ensureCursorVisible()

        self._highlight_all_matches()
        self._update_match_display()

    def _update_match_display(self):
        if not self._search_matches:
            self.findbar.set_match_info("No matches")
            return
        total = len(self._search_matches)
        current = self._current_match_index + 1
        self.findbar.set_match_info(f"{current} of {total}")

    def _find_next(self):
        self._find_once(True)

    def _find_prev(self):
        self._find_once(False)


    # ----------------------------------------------------------------------
    # FORMAT HANDLERS
    # ----------------------------------------------------------------------
    def _fmt_bold(self):
        ed = self.current_editor()
        if ed:
            ed.toggle_bold()

    def _fmt_italic(self):
        ed = self.current_editor()
        if ed:
            ed.toggle_italic()

    def _fmt_underline(self):
        ed = self.current_editor()
        if ed:
            ed.toggle_underline()

    def _reset_format(self):
        ed = self.current_editor()
        if ed:
            ed.reset_to_default()

    def _undo(self):
        ed = self.current_editor()
        if ed:
            ed.undo()

    def _redo(self):
        ed = self.current_editor()
        if ed:
            ed.redo()


    # ----------------------------------------------------------------------
    # THEME TOGGLE
    # ----------------------------------------------------------------------
    def _toggle_theme(self):
        self.is_dark_theme = self.header_widget.theme_checkbox.isChecked()
        self._apply_theme()
        if hasattr(self, 'findbar'):
            self.findbar.is_dark = self.is_dark_theme
            self.findbar._update_theme()
        if hasattr(self, 'header_widget'):
            self.header_widget.update_theme(self.is_dark_theme)
        mode = "Dark" if self.is_dark_theme else "Light"
        self.status.showMessage(f"Switched to {mode} theme", 2000)


    # ----------------------------------------------------------------------
    # LINE NUMBERS
    # ----------------------------------------------------------------------
    def _toggle_line_numbers(self):
        if hasattr(self, 'header_widget'):
            self.show_line_numbers = self.header_widget.line_numbers_checkbox.isChecked()
        else:
            self.show_line_numbers = not self.show_line_numbers

        self.act_toggle_line_numbers.setChecked(self.show_line_numbers)

        for i in range(self.tabs.count()):
            container = self.tabs.widget(i)
            if isinstance(container, QWidget):
                editor = self._editor_from_widget(container)
                if editor:
                    if self.show_line_numbers and not hasattr(editor, '_line_gutter'):
                        gutter = LineNumberGutter(editor)
                        container.layout().insertWidget(0, gutter)
                        editor._line_gutter = gutter
                    elif not self.show_line_numbers and hasattr(editor, '_line_gutter'):
                        container.layout().removeWidget(editor._line_gutter)
                        editor._line_gutter.deleteLater()
                        delattr(editor, '_line_gutter')

        self.status.showMessage(f"Line numbers {'enabled' if self.show_line_numbers else 'disabled'}", 2000)


    # ----------------------------------------------------------------------
    # CLOSE EVENT
    # ----------------------------------------------------------------------
    def closeEvent(self, event):
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and ed.document().isModified():
                r = QMessageBox.question(self, "Save Changes?",
                                         "There are unsaved changes. Close everything now?",
                                         QMessageBox.Yes | QMessageBox.No)
                if r != QMessageBox.Yes:
                    event.ignore()
                    return
                else:
                    break
        for i in range(self.tabs.count()):
            ed = self._editor_from_widget(self.tabs.widget(i))
            if ed and hasattr(ed, '_backup'):
                ed._backup.delete()
        event.accept()
