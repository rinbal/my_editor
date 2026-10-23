# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An article's hashtags as tokens, as Mail shows recipients and Finder
shows tags: one field for Article Details and Publish as Article.

    [ #bitcoin × ] [ #essay × ]  Add a hashtag

Words typed become a tag at Return or a comma; Return with nothing typed
is left to the window (its default button). A tag is read as a file's
front matter string is (article_details.hashtags_written): commas
separate, else spaces, a leading "#" is dropped, and a tag the field has
already, in any case, is not added again. So pasting "a, b c" makes
three tags. Words typed and not yet a tag count as tags, and leaving the
field makes them one.

× removes its tag; Backspace with nothing typed removes the last one,
and a screen reader hears which. A tag's context menu copies it and
removes it, as Apple's guidelines suggest for tokens ("Add value with a
context menu"). Each tag is named by itself and each ×
"Remove <tag>"; Tab reaches the × buttons left to right, then the field.

Colours come from the theme (constants.py), light and dark.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QToolButton, QWidget,
)

import announce
from . import appearance
from i18n import _

from ..article_details import hashtags_written
from .flow_layout import FlowLayout


class _Entry(QLineEdit):
    """Where a tag is typed: Return and a comma make it a tag, Backspace
    with nothing typed takes the last one away."""

    def __init__(self, field: "TagField") -> None:
        super().__init__(field)
        self._field = field

    def keyPressEvent(self, event) -> None:  # noqa: N802, Qt's name
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.text().strip():
            self._field.commit()
            event.accept()
            return
        if key == Qt.Key.Key_Comma and not self.isReadOnly():
            self._field.commit()
            event.accept()
            return
        if (key == Qt.Key.Key_Backspace and not self.text() and not self.isReadOnly()
                and self._field.tags()):
            self._field.remove_last()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().focusOutEvent(event)
        self._field.commit()


class _Chip(QFrame):
    """One tag: its words and, unless the field is read-only, ×."""

    def __init__(self, tag: str, field: "TagField") -> None:
        super().__init__(field)
        self.tag = tag
        self.setObjectName("tag_chip")
        self.setAccessibleName(tag)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 1, 2, 1)
        row.setSpacing(0)
        words = QLabel("#" + tag)
        words.setObjectName("tag_chip_words")
        words.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(words)
        self.remove = QToolButton()
        self.remove.setObjectName("tag_chip_remove")
        self.remove.setText("×")
        name = _("Remove {name}").format(name=tag)
        self.remove.setAccessibleName(name)
        self.remove.setToolTip(name)
        self.remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove.clicked.connect(lambda: field.remove(tag))
        row.addWidget(self.remove)

    def menu(self) -> QMenu:
        """Copy the tag, and Remove it while the field can be changed."""
        menu = QMenu(self)
        menu.addAction(_("Copy"), lambda: QGuiApplication.clipboard().setText("#" + self.tag))
        if self.remove.isVisible():
            menu.addAction(_("Remove"), self.remove.click)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802, Qt's name
        self.menu().exec(event.globalPos())


