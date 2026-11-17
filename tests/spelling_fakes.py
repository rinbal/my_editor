# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A spell checker that knows a handful of words, and an editor wired to
spelling the way docs/develop/spelling.md says, for the spelling tests."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextEdit

from spelling.backends import AUTOMATIC, SpellBackend, TextCheck
from spelling.service import DocumentSpelling, SpellChecker

ENGLISH = ("the", "a", "is", "this", "text", "word", "words", "with", "one", "mistake",
           "and", "here", "see", "it", "spelled", "right", "code", "link", "line",
           "hello", "world", "well", "known", "don't", "it's", "naïve", "e-mail", "first",
           "second", "third", "Alice", "read", "more", "at", "or", "on", "in", "of",
           "title", "write", "every", "block", "check", "after", "before", "end")
GERMAN = ("das", "Das", "ist", "ein", "eine", "Haus", "schön", "Straße", "Grüße", "und",
          "mit", "Wörter", "Wort", "geht", "Adresse", "Mail", "Fehler", "hier", "der",
          "die", "bzw.", "Text", "Zeile", "wie", "gut", "Hab", "gesehen")
WORDS = {"en-US": ENGLISH, "de-DE": GERMAN}

_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
# A system checker reads "_" as part of a word, as macOS does: "_Wrot_".
_SYSTEM_WORD = re.compile(r"[^\W\d]+(?:['’\-][^\W\d]+)*")


class FakeBackend(SpellBackend):
    """Checks word by word against a short list, and records every call."""

    name = "fake"

    def __init__(self, words: Optional[Dict[str, Iterable[str]]] = None, *,
                 default: Optional[str] = "en-US") -> None:
        super().__init__()
        self.words = {language: set(known) for language, known in (words or WORDS).items()}
        self.default = default
        self.calls: List[tuple] = []
        self.learned: List[str] = []
        self.ignored: List[str] = []
        self.fail: Optional[str] = None       # the hook that raises next
        self.absent: Optional[str] = None     # open() says the system has no checker
        self.opened = 0
        self.closed = 0

    def _boom(self, hook: str) -> None:
        if self.fail == hook:
            raise RuntimeError(f"{hook} broke")

    def _open(self) -> None:
        from spelling.backends import Unavailable
        self.opened += 1
        if self.absent:
            raise Unavailable(self.absent)
        self._boom("open")

    def _close(self) -> None:
        self.closed += 1

    def _languages(self) -> Sequence[str]:
        self._boom("languages")
        return list(self.words)

    def _default_language(self) -> Optional[str]:
        return self.default

    def knows(self, word: str, language: str) -> bool:
        known = self.words.get(language, ())
        # Like a real dictionary, a word may start a sentence: "Geht".
        return (word in known or word[:1].lower() + word[1:] in known
                or word in self.learned or word in self.ignored)

    def _check(self, word: str, language: str) -> bool:
        self.calls.append(("check", word, language))
        self._boom("check")
        return self.knows(word, language)

    def _suggestions(self, word: str, language: str) -> Sequence[str]:
        self.calls.append(("suggestions", word, language))
        self._boom("suggestions")
        return sorted(known for known in self.words.get(language, ())
                      if known[:1].lower() == word[:1].lower() and abs(len(known) - len(word)) <= 1)

    def _learn(self, word: str, language: str) -> None:
        self.calls.append(("learn", word, language))
        self._boom("learn")
        self.learned.append(word)

    def _ignore(self, word: str, language: str) -> None:
        self.calls.append(("ignore", word, language))
        self._boom("ignore")
        self.ignored.append(word)

    # A system that says what it learned (macOS, Enchant), or None for one
    # that cannot tell (Windows).
    tells_learned = True

    def _unlearn(self, word: str, language: str):
        self.calls.append(("unlearn", word, language))
        self._boom("unlearn")
        if word not in self.learned:
            return False
        self.learned.remove(word)
        return True

    def _has_learned(self, word: str, language: str):
        return (word in self.learned) if self.tells_learned else None

    def checked_words(self) -> List[str]:
        return [call[1] for call in self.calls if call[0] == "check"]


class FakeTextBackend(FakeBackend):
    """Checks a whole paragraph in one pass, the way macOS and Windows do,
    and tells German from English by a few common words."""

    name = "fake-text"

    def _check_text(self, text: str, language: str) -> Optional[TextCheck]:
        self.calls.append(("check_text", text, language))
        self._boom("check_text")
        text.encode("utf-16-le")        # as strict as macOS: a lone surrogate fails
        if language == AUTOMATIC:
            language = self.identify(text)
        languages = list(self.words) if language == AUTOMATIC else [language]
        misspelled = tuple((m.start(), m.end()) for m in _SYSTEM_WORD.finditer(text)
                           if not any(self.knows(m.group(0), each) for each in languages))
        return TextCheck(misspelled, language)

    def _resolve(self, language: str) -> str:
        return language or AUTOMATIC

    @staticmethod
    def identify(text: str) -> str:
        found = set(_WORD.findall(text))
        if found & {"das", "Das", "ist", "und", "ein"}:
            return "de-DE"
        if found & {"the", "is", "and", "this"}:
            return "en-US"
        return AUTOMATIC

    def checked_texts(self) -> List[str]:
        return [call[1] for call in self.calls if call[0] == "check_text"]


def drain(spelling):
    for _ in range(1000):
        QCoreApplication.processEvents()
        if not spelling.is_checking():
            return
    raise AssertionError("spell checking never finished")


class Editor:
    """A QTextEdit wired the way docs/develop/spelling.md says: it tells the
    service where its cursor is, keeps its underlines as text cursors
    (which is what extra selections are, and they move and grow with
    edits), and redraws only the blocks ``misspellingsChanged`` names.
    ``underlined`` is what it shows."""

    def __init__(self, backend=None):
        self.edit = QTextEdit()
        self.edit.show()
        self.checker = SpellChecker(backend or FakeBackend())
        self.spelling = DocumentSpelling(self.checker, self.edit.document())
        self.marks: List[QTextCursor] = []
        self.spelling.misspellingsChanged.connect(self.redraw)
        self.edit.cursorPositionChanged.connect(
            lambda: self.spelling.set_cursor_position(self.edit.textCursor().position()))

    def redraw(self, first, last):
        doc = self.edit.document()
        self.marks = [mark for mark in self.marks
                      if not first <= doc.findBlock(mark.selectionStart()).blockNumber() <= last]
        for number in range(first, last + 1):
            block = doc.findBlockByNumber(number)
            for misspelling in self.spelling.misspellings(block):
                mark = QTextCursor(doc)
                mark.setPosition(block.position() + misspelling.start)
                mark.setPosition(block.position() + misspelling.end,
                                 QTextCursor.MoveMode.KeepAnchor)
                self.marks.append(mark)

    def type(self, keys):
        for key in keys:
            if key == "\n":
                QTest.keyClick(self.edit, Qt.Key.Key_Return)
            else:
                QTest.keyClicks(self.edit, key)
            drain(self.spelling)

    def underlined(self):
        """The underlined text of each block."""
        doc = self.edit.document()
        out = [[] for _ in range(doc.blockCount())]
        for mark in sorted(self.marks, key=lambda mark: mark.selectionStart()):
            if mark.hasSelection():
                out[doc.findBlock(mark.selectionStart()).blockNumber()].append(
                    mark.selectedText())
        return out
