# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A group picture's caption as runs, and how its words are changed.

A caption is held as runs (photo_group.Unit): pieces in one format each,
what Markdown can say of them (bold, italic, struck out, code, a link, a
soft line break). Its words are changed as plain words: in the caption
field, which shows them on one line (line), by Replace and by a spelling
correction (replaced). What a change does not touch keeps its run, so
the bold words, the links and the line breaks a file gave a caption stay
as they were; new words take the format of the place they are written
in, as typing does in the text (a link does not grow at its ends).
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, Tuple

from PySide6.QtGui import QFont, QGuiApplication, QTextCharFormat

from doc_walk import ESCAPED_TEXT_PROPERTY, SOFT_BREAK_PROPERTY, TYPED_TEXT_PROPERTY, doc_length


class CaptionRun(NamedTuple):
    """A piece of a caption in one format: what Markdown can say of it."""

    text: str
    bold: bool = False
    italic: bool = True
    strike: bool = False
    code: bool = False
    href: str = ""
    soft: bool = False          # a soft line break, kept as the file had it
    typed: bool = False         # written in the caption field: text (TYPED_TEXT_PROPERTY)
    escaped: bool = False       # escaped in the file (ESCAPED_TEXT_PROPERTY)

    def format(self) -> QTextCharFormat:
        """The format a reader of Markdown gives such a piece."""
        fmt = QTextCharFormat()
        if self.bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if self.italic:
            fmt.setFontItalic(True)
        if self.strike:
            fmt.setFontStrikeOut(True)
        if self.code:
            fmt.setFontFixedPitch(True)
        if self.href:
            fmt.setAnchor(True)
            fmt.setAnchorHref(self.href)
            fmt.setForeground(QGuiApplication.palette().link())
        if self.soft:
            fmt.setProperty(SOFT_BREAK_PROPERTY, True)
        if self.typed:
            fmt.setProperty(TYPED_TEXT_PROPERTY, True)
        if self.escaped:
            fmt.setProperty(ESCAPED_TEXT_PROPERTY, True)
        return fmt


def one_line(text: str) -> str:
    """``text`` as a picture's field shows it and gives it back: on one
    line, every run of spaces (a line break too) one space, none around."""
    return " ".join(text.split())


def line(runs: Sequence[CaptionRun]) -> str:
    """The caption's words as the caption field shows them (one_line)."""
    return one_line("".join(run.text for run in runs))


def edited(runs: Sequence[CaptionRun], words: str) -> Tuple[CaptionRun, ...]:
    """``runs`` once their line (``line``) was edited into ``words``: the
    words before and after the change keep their runs, the changed ones
    take the format of the place the change starts in."""
    text = "".join(run.text for run in runs)
    shown, origin = _shown(text)
    if words == shown:
        return tuple(runs)
    if not runs or not words:
        return (CaptionRun(words, typed=True),) if words else ()
    same = _common(shown, words)
    tail = _common(shown[same:][::-1], words[same:][::-1])
    # From right after the last word kept in front to the first one kept
    # behind: the spaces between them were shown as one, and go with it.
    start = origin[same - 1] + 1 if same else 0
    end = origin[len(shown) - tail] if tail else len(text)
    return replaced(runs, doc_length(text[:start]), doc_length(text[:end]),
                    words[same:len(words) - tail])


def replaced(runs: Sequence[CaptionRun], start: int, end: int,
             words: str) -> Tuple[CaptionRun, ...]:
    """``runs`` with their text from ``start`` to ``end`` (UTF-16 units, as
    Qt counts them) replaced by ``words``, in the format of the run the
    range starts in; words put between two runs go with the one before
    (the first, at the start), but not into a link they only touch. The
    words are ``typed``: text, whatever characters they hold."""
    out: List[CaptionRun] = []
    at = 0
    placed = False
    for run in runs:
        length = doc_length(run.text)
        first, last = max(start - at, 0), min(end - at, length)
        if first < last or (not placed and start == end
                            and (at < start <= at + length or start == at == 0)):
            inside = first < last or 0 < first < length
            out.append(run._replace(text=_units(run.text, 0, first)))
            if not placed:
                out.append(run._replace(text=words, soft=False, typed=True, escaped=False,
                                        href=run.href if inside else ""))
            out.append(run._replace(text=_units(run.text, last, None)))
            placed = True
        else:
            out.append(run)
        at += length
    return merged(out)


def trimmed(runs: Sequence[CaptionRun]) -> Tuple[CaptionRun, ...]:
    """``runs`` without the spaces at their two ends (a no-break space and
    a line break too), and without empty runs. A caption is written
    between stars, and a space inside them, or one written beside them as
    ``&#x20;``, keeps readers from seeing a caption at all: STANDUP read
    "*Der Steg *" as words and the group fell apart."""
    out = [run for run in runs if run.text]
    while out and not out[0].text.strip():
        out.pop(0)
    while out and not out[-1].text.strip():
        out.pop()
    if out:
        out[0] = out[0]._replace(text=out[0].text.lstrip())
        out[-1] = out[-1]._replace(text=out[-1].text.rstrip())
    return tuple(out)


def merged(runs: Sequence[CaptionRun]) -> Tuple[CaptionRun, ...]:
    """``runs`` without empty ones, neighbours in the same format as one
    (a soft line break stays a run of its own): a caption that reads and
    looks the same is the same."""
    out: List[CaptionRun] = []
    for run in runs:
        if not run.text:
            continue
        if out and not run.soft and not out[-1].soft and run[1:] == out[-1][1:]:
            out[-1] = out[-1]._replace(text=out[-1].text + run.text)
        else:
            out.append(run)
    return tuple(out)


def _shown(text: str) -> Tuple[str, List[int]]:
    """``text`` as ``line`` shows it, and where in ``text`` each of its
    characters is."""
    chars: List[str] = []
    origin: List[int] = []
    for index, char in enumerate(text):
        space = char.isspace()
        if space and (not chars or chars[-1] == " "):
            continue
        chars.append(" " if space else char)
        origin.append(index)
    if chars and chars[-1] == " ":
        chars.pop()
        origin.pop()
    return "".join(chars), origin


def _common(one: str, other: str) -> int:
    """How many characters ``one`` and ``other`` begin with alike."""
    count = 0
    for mine, theirs in zip(one, other):
        if mine != theirs:
            break
        count += 1
    return count


def _units(text: str, first: int, last: Optional[int]) -> str:
    """``text`` from the UTF-16 offset ``first`` to ``last`` (None: its end)."""
    out: List[str] = []
    units = 0
    for char in text:
        if units >= first and (last is None or units < last):
            out.append(char)
        units += doc_length(char)
    return "".join(out)
