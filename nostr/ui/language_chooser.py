# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One searchable, offline language chooser for writing and article details.

A short initial palette is a convenience, never the supported-language list.
Selection is immediate; paging bounds widget count even for the full registry.
QLineEdit owns IME handling, so composing Return never creates a document.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from i18n import _, language as interface_language
from ..content_language import language_name as catalogue_name, search_languages
from . import appearance

PAGE_SIZE = 40


def language_name(code):
    """A human label in the current interface, alongside native names in lists."""
    return catalogue_name(code, interface_language())


class LanguageChooser(QDialog):
    chosen = Signal(str)

    def __init__(self, *, current="", existing=(), recent=(), dark=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Write in another language…"))
        self.setObjectName("LanguageChooser")
        self.setWindowFlag(Qt.WindowType.Popup, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(300)
        self.resize(360, 350)
        self._current = current
        self._existing = tuple(dict.fromkeys(existing))
        self._recent = tuple(dict.fromkeys((current, *existing, *recent, "en", "de", "es", "fr")))
        self._all = False
        self._offset = 0
        self._composing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("Search languages"))
        self.search.setAccessibleName(_("Search languages"))
        self.search.installEventFilter(self)
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.setAccessibleName(_("Languages"))
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setMinimumHeight(180)
        layout.addWidget(self.results, 1)
        self.empty = QLabel(_("No matching language"))
        self.empty.hide()
        layout.addWidget(self.empty)
        footer = QHBoxLayout()
        self.all_button = QPushButton(_("All languages"))
        self.all_button.setAutoDefault(False)
        self.all_button.clicked.connect(self._show_all)
        self.more_button = QPushButton(_("More languages"))
        self.more_button.setAutoDefault(False)
        self.more_button.clicked.connect(self._more)
        footer.addWidget(self.all_button)
        footer.addStretch(1)
        footer.addWidget(self.more_button)
        layout.addLayout(footer)
        self.results.itemClicked.connect(self._select)
        self.results.itemActivated.connect(self._select)
        self.search.textChanged.connect(self._search)
        self.search.returnPressed.connect(self._choose_first)
        appearance.bind(self)
        self.apply_theme(dark)
        self._search()

    def apply_theme(self, dark):
        appearance.surface(self, dark, kind="popover", glow=False)
        self.setStyleSheet(appearance.controls(dark) +
                          f"QDialog#LanguageChooser {{ background: {appearance.roles(dark).popover}; }}")

    def eventFilter(self, watched, event):
        if watched is self.search:
            if event.type() == QEvent.Type.InputMethod:
                self._composing = bool(event.preeditString())
            elif event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Down and not self._composing:
                    self.results.setFocus()
                    if self.results.currentRow() < 0 and self.results.count():
                        self.results.setCurrentRow(0)
                    return True
        return super().eventFilter(watched, event)

    def _search(self, *_args):
        self._offset = 0
        self._fill()

    def _show_all(self):
        self._all = True
        self._search()

    def _more(self):
        self._offset += PAGE_SIZE
        self._fill()

    def _fill(self):
        query = self.search.text().strip()
        if query or self._all:
            rows = search_languages(query, recent=self._recent,
                                    limit=PAGE_SIZE + 1, offset=self._offset)
        else:
            # Resolve only palette codes, then render their normal names.
            rows = [{"code": code, "name": language_name(code), "native_name": ""}
                    for code in self._recent[:8] if code]
        self.results.clear()
        for row in rows[:PAGE_SIZE]:
            code = row["code"]
            name = language_name(code)
            native = row.get("native_name", "")
            label = f"{native} · {name}" if native and native != name else name
            if code == self._current:
                label += "  ✓"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setToolTip(_("Open existing version") if code in self._existing else name)
            self.results.addItem(item)
        self.empty.setVisible(not self.results.count())
        self.all_button.setVisible(not self._all and not query)
        self.more_button.setVisible(len(rows) > PAGE_SIZE)
        if self.results.count():
            self.results.setCurrentRow(0)

    def _choose_first(self):
        if not self._composing and self.results.count():
            self._select(self.results.item(0))

    def _select(self, item):
        if item is not None:
            self.chosen.emit(item.data(Qt.ItemDataRole.UserRole))
            self.accept()
