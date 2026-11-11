# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups in a document: pictures that follow each other, held as one.

Two or more pictures with nothing but empty lines between them are a
group (STANDUP issue #115; the design is SPEC.md and EDITING.md of the
photo grid work). The Markdown stays what the author wrote: a picture
paragraph each, with its caption, an italic line, under it or in the
paragraph that follows. Every app that does not know groups shows the
pictures one under another, each with its line, in the author's order.

In a document a group is **one paragraph**, marked as a group (GROUP),
holding **one picture character for each picture**, in the author's
order. A picture keeps its own format (its address, its description);
what else its paragraphs held rides on it as its unit (UNIT): its
caption, and how it was joined to the picture before it. Nothing about
how a group is arranged is in the document: its rows are worked out from
the width and the pictures' sizes whenever it is laid out
(photo_group_view, with the numbers of photo_group_geometry), so a window
resized, or a picture arriving late, changes nothing in the document and
leaves no step in Undo.

- **find_groups** finds the groups as STANDUP's renderers find them,
  **fold** makes each its paragraph, and **unfold** gives the paragraphs
  back as they were. A writer writes a group as its paragraphs
  (group_segments), so it is written as it was read, to the byte.
- **#wide** at the end of a picture's address asks for a picture as wide
  as the page (a drawing, a plan). In a group it gets a row of its own and
  the group goes on. It stays in the Markdown; the picture is loaded
  without it (without_wide).

Editing a document with groups in it (keeping them whole, moving a
picture) is photo_group_edit.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from PySide6.QtCore import QUrl
from PySide6.QtGui import (
    QFont,
    QGuiApplication,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextFormat,
)

import media_files
from doc_walk import LINE_SEPARATOR, OBJECT_REPLACEMENT, SOFT_BREAK_PROPERTY, doc_length

# --------------------------------------------------------------------------- #
# Marks in the document                                                        #
# --------------------------------------------------------------------------- #

_USER = QTextFormat.Property.UserProperty
# A paragraph that is a group (its block format): True.
GROUP = _USER + 0x6A0
# A picture of a group (its character format): the unit as JSON, see
# _unit_json. A letter typed after a picture inherits its format, this mark
# included (QTextCursor::insertText keeps everything but the object type),
# so only an object character carrying it is a unit.
UNIT = _USER + 0x6A3

# How a soft line break is kept in a join (markdown_writer carries soft
# breaks the same way between reading and writing).
SOFT = "\ue0b1"

# A picture's address ending in this asks for a picture as wide as the page.
WIDE_SUFFIX = "#wide"


def is_wide(name: str) -> bool:
    """Whether a picture's address asks for the page's width (#wide), as
    STANDUP reads it: the address ends in it, in any case."""
    return bool(name) and name.lower().endswith(WIDE_SUFFIX)


def without_wide(name: str) -> str:
    """The address to load a picture from: without its #wide."""
    return name[:-len(WIDE_SUFFIX)] if is_wide(name) else name


def without_wide_url(url: QUrl) -> QUrl:
    """``url`` without a #wide fragment, for the code that loads it."""
    if url.fragment().lower() == WIDE_SUFFIX[1:]:
        url = QUrl(url)
        url.setFragment(None)
    return url


# --------------------------------------------------------------------------- #
# Units: a picture and its caption                                             #
# --------------------------------------------------------------------------- #

class CaptionRun(NamedTuple):
    """A piece of a caption in one format: what Markdown can say of it."""

    text: str
    bold: bool = False
    italic: bool = True
    strike: bool = False
    code: bool = False
    href: str = ""
    soft: bool = False          # a soft line break, kept as the file had it

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
        return fmt


_BOLD, _ITALIC, _STRIKE, _CODE, _SOFT_RUN = 1, 2, 4, 8, 16
_numbers = itertools.count(1)