class TagField(QFrame):
    """Hashtags as tokens (see the module docstring). ``changed`` when the
    person adds or removes one."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *, is_dark: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("tag_field")
        self._tags: List[str] = []
        self._chips: List[_Chip] = []
        self._read_only = False
        self._flow = FlowLayout(self, spacing=4)
        self._flow.setContentsMargins(4, 3, 4, 3)
        self.entry = _Entry(self)
        self.entry.setObjectName("tag_field_entry")
        self.entry.setFrame(False)
        self.entry.setPlaceholderText(_("Add a hashtag"))
        # Room for its words, in any language, before a tag is typed.
        self.entry.setMinimumWidth(max(110, self.entry.fontMetrics().horizontalAdvance(
            self.entry.placeholderText()) + 16))
        # A comma typed or pasted ends a tag: what came before it is one.
        self.entry.textEdited.connect(self._edited)
        self._flow.addWidget(self.entry)
        self.setFocusProxy(self.entry)
        self.set_dark(is_dark)

    # -- what it holds ------------------------------------------------------------

    def tags(self) -> Tuple[str, ...]:
        """The tags, with the words typed and not yet made a tag."""
        return self._with(self._tags, hashtags_written(self.entry.text()))

    def set_tags(self, tags: Iterable[str]) -> None:
        """Show ``tags`` (as the article has them), with nothing typed."""
        self.entry.clear()
        self._tags = list(self._with([], tags))
        self._show_chips()

    def chips(self) -> List[QWidget]:
        return list(self._chips)

    def setAccessibleName(self, name: str) -> None:  # noqa: N802, Qt's name
        super().setAccessibleName(name)
        self.entry.setAccessibleName(name)

    def setReadOnly(self, read_only: bool) -> None:  # noqa: N802, Qt's name
        """Kept as it is (a file that writes the tags in a way this app
        can't change): the tags without ×, and nowhere to type."""
        self._read_only = read_only
        self.entry.setReadOnly(read_only)
        self.entry.setVisible(not read_only)
        for chip in self._chips:
            chip.remove.setVisible(not read_only)

    def isReadOnly(self) -> bool:  # noqa: N802, Qt's name
        return self._read_only

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        r = appearance.roles(is_dark)
        field, border, text, muted, ring = r.field, r.edge, r.text, r.muted, r.focus
        self.setPalette(appearance.palette(is_dark))
        self.setStyleSheet(
            f"QFrame#tag_field {{ background: {field}; border: 1px solid {border};"
            f" border-radius: 4px; }}"
            f"QLineEdit#tag_field_entry {{ background: transparent; border: none;"
            f" color: {text}; padding: 2px; }}"
            f"QFrame#tag_chip {{ background: {r.soft}; border: none; border-radius: 10px; }}"
            f"QLabel#tag_chip_words {{ color: {r.ink}; background: transparent; }}"
            f"QToolButton#tag_chip_remove {{ color: {muted}; background: transparent;"
            f" border: 1px solid transparent; border-radius: 8px; padding: 0 4px; }}"
            f"QToolButton#tag_chip_remove:hover {{ color: {text}; }}"
            f"QToolButton#tag_chip_remove:focus {{ border-color: {ring}; }}")

    # -- editing ------------------------------------------------------------------

    def commit(self) -> None:
        """Make the words typed tags."""
        typed = self.entry.text()
        if not typed.strip():
            if typed:
                self.entry.clear()
            return
        self.entry.clear()
        self._add(hashtags_written(typed))

    def remove(self, tag: str) -> None:
        if tag in self._tags:
            self._tags.remove(tag)
            self._show_chips()
            self.entry.setFocus()
            self.changed.emit()

    def remove_last(self) -> None:
        tag = self._tags[-1]
        self.remove(tag)
        announce.announce(self.entry, _("Removed {name}").format(name=tag))

    def mousePressEvent(self, event) -> None:  # noqa: N802, Qt's name
        # A click beside the tags is a click in the field.
        self.entry.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def _edited(self, text: str) -> None:
        if "," in text:
            done, _comma, rest = text.rpartition(",")
            self.entry.setText(rest.lstrip())
            self._add(hashtags_written(done))

    def _add(self, tags: Iterable[str]) -> None:
        after = list(self._with(self._tags, tags))
        if after != self._tags:
            self._tags = after
            self._show_chips()
            self.changed.emit()

    @staticmethod
    def _with(tags: Iterable[str], more: Iterable[str]) -> Tuple[str, ...]:
        """``tags`` and then those of ``more`` not among them, in any case."""
        out = list(tags)
        for tag in more:
            tag = tag.strip().lstrip("#").strip()
            if tag and tag.lower() not in (have.lower() for have in out):
                out.append(tag)
        return tuple(out)

    def _show_chips(self) -> None:
        had_focus = any(chip.remove.hasFocus() for chip in self._chips)
        for chip in self._chips:
            self._flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        # The tags in order, the place to type after them.
        self._flow.removeWidget(self.entry)
        self._chips = [_Chip(tag, self) for tag in self._tags]
        for chip in self._chips:
            self._flow.addWidget(chip)
            chip.remove.setVisible(not self._read_only)
            chip.show()
        self._flow.addWidget(self.entry)
        # Tab goes left to right: each ×, then the field. (Each × follows
        # the field first, where Tab already goes; then the field moves
        # after the last of them.)
        previous = self.entry
        for chip in self._chips:
            QWidget.setTabOrder(previous, chip.remove)
            previous = chip.remove
        if self._chips:
            QWidget.setTabOrder(previous, self.entry)
        if had_focus:
            self.entry.setFocus()
