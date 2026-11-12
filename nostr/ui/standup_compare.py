# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two versions of a text, compared word by word and shown (plan 8.1;
the prototype's screen 29).

:class:`Comparer` runs ``nostr/standup/compare.py`` off the main thread
(the shared worker of ``nostr/imports/workers.py``), once per pair of
texts: a pair asked for again is answered from what was computed, and
one asked for while it is being computed hears that one answer. Its
texts belong to the account in use, so :meth:`Comparer.stop` (sign-out,
another account) forgets them and lets no answer still on its way
through (criterion 19).

:class:`CompareView` shows the answer: what is new underlined on a
tinted ground, what was removed struck through, so the marks do not
rest on colour alone, with one line that says so. Past the bounds it
says "Zu viele Änderungen zum Markieren" and shows both versions, one
after the other. :class:`CompareDialog` is that view as a sheet with
Close.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QSize, Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QTextEdit, QVBoxLayout, QWidget,
)

import announce
from i18n import N_, _

from ..imports import workers
from ..standup import compare
from ..standup.compare import ADDED, REMOVED, Comparison
from . import appearance
from .publish_sheet import fit_to_window, make_sheet, sheet_stylesheet

COMPARING = N_("Comparing …")
TOO_MANY = N_("Too many changes to mark")
NO_CHANGES = N_("No changes to the text")
LEGEND = N_("New text is underlined, removed text is struck through.")
# The marks' grounds, readable on either theme (with the underline and
# the strike, never alone).
_GROUND = {True: {ADDED: "#1F4D2E", REMOVED: "#5A2327"},
           False: {ADDED: "#D8F5DF", REMOVED: "#FBDADD"}}


def _pair(old: str, new: str) -> str:
    digest = hashlib.sha256()
    for text in (old, new):
        data = text.encode("utf-8")
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


class Comparer(QObject):
    """See the module docstring. ``run(fn, on_done, on_error)`` is the
    executor (a seam; the worker thread's by default)."""

    def __init__(self, parent: Optional[QObject] = None, *, run: Optional[Callable] = None,
                 compare_words: Callable[[str, str], Comparison] = compare.compare_words
                 ) -> None:
        super().__init__(parent)
        self._run = run or (lambda fn, on_done, on_error: workers.run_blocking(
            fn, on_done, on_error, parent=self))
        self._compare = compare_words
        self._done: Dict[str, Comparison] = {}
        self._waiting: Dict[str, List[Callable[[Comparison], None]]] = {}
        # A pair being computed stops once nobody waits for it any more.
        self._stops: Dict[str, threading.Event] = {}
        self._generation = 0
        self.computed = 0               # pairs computed (each once)

    def compare(self, old: str, new: str,
                on_done: Callable[[Comparison], None]) -> Callable[[], None]:
        """``on_done(comparison)``, on this thread; at once for a pair
        already compared. What it gives back takes the question back (a
        view that closes): ``on_done`` is not called, and a pair nobody
        waits for any more stops being computed."""
        key = _pair(old, new)
        if key in self._done:
            on_done(self._done[key])
            return lambda: None
        if key in self._waiting:
            self._waiting[key].append(on_done)
            return lambda: self._take_back(key, on_done)
        self._waiting[key] = [on_done]
        stop = self._stops[key] = threading.Event()
        generation = self._generation

        def computed(result: Comparison) -> None:
            if generation != self._generation or stop.is_set():
                return                  # the account was let go, or nobody waits
            self._stops.pop(key, None)
            self.computed += 1
            self._done[key] = result
            for heard in self._waiting.pop(key, []):
                heard(result)

        def failed(_error: BaseException) -> None:
            # Nothing to mark with: both versions are shown all the same.
            computed(Comparison(old, new, too_many=True))

        self._run(lambda: self._compare(old, new, stopped=stop.is_set), computed, failed)
        return lambda: self._take_back(key, on_done)

    def _take_back(self, key: str, on_done: Callable[[Comparison], None]) -> None:
        waiting = self._waiting.get(key, [])
        if on_done in waiting:
            waiting.remove(on_done)
        if key in self._waiting and not waiting:
            del self._waiting[key]
            self._stops.pop(key).set()

    def stop(self) -> None:
        """Sign-out or another account: its texts are forgotten, and an
        answer still being computed is heard by nobody."""
        self._generation += 1
        for stop in self._stops.values():
            stop.set()
        self._done, self._waiting, self._stops = {}, {}, {}