@dataclass
class Unit:
    """One picture of a group, with its caption: the picture's own format
    (its address, its description, a link on it), the caption as runs, and
    how the Markdown joined them."""

    image: QTextCharFormat
    name: str
    alt: str = ""
    caption: Tuple[CaptionRun, ...] = ()
    # The text between the picture and a caption on its paragraph (None:
    # the caption was a paragraph of its own, or there is none).
    caption_join: Optional[str] = None
    # The text joining it to the unit before, when they were one paragraph
    # ("![a](a.png) ![b](b.png)"); soft line breaks as SOFT.
    join: Optional[str] = None
    # Its number in this session: what stands for STANDUP's data-id (SPEC
    # 3.9). A picture keeps it while it stays in a group.
    number: int = 0

    @property
    def wide(self) -> bool:
        return is_wide(self.name)

    @property
    def has_caption(self) -> bool:
        return bool(self.caption)

    @property
    def caption_text(self) -> str:
        return "".join(run.text for run in self.caption)

    def captioned(self, words: str) -> "Unit":
        """This unit with ``words`` as its caption (none for no words): in
        italic, as a caption is; in the paragraph under the picture,
        unless its caption stood on the picture's own line before."""
        caption = (CaptionRun(words),) if words else ()
        return Unit(self.image, self.name, self.alt, caption,
                    self.caption_join if words else None, self.join, self.number)

    def caption_runs(self) -> List[Tuple[str, QTextCharFormat]]:
        """The caption as (text, format) pieces, as a paragraph's are."""
        return [(run.text, run.format()) for run in self.caption]

    def picture_format(self) -> QTextCharFormat:
        """The picture's format outside a group: without the unit's mark."""
        fmt = QTextCharFormat(self.image)
        fmt.clearProperty(UNIT)
        return fmt

    def unit_format(self) -> QTextCharFormat:
        """The picture's format in a group: its own, carrying the unit."""
        if not self.number:
            self.number = next(_numbers)
        fmt = QTextCharFormat(self.image)
        runs = [[run.text, (_BOLD * run.bold | _ITALIC * run.italic | _STRIKE * run.strike
                            | _CODE * run.code | _SOFT_RUN * run.soft), run.href]
                for run in self.caption]
        fmt.setProperty(UNIT, json.dumps([self.number, self.join, self.caption_join, runs],
                                         ensure_ascii=False, separators=(",", ":")))
        return fmt


def unit_of_picture(fmt: QTextCharFormat) -> Unit:
    """A unit for the picture ``fmt`` (an image's format): with no caption
    yet, or, for a picture that was in a group, the unit it carries."""
    if fmt.hasProperty(UNIT):
        return _unit_of_mark(fmt)
    image = fmt.toImageFormat()
    return Unit(fmt, image.name(), str(image.property(QTextFormat.Property.ImageAltText) or ""))


# What a unit's mark says, by its text: every layout reads the units of the
# groups it shows, and a mark never changes (a new caption is a new mark).
_parsed: Dict[str, tuple] = {}
_PARSED_KEPT = 65536


def _unit_of_mark(fmt: QTextCharFormat) -> Unit:
    raw = fmt.property(UNIT)
    found = _parsed.get(raw)
    if found is None:
        number, join, caption_join, runs = json.loads(raw)
        caption = tuple(CaptionRun(text, bool(flags & _BOLD), bool(flags & _ITALIC),
                                   bool(flags & _STRIKE), bool(flags & _CODE), href,
                                   bool(flags & _SOFT_RUN)) for text, flags, href in runs)
        if len(_parsed) >= _PARSED_KEPT:
            _parsed.clear()
        found = _parsed[raw] = (number, join, caption_join, caption)
    number, join, caption_join, caption = found
    image = fmt.toImageFormat()
    return Unit(fmt, image.name(), str(image.property(QTextFormat.Property.ImageAltText) or ""),
                caption, caption_join, join, number)


# --------------------------------------------------------------------------- #
# Reading a paragraph                                                          #
# --------------------------------------------------------------------------- #

def is_group_block(block) -> bool:
    """Whether ``block`` is a group's paragraph."""
    return block.blockFormat().boolProperty(GROUP)


