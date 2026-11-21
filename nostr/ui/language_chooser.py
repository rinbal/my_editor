# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One searchable, offline language chooser for writing and article details.

A short initial palette is a convenience, never the supported-language list.
Selection is immediate. "All languages" is one scrolling list in the order of
the names shown (red-b-5); a search shows its best matches, and "More
languages" adds the next ones below them. The versions this article already
has come first, under their own heading, and the current one carries a check
mark beside its name, not in it (red-b-12). Names are plain words: a region
by its name, the code only in the tooltip (red-b-6).
QLineEdit owns IME handling, so composing Return never creates a document.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QCollator, QEvent, QLocale, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from i18n import _, language as interface_language
from ..content_language import (
    _CATALOG, _search_text, language_name as catalogue_name, normalize_language,
    search_languages,
)
from . import appearance
from .imports_glyphs import glyph_icon

PAGE_SIZE = 40
MARK = 14            # the check mark's room, at the start of every row

# Registry words that are no language's name ("Swahili (macrolanguage)").
_REGISTRY_WORDS = re.compile(r"\s*\((?:macrolanguage|individual language)\)", re.IGNORECASE)


def _display_names() -> dict:
    """Region and script names per interface language, from Unicode CLDR
    (nostr/language_display_names.json, made by scripts/cldr_display_names.py):
    Qt names them in English only (me68-verify-2)."""
    global _DISPLAY
    if _DISPLAY is None:
        try:
            path = Path(__file__).resolve().parent.parent / "language_display_names.json"
            _DISPLAY = json.loads(path.read_text(encoding="utf-8")).get("locales", {})
        except (OSError, ValueError):
            _DISPLAY = {}
    return _DISPLAY


_DISPLAY = None


def _cldr(kind: str, code: str, interface: str) -> str:
    return _display_names().get(interface.split("-")[0].lower(), {}).get(kind, {}).get(code, "")


def _region_name(region: str, code: str, interface: str) -> str:
    """``region`` (``BR``, ``419``) as a name in the interface's language:
    CLDR's (Brasilien, Lateinamerika) where MyEditor carries them, else
    Qt's where it has them (every region in English), else as the region
    calls itself (Brasil)."""
    named = _cldr("regions", region.upper(), interface)
    if named:
        return named
    territory = QLocale.codeToTerritory(region)
    if territory == QLocale.Country.AnyTerritory:
        return region
    ui = QLocale(interface)
    there = QLocale(ui.language(), territory)
    if there.language() == ui.language() and there.territory() == territory:
        return there.nativeTerritoryName()
    if ui.language() == QLocale.Language.English:
        return QLocale.territoryToString(territory)
    own = QLocale(code.replace("-", "_"))
    if own.territory() == territory and own.nativeTerritoryName():
        return own.nativeTerritoryName()
    return QLocale.territoryToString(territory)


def _script_name(part: str, interface: str) -> str:
    named = _cldr("scripts", part.title(), interface)
    if named:
        return named
    script = QLocale.codeToScript(part)
    return QLocale.scriptToString(script) if script != QLocale.Script.AnyScript else part


def _plain(name: str) -> str:
    """A registry description as words: "Basa (Cameroon)" as "Basa, Cameroon"."""
    return re.sub(r"\s*\(([^()]*)\)", r", \1", _REGISTRY_WORDS.sub("", name)).strip()


@lru_cache(maxsize=4)
def _base_names(interface: str) -> dict:
    """Every catalogue language's name in ``interface``, two languages
    never under one name (me68-verify-3). Where the interface's names
    meet ("Chinook" for chh and chn), the one the registry calls otherwise
    says so ("Chinook (Chinook jargon)"); a single language of a group
    of the same name ("Malay" zlm, beside the group ms) says that."""
    shown = {code: _REGISTRY_WORDS.sub("", catalogue_name(code, interface))
             for code, _names in _CATALOG["languages"]}
    registry = {code: names[0] for code, names in _CATALOG["languages"]}
    groups = {}
    for code, name in shown.items():
        groups.setdefault(name.casefold(), []).append(code)
    for codes in (group for group in groups.values() if len(group) > 1):
        for code in codes:
            own = _plain(registry[code])
            if own.casefold() != shown[code].casefold():
                shown[code] = f"{shown[code]} ({own})"
            elif "individual language" in registry[code].lower():
                shown[code] = f"{shown[code]} ({_('single language')})"
        left = {}
        for code in codes:
            left.setdefault(shown[code].casefold(), []).append(code)
        for same in (group for group in left.values() if len(group) > 1):
            for code in same[1:]:
                shown[code] = f"{shown[code]} ({code})"
    return shown


def language_name(code):
    """A human label in the current interface, alongside native names in
    lists: "Portugiesisch (Brasilien)", never "(pt-BR)" (red-b-6)."""
    interface = interface_language()
    language = normalize_language(code)
    if not language:
        return ""
    base, *rest = language.split("-")
    name = _base_names(interface).get(base) or _REGISTRY_WORDS.sub(
        "", catalogue_name(base, interface))
    parts = []
    for part in rest:
        if len(part) == 4 and part.isalpha():
            parts.append(_script_name(part, interface))
        elif len(part) == 2 and part.isalpha() or len(part) == 3 and part.isdigit():
            parts.append(_region_name(part, language, interface))
        else:
            parts.append(part)
    return f"{name} ({', '.join(parts)})" if parts else name