class CompareView(QWidget):
    """The marked text, or both versions (see the module docstring)."""

    def __init__(self, *, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = bool(is_dark)
        self._shown: Optional[Comparison] = None
        self._asked = 0
        self._take_back: Callable[[], None] = lambda: None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.state = QLabel(_(COMPARING))
        self.state.setObjectName("help")
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.state)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(_("Changes"))
        self.text.setTabChangesFocus(True)
        column.addWidget(self.text, 1)

    def show_pair(self, comparer: Comparer, old: str, new: str) -> None:
        """Compare ``old`` with ``new`` and show it; "Wird verglichen …"
        meanwhile. Only the pair asked for last is shown."""
        self._take_back()
        self._asked += 1
        asked = self._asked
        self._shown = None
        self.state.setText(_(COMPARING))
        self.text.clear()
        self._take_back = comparer.compare(old, new, lambda result: asked == self._asked
                                           and self.show_comparison(result))

    def show_comparison(self, comparison: Comparison) -> None:
        self._shown = comparison
        cursor = QTextCursor(self.text.document())
        self.text.clear()
        plain = QTextCharFormat()
        if comparison.too_many:
            self.state.setText(_(TOO_MANY))
            head = QTextCharFormat()
            head.setFontWeight(QFont.Weight.DemiBold)
            for title, body in ((_("Before"), comparison.old), (_("After"), comparison.new)):
                cursor.insertText(title + "\n", head)
                cursor.insertText(body.strip("\n") + "\n\n", plain)
        else:
            self.state.setText(_(LEGEND) if comparison.changed else _(NO_CHANGES))
            before = ""
            for part in comparison.parts:
                if (before, part.kind) == (REMOVED, ADDED):
                    cursor.insertText(" ", plain)   # a word replaced: the two apart
                cursor.insertText(part.text, self._format(part.kind))
                before = part.kind
        self.text.moveCursor(QTextCursor.MoveOperation.Start)
        announce.announce(self.state, self.state.text())

    def _format(self, kind: str) -> QTextCharFormat:
        style = QTextCharFormat()
        if kind == ADDED:
            style.setFontUnderline(True)
            style.setBackground(QColor(_GROUND[self._is_dark][ADDED]))
        elif kind == REMOVED:
            style.setFontStrikeOut(True)
            style.setBackground(QColor(_GROUND[self._is_dark][REMOVED]))
        return style

    def marked(self) -> List[tuple]:
        """What is shown, as ``(kind, text)`` stretches read back from
        the document's own formats (for a test, and a screen reader's
        description)."""
        out: List[tuple] = []
        block = self.text.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                style = fragment.charFormat()
                kind = (ADDED if style.fontUnderline() else REMOVED if style.fontStrikeOut()
                        else compare.SAME)
                out.append((kind, fragment.text()))
                it += 1
            block = block.next()
        return out

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        if self._shown is not None:
            self.show_comparison(self._shown)

    def forget(self) -> None:
        """The view closes: the texts go, and a compare still on its way
        is taken back (RG-M3-a 5)."""
        self._take_back()
        self._take_back = lambda: None
        self._asked += 1
        self._shown = None
        self.text.clear()


class CompareDialog(QDialog):
    """The compare view as a sheet: a heading, the view, Close. It holds
    two versions' texts, so it goes once it is closed."""

    def __init__(self, *, comparer: Comparer, old: str, new: str, heading: str, is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Changes"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        make_sheet(self)
        self.setStyleSheet(sheet_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        appearance.bind(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 18, 20, 14)
        column.setSpacing(10)
        title = QLabel(heading)
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(title)
        self.view = CompareView(is_dark=is_dark)
        column.addWidget(self.view, 1)
        buttons = QDialogButtonBox()
        close = buttons.addButton(_("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        close.setDefault(True)
        close.clicked.connect(self.reject)
        column.addWidget(buttons)
        fit_to_window(self, QSize(640, 520))
        self.view.show_pair(comparer, old, new)

    def done(self, result: int) -> None:  # noqa: N802, Qt's name
        super().done(result)
        self.view.forget()

    def apply_theme(self, is_dark: bool) -> None:
        self.setStyleSheet(sheet_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        self.view.set_dark(is_dark)


__all__ = ["COMPARING", "CompareDialog", "CompareView", "Comparer", "LEGEND", "NO_CHANGES",
           "TOO_MANY"]