def has_groups(doc) -> bool:
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().boolProperty(GROUP):
            return True
        block = block.next()
    return False


# A stretch of a paragraph: the units that follow each other in it, or what
# else it holds (text, another object) as (position, text, format) pieces.
Segment = Union[List[Unit], List[Tuple[int, str, QTextCharFormat]]]


def block_segments(block) -> Optional[List[Tuple[int, Segment]]]:
    """``block`` as stretches of units and of anything else, each with
    where it starts; None for a paragraph without a unit. A group's
    paragraph is one stretch of units; an edit may leave more (text typed
    or dropped between its pictures), until photo_group_edit puts it
    right, and a writer still writes all of it."""
    if OBJECT_REPLACEMENT not in block.text():
        return None
    out: List[Tuple[int, Segment]] = []
    units = False
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        it += 1
        if not fragment.isValid():
            continue
        fmt = fragment.charFormat()
        text = fragment.text()
        position = fragment.position()
        if not fmt.hasProperty(UNIT):
            _extend(out, position, [(position, text, fmt)], False)
            continue
        for offset, char in enumerate(text):
            if char == OBJECT_REPLACEMENT:
                units = True
                _extend(out, position + offset, [_unit_of_mark(fmt)], True)
            else:
                _extend(out, position + offset, [(position + offset, char, fmt)], False)
    return out if units else None


def _extend(out: list, position: int, items: list, is_units: bool) -> None:
    if out and isinstance(out[-1][1][0], Unit) == is_units:
        out[-1][1].extend(items)
    else:
        out.append((position, items))


def block_units(block) -> List[Unit]:
    """The units in ``block``, in order (a group's paragraph: its pictures)."""
    return [unit for _position, units in unit_stretches(block) for unit in units]


def unit_stretches(block) -> List[Tuple[int, List[Unit]]]:
    """The stretches of units in ``block``, each with its first position."""
    return [(position, segment) for position, segment in block_segments(block) or ()
            if isinstance(segment[0], Unit)]


def is_plain_paragraph(block) -> bool:
    """A paragraph outside lists, quotes, headings and code: where a
    picture paragraph of a group is. (Whether it is at the top of the
    document, outside a table, is for the caller: _top_paragraphs.)"""
    if block.textList() is not None:
        return False
    fmt = block.blockFormat()
    if fmt.headingLevel():
        return False
    quote = fmt.property(QTextFormat.Property.BlockQuoteLevel)
    if isinstance(quote, int) and quote > 0:
        return False
    if fmt.property(QTextFormat.Property.BlockCodeFence) or fmt.hasProperty(
            QTextFormat.Property.BlockCodeLanguage):
        return False
    return not fmt.hasProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth)


def _pieces(block) -> List[Tuple[int, str, QTextCharFormat]]:
    """``(position, text, format)`` of each piece of ``block``, a picture
    always a piece of its own."""
    out = []
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        it += 1
        if not fragment.isValid():
            continue
        text, fmt, position = fragment.text(), fragment.charFormat(), fragment.position()
        if len(text) > 1 and OBJECT_REPLACEMENT in text and fmt.isObjectFormat():
            out.extend((position + offset, char, fmt) for offset, char in enumerate(text))
        else:
            out.append((position, text, fmt))
    return out


def is_picture(text: str, fmt: QTextCharFormat) -> bool:
    """A picture's character (not audio or video, which have a card)."""
    if text != OBJECT_REPLACEMENT or not fmt.isImageFormat():
        return False
    name = fmt.toImageFormat().name()
    return bool(name) and not media_files.is_audio_or_video(name)


def _join_text(pieces) -> Optional[str]:
    """The text of pieces that join two parts of a paragraph (spaces and
    line breaks only), soft breaks as SOFT; None for anything else."""
    out = []
    for _position, text, fmt in pieces:
        if text.strip(" \t" + LINE_SEPARATOR):
            return None
        if fmt.boolProperty(SOFT_BREAK_PROPERTY):
            text = text.replace(LINE_SEPARATOR, SOFT)
        out.append(text)
    return "".join(out)