def _regional_matches(query: str, interface: str, recent=()) -> list:
    """Regional versions a search names in the interface's words: a
    version already used whose label holds the query, and "Portugiesisch
    Brasilien" as pt-BR (CLDR region names, me68-verify-2)."""
    needle = _search_text(query)
    if not needle:
        return []
    found = [code for code in recent if "-" in code and needle in _search_text(language_name(code))]
    regions = _display_names().get(interface.split("-")[0].lower(), {}).get("regions", {})
    words = needle.split()
    if len(words) > 1 and regions:
        for index, word in enumerate(words):
            if len(word) < 4:
                continue
            for region, name in regions.items():
                if region.isalpha() and _search_text(name).startswith(word):
                    rest = " ".join(words[:index] + words[index + 1:])
                    rows = search_languages(rest, limit=1, locale=interface)
                    code = normalize_language(f"{rows[0]['code']}-{region}") if rows else ""
                    if code and "-" in code and code not in found:
                        found.append(code)
    return found


def _native_name(code):
    base = normalize_language(code).split("-")[0]
    return _REGISTRY_WORDS.sub("", _CATALOG["native_names"].get(base, ""))


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
        self._dark = dark
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
        self.results.setIconSize(QSize(MARK, MARK))
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
        self.more_button.setAccessibleDescription(_("Adds more languages below the list"))
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
        self._dark = dark
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
        appending = bool(self._offset)
        if self._all and not query:
            # Every language, in the order of the names shown.
            # Sorted as the interface's language sorts (Ä with A in German),
            # a leading apostrophe ignored ('Are'are with A).
            collator = QCollator(QLocale(interface_language()))
            rows = sorted(({"code": code} for code, _names in _CATALOG["languages"]),
                          key=lambda row: collator.sortKey(
                              language_name(row["code"]).lstrip("'’ -")))
        elif query:
            rows = search_languages(query, recent=self._recent, limit=PAGE_SIZE + 1,
                                    offset=self._offset, locale=interface_language())
            if not self._offset:
                regional = [{"code": code} for code in
                            _regional_matches(query, interface_language(), self._recent)]
                rows = regional + [row for row in rows
                                   if row["code"] not in {r["code"] for r in regional}]
        else:
            # Resolve only palette codes, then render their normal names.
            rows = [{"code": code} for code in self._recent[:8] if code]
        page = rows if self._all and not query else rows[:PAGE_SIZE]
        if not appending:
            self.results.clear()
        existing = [row for row in page if row["code"] in self._existing]
        others = [row for row in page if row["code"] not in self._existing]
        if existing and not appending:
            self._heading(_("Versions of this article"))
            for row in existing:
                self._add(row["code"])
            self._heading(_("Other languages"))
        for row in (others if not appending else page):
            self._add(row["code"])
        self.empty.setVisible(not self._choosable())
        self.all_button.setVisible(not self._all and not query)
        self.more_button.setVisible(bool(query) and len(rows) > PAGE_SIZE)
        if not appending:
            # The first row that opens something: never the current one.
            first = next((row for row in self._choosable()
                          if self.results.item(row).data(Qt.ItemDataRole.UserRole)
                          != self._current), None)
            if first is not None:
                self.results.setCurrentRow(first)

    def _heading(self, text):
        item = QListWidgetItem(self._no_mark(), text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.results.addItem(item)

    def _add(self, code):
        name = language_name(code)
        native = _native_name(code)
        label = f"{native} · {name}" if native and native != name else name
        # Every row keeps the check mark's room, so all are as tall and
        # start at one edge, as a menu's items do (me68-verify-4).
        item = QListWidgetItem(self._no_mark(), label)
        item.setData(Qt.ItemDataRole.UserRole, code)
        item.setToolTip(code)
        if code == self._current:
            item.setIcon(glyph_icon("check", MARK, QColor(appearance.roles(self._dark).text)))
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, _("Current language"))
        elif code in self._existing:
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, _("Opens this version"))
        self.results.addItem(item)

    @staticmethod
    def _no_mark():
        blank = QPixmap(MARK * 2, MARK * 2)
        blank.setDevicePixelRatio(2.0)
        blank.fill(Qt.GlobalColor.transparent)
        return QIcon(blank)

    def _choosable(self):
        return [row for row in range(self.results.count())
                if self.results.item(row).flags() & Qt.ItemFlag.ItemIsEnabled]

    def _choose_first(self):
        rows = self._choosable()
        if not self._composing and rows:
            current = self.results.currentRow()
            self._select(self.results.item(current if current in rows else rows[0]))

    def _select(self, item):
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.chosen.emit(item.data(Qt.ItemDataRole.UserRole))
            self.accept()