def join_runs(join: str) -> List[Tuple[str, QTextCharFormat]]:
    """A join as the (text, format) pieces a paragraph holds it in."""
    out = []
    for char in join:
        fmt = QTextCharFormat()
        if char == SOFT:
            fmt.setProperty(SOFT_BREAK_PROPERTY, True)
            char = LINE_SEPARATOR
        out.append((char, fmt))
    return out


def _is_caption_pieces(pieces) -> bool:
    """Words in italic and nothing else (STANDUP's one emphasis): no
    picture, no upright text between them."""
    if not pieces or not "".join(text for _p, text, _f in pieces).strip():
        return False
    return all(OBJECT_REPLACEMENT not in text and fmt.fontItalic() and not fmt.isImageFormat()
               for _p, text, fmt in pieces)


def _caption_of(pieces) -> Tuple[CaptionRun, ...]:
    return tuple(CaptionRun(text, fmt.fontWeight() >= QFont.Weight.DemiBold, fmt.fontItalic(),
                            fmt.fontStrikeOut(), bool(fmt.fontFixedPitch()),
                            (fmt.anchorHref() if fmt.isAnchor() else "") or "",
                            fmt.boolProperty(SOFT_BREAK_PROPERTY))
                 for _position, text, fmt in pieces)


def _after_join(pieces):
    """``pieces`` split where the spaces and line breaks at their start
    end: (the join's pieces, the rest)."""
    chars = [(position + offset, char, fmt) for position, text, fmt in pieces
             for offset, char in enumerate(text)]
    start = 0
    while start < len(chars) and not chars[start][1].strip(" \t" + LINE_SEPARATOR):
        start += 1

    def merged(some):
        out = []
        for position, char, fmt in some:
            if out and out[-1][2] == fmt:
                out[-1] = (out[-1][0], out[-1][1] + char, fmt)
            else:
                out.append((position, char, fmt))
        return out
    return merged(chars[:start]), merged(chars[start:])


def picture_units(block, text: Optional[str] = None) -> Optional[List[Unit]]:
    """The units of a picture paragraph: one picture with an optional
    caption on it, or two or more pictures with only spaces and line
    breaks between them; None for any other paragraph."""
    if text is None:
        text = block.text()
    if not text or text[0] != OBJECT_REPLACEMENT or not is_plain_paragraph(block):
        return None
    if len(text) == 1:
        # One character, the common case: no other piece to look at.
        fmt = block.begin().fragment().charFormat()
        return [unit_of_picture(fmt)] if is_picture(text, fmt) else None
    pieces = _pieces(block)
    if not pieces or not is_picture(pieces[0][1], pieces[0][2]):
        return None
    pictures = [n for n, (_p, char, fmt) in enumerate(pieces) if is_picture(char, fmt)]
    if len(pictures) == 1:
        join_pieces, words = _after_join(pieces[1:])
        join = _join_text(join_pieces)
        if join is None or not _is_caption_pieces(words):
            return None
        unit = unit_of_picture(pieces[0][2])
        unit.caption = _caption_of(words)
        unit.caption_join = join
        return [unit]
    # Two or more pictures and nothing else (STANDUP's image run).
    if pictures[-1] != len(pieces) - 1:
        return None
    units = [unit_of_picture(pieces[0][2])]
    for before, n in zip(pictures, pictures[1:]):
        join = _join_text(pieces[before + 1:n])
        if join is None:
            return None
        unit = unit_of_picture(pieces[n][2])
        unit.join = join
        units.append(unit)
    return units


def caption_paragraph(block, text: Optional[str] = None) -> Optional[Tuple[CaptionRun, ...]]:
    """The caption a paragraph of italic words alone is, for the picture
    above it; None for any other paragraph."""
    if text is None:
        text = block.text()
    if not text or OBJECT_REPLACEMENT in text or not text.strip():
        return None
    if not block.begin().fragment().charFormat().fontItalic() or not is_plain_paragraph(block):
        return None
    pieces = _pieces(block)
    return _caption_of(pieces) if _is_caption_pieces(pieces) else None


# --------------------------------------------------------------------------- #
# Finding the groups                                                           #
# --------------------------------------------------------------------------- #

_SEPARATORS = re.compile("[\u2029\ufdd0\ufdd1]")
_BEYOND_BMP = re.compile("[\U00010000-\U0010FFFF]")

# (position, text) of a paragraph at the top of a document; (position,
# None) where a table or a frame stands.
_Paragraph = Tuple[int, Optional[str]]


def _top_paragraphs(doc) -> List[_Paragraph]:
    """Every paragraph at the top of ``doc``, in order, from the
    document's raw text: a paragraph that is no picture's costs no call
    into the document."""
    raw = doc.toRawText()
    frames = [(frame.firstPosition(), frame.lastPosition())
              for frame in doc.rootFrame().childFrames()]
    # A character beyond U+FFFF takes two positions in the document.
    length = doc_length if _BEYOND_BMP.search(raw) else len
    out: List[_Paragraph] = []
    position = 0
    frame = 0
    last = 0
    for found in _SEPARATORS.finditer(raw):
        text = raw[last:found.start()]
        last = found.end()
        start = position
        position += length(text) + 1
        if frames:
            while frame < len(frames) and frames[frame][1] < start:
                frame += 1
            if frame < len(frames) and frames[frame][0] <= start:
                if not out or out[-1][1] is not None:
                    out.append((start, None))
                continue
        out.append((start, text))
    out.append((position, raw[last:]))
    return out


def _paragraphs_between(doc, first: int, last: int) -> List[_Paragraph]:
    """The paragraphs at the top of ``doc`` from the one at ``first`` to
    the one at ``last``, read block by block: for a few paragraphs of a
    long document."""
    out: List[_Paragraph] = []
    block = doc.findBlock(first)
    while block.isValid() and block.position() <= last:
        if doc.frameAt(block.position()).parentFrame() is None:
            out.append((block.position(), block.text()))
        elif not out or out[-1][1] is not None:
            out.append((block.position(), None))
        block = block.next()
    return out


@dataclass
class Found:
    """A run of units found in a document: the units, the paragraphs they
    take up, and whether it is already one well-formed group paragraph."""

    units: List[Unit]
    first: int          # position of its first paragraph
    end: int            # end of its last paragraph (before the break)
    paragraphs: int = 0
    groups: int = 0     # how many of its paragraphs are group paragraphs
    format: QTextBlockFormat = field(default_factory=QTextBlockFormat)
    # Where its paragraphs that hold pictures start and end (not a
    # caption's paragraph).
    pictures: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return self.paragraphs == 1 and self.groups == 1 and len(self.units) >= 2


def find_runs(doc, first: Optional[int] = None, last: Optional[int] = None, *,
              lone: bool = True) -> List[Found]:
    """The runs of units at the top of ``doc`` (with ``first`` and
    ``last``: among the paragraphs from the one at ``first`` to the one at
    ``last``): picture paragraphs and group paragraphs that follow each
    other with nothing between them. A lone picture takes the italic
    paragraph under it as its caption; a group's paragraph takes none (its
    captions are in it). Without ``lone`` only group paragraphs count,
    each a run of its own."""
    found: List[Found] = []
    run: Optional[Found] = None
    items = (_top_paragraphs(doc) if first is None
             else _paragraphs_between(doc, first, last if last is not None else first))
    count = len(items)
    i = 0
    while i < count:
        position, text = items[i]
        i += 1
        if not text or text[0] != OBJECT_REPLACEMENT:
            run = None
            continue
        block = doc.findBlock(position)
        fmt = block.blockFormat()
        group = fmt.boolProperty(GROUP)
        if group:
            segments = block_segments(block) or []
            units = segments[0][1] if len(segments) == 1 and isinstance(
                segments[0][1][0], Unit) else None
        else:
            units = picture_units(block, text) if lone else None
        if units is None:
            run = None
            continue
        end = position + block.length() - 1
        if (not group and len(units) == 1 and not units[0].has_caption and i < count
                and items[i][1]):
            caption = caption_paragraph(doc.findBlock(items[i][0]), items[i][1])
            if caption is not None:
                units[0].caption = caption
                end = items[i][0] + doc_length(items[i][1])
                i += 1
        if run is None or not lone:
            run = Found([], position, end, format=own_format(block))
            found.append(run)
        run.units.extend(units)
        run.pictures.append((position, position + block.length() - 1))
        run.end = end
        run.paragraphs += 1
        run.groups += group
    return found


def find_groups(doc) -> List[Found]:
    """The groups of a document read from Markdown, as STANDUP's md.py and
    designBlocks.js find them: a picture paragraph (with its caption on
    it, or in the italic paragraph that follows), then the next one, with
    nothing but empty lines between them in the Markdown; two or more."""
    return [run for run in find_runs(doc) if len(run.units) >= 2]


# --------------------------------------------------------------------------- #
# Fold and unfold                                                              #
# --------------------------------------------------------------------------- #

def write_group(cursor: QTextCursor, units: Sequence[Unit], own: QTextBlockFormat) -> None:
    """``units`` as one group paragraph, at ``cursor`` in an empty
    paragraph; ``own`` is the format their paragraphs have outside it."""
    fmt = QTextBlockFormat(own)
    fmt.setProperty(GROUP, True)
    cursor.setBlockFormat(fmt)
    for unit in units:
        cursor.insertText(OBJECT_REPLACEMENT, unit.unit_format())


def write_paragraphs(cursor: QTextCursor, units: Sequence[Unit], own: QTextBlockFormat) -> None:
    """``units`` as the paragraphs Markdown holds, from ``cursor`` (at the
    start of an empty paragraph): each picture's paragraph, its caption
    on it or after it; pictures that were one paragraph, one again."""
    cursor.setBlockFormat(own)
    first = True
    for unit in units:
        if unit.join is not None and not first:
            for text, fmt in join_runs(unit.join):
                cursor.insertText(text, fmt)
        elif not first:
            cursor.insertBlock(own, QTextCharFormat())
        first = False
        cursor.insertText(OBJECT_REPLACEMENT, unit.picture_format())
        if not unit.has_caption:
            continue
        if unit.caption_join is not None:
            for text, fmt in join_runs(unit.caption_join):
                cursor.insertText(text, fmt)
        else:
            cursor.insertBlock(own, QTextCharFormat())
        for text, fmt in unit.caption_runs():
            cursor.insertText(text, fmt)


def replace_run(cursor: QTextCursor, run: Found, units: Sequence[Unit]) -> None:
    """Write ``units`` where ``run`` stands: two or more as one group
    paragraph, one as its own paragraphs, none as an empty paragraph."""
    cursor.setPosition(run.first)
    cursor.setPosition(run.end, QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    if len(units) >= 2:
        write_group(cursor, units, run.format)
    else:
        write_paragraphs(cursor, units, run.format)


def fold(doc: QTextDocument) -> int:
    """Make every group of ``doc`` its paragraph; how many groups it has."""
    # Found inside the edit block too: a cursor placed outside one asks the
    # layout where it stands, and on a shown editor that lays out every
    # paragraph the fold is about to replace.
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    runs = [run for run in find_runs(doc) if len(run.units) >= 2]
    for run in reversed(runs):
        if not run.is_group:
            replace_run(cursor, run, run.units)
    cursor.endEditBlock()
    return len(runs)


@dataclass
class Group:
    """A group of a document: where its paragraph starts and ends, the
    number of its first picture, and its units."""

    start: int
    end: int
    number: int
    units: List[Unit]
    format: QTextBlockFormat


def _group_of(block) -> Optional[Group]:
    units = block_units(block)
    if not units:
        return None
    return Group(block.position(), block.position() + block.length() - 1, units[0].number,
                 units, own_format(block))


def groups_in(doc) -> List[Group]:
    """The groups of ``doc``, in order."""
    out: List[Group] = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().boolProperty(GROUP):
            group = _group_of(block)
            if group is not None:
                out.append(group)
        block = block.next()
    return out


def captions(doc) -> List[str]:
    """The captions of the groups of ``doc``, in order: words a reader
    sees that the document's text does not hold."""
    out: List[str] = []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().boolProperty(GROUP):
            out.extend(unit.caption_text for unit in block_units(block) if unit.has_caption)
        block = block.next()
    return out


def group_at(doc, position: int) -> Optional[Group]:
    """The group whose paragraph holds ``position``, or None."""
    block = doc.findBlock(position)
    return _group_of(block) if block.isValid() and is_group_block(block) else None


def own_format(block) -> QTextBlockFormat:
    """The format of ``block`` as a paragraph outside a group."""
    own = QTextBlockFormat(block.blockFormat())
    own.clearProperty(GROUP)
    return own


def isolate(cursor: QTextCursor, block):
    """Split ``block`` so that every stretch of units in it stands in a
    paragraph of its own (a group's already does), from its last stretch
    to its first; yields each as (start, end, units) once it stands so,
    for the caller to rewrite before the next is split off."""
    segments = block_segments(block) or []
    own = own_format(block)
    end = block.position() + block.length() - 1
    for n in range(len(segments) - 1, -1, -1):
        position, segment = segments[n]
        if not isinstance(segment[0], Unit):
            continue
        stop = segments[n + 1][0] if n + 1 < len(segments) else end
        if n + 1 < len(segments):
            cursor.setPosition(stop)
            cursor.insertBlock(own, QTextCharFormat())
        if n:
            cursor.setPosition(position)
            cursor.insertBlock(own, QTextCharFormat())
            position, stop = position + 1, stop + 1
        yield position, stop, segment


def unfold(doc: QTextDocument) -> int:
    """Turn every group of ``doc`` back into its paragraphs; how many. A
    paragraph that holds units without being a group's (the start of a
    selection copied from inside one) is unfolded with them."""
    found = []
    block = doc.begin()
    while block.isValid():
        if block_segments(block) is not None:
            found.append(block.position())
        block = block.next()
    if not found:
        return 0
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for position in reversed(found):
        block = doc.findBlock(position)
        own = own_format(block)
        for start, end, units in isolate(cursor, block):
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            write_paragraphs(cursor, units, own)
    cursor.endEditBlock()
    return len(found)


def copy_of(doc: QTextDocument) -> QTextDocument:
    """A copy of ``doc``'s text and formats, freed once dropped. (PySide
    keeps a clone() alive as long as the document it was cloned from.)"""
    copy = QTextDocument()
    copy.setDefaultFont(doc.defaultFont())
    copy.setDefaultTextOption(doc.defaultTextOption())
    copy.setIndentWidth(doc.indentWidth())
    copy.setDocumentMargin(doc.documentMargin())
    QTextCursor(copy).insertFragment(QTextDocumentFragment(doc))
    copy.rootFrame().setFrameFormat(doc.rootFrame().frameFormat())
    return copy


def unfolded(doc: QTextDocument) -> QTextDocument:
    """``doc`` itself when it holds no group; else a copy of it with every
    group as its paragraphs."""
    if not has_groups(doc):
        return doc
    copy = copy_of(doc)
    unfold(copy)
    return copy


def unfolded_fragment(fragment: QTextDocumentFragment) -> QTextDocumentFragment:
    """``fragment`` (a selection) with the groups in it as their
    paragraphs: what a copy gives other apps and a paste here reads. A
    selection that starts inside a group brings its pictures without the
    paragraph's mark (a fragment keeps the characters of its first
    paragraph, not its format): they are unfolded as well."""
    doc = QTextDocument()
    QTextCursor(doc).insertFragment(fragment)
    return QTextDocumentFragment(doc) if unfold(doc) else fragment
