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

- photo_group_find finds the groups as STANDUP's renderers find them and
  **folds** each into its paragraph; **unfold** here gives the paragraphs
  back as they were. A writer writes a group as its paragraphs
  (block_segments), so it is written as it was read, to the byte.
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
import posixpath
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

from PySide6.QtCore import QUrl
from PySide6.QtGui import (
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextFormat,
)

import photo_caption
from doc_walk import LINE_SEPARATOR, OBJECT_REPLACEMENT, SOFT_BREAK_PROPERTY
from photo_caption import CaptionRun

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


def description_of(alt: str, name: str) -> str:
    """``alt``, the description of the picture at ``name``, when it
    describes it; "" when it says nothing a person could not see: empty,
    "image" (what the app and Qt's writer put for none) or the file's own
    name, with or without its extension (what the app gave a picture it
    added, before)."""
    words = alt.strip()
    file = posixpath.basename(without_wide(name).replace("\\", "/").split("?")[0])
    if words.casefold() in ("", "image", file.casefold(),
                            posixpath.splitext(file)[0].casefold()):
        return ""
    return words


def without_wide_url(url: QUrl) -> QUrl:
    """``url`` without a #wide fragment, for the code that loads it."""
    if url.fragment().lower() == WIDE_SUFFIX[1:]:
        url = QUrl(url)
        url.setFragment(None)
    return url


# --------------------------------------------------------------------------- #
# Units: a picture and its caption                                             #
# --------------------------------------------------------------------------- #

_BOLD, _ITALIC, _STRIKE, _CODE, _SOFT_RUN, _TYPED = 1, 2, 4, 8, 16, 32
_numbers = itertools.count(1)


@dataclass
class Unit:
    """One picture of a group, with its caption: the picture's own format
    (its address, its description, a link on it), the caption as runs, and
    how the Markdown joined them."""

    image: QTextCharFormat
    name: str
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
    def alt(self) -> str:
        """The picture's description (its alt text)."""
        return str(self.image.toImageFormat().property(QTextFormat.Property.ImageAltText) or "")

    @property
    def description(self) -> str:
        """The description when it describes the picture (description_of)."""
        return description_of(self.alt, self.name)

    @property
    def wide(self) -> bool:
        return is_wide(self.name)

    @property
    def has_caption(self) -> bool:
        return bool(self.caption)

    @property
    def caption_text(self) -> str:
        return "".join(run.text for run in self.caption)

    @property
    def caption_line(self) -> str:
        """The caption's words on one line, as the caption field shows them."""
        return photo_caption.line(self.caption)

    def captioned(self, words: str) -> "Unit":
        """This unit with its caption's line edited into ``words`` (none for
        no words): what the edit left keeps its format (photo_caption.edited);
        a new caption is in italic, as a caption is."""
        return self.with_caption(photo_caption.edited(self.caption, words))

    def with_caption(self, caption: Sequence[CaptionRun]) -> "Unit":
        """This unit with the runs ``caption`` as its caption (none for no
        runs), in the paragraph under the picture, unless its caption stood
        on the picture's own line before."""
        caption = tuple(run for run in caption if run.text)
        return Unit(self.image, self.name, caption,
                    self.caption_join if caption else None, self.join, self.number)

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
        if not self.caption and self.join is None:
            # A picture alone in its paragraph, the common one: the same
            # text json.dumps writes, without asking it.
            fmt.setProperty(UNIT, f"[{self.number},null,null,[]]")
            return fmt
        runs = [[run.text, (_BOLD * run.bold | _ITALIC * run.italic | _STRIKE * run.strike
                            | _CODE * run.code | _SOFT_RUN * run.soft | _TYPED * run.typed),
                 run.href]
                for run in self.caption]
        fmt.setProperty(UNIT, json.dumps([self.number, self.join, self.caption_join, runs],
                                         ensure_ascii=False, separators=(",", ":")))
        return fmt


def unit_of_picture(fmt: QTextCharFormat) -> Unit:
    """A unit for the picture ``fmt`` (an image's format): with no caption
    yet, or, for a picture that was in a group, the unit it carries."""
    if fmt.hasProperty(UNIT):
        return _unit_of_mark(fmt)
    return Unit(fmt, fmt.toImageFormat().name())


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
                                   bool(flags & _SOFT_RUN), bool(flags & _TYPED))
                        for text, flags, href in runs)
        if len(_parsed) >= _PARSED_KEPT:
            _parsed.clear()
        found = _parsed[raw] = (number, join, caption_join, caption)
    number, join, caption_join, caption = found
    return Unit(fmt, fmt.toImageFormat().name(), caption, caption_join, join, number)


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


def is_plain_paragraph(block, fmt: Optional[QTextBlockFormat] = None) -> bool:
    """A paragraph outside lists, quotes, headings and code: where a
    picture paragraph of a group is; ``fmt`` is its format when the caller
    has it. (Whether it is at the top of the document, outside a table, is
    for the caller: _top_paragraphs.)"""
    if block.textList() is not None:
        return False
    if fmt is None:
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


def joins_previous(previous: Optional[Unit], unit: Unit) -> bool:
    """Whether ``unit`` is written on the paragraph of the unit before it:
    it was read so (its join), and that unit has no caption paragraph of
    its own, after which a picture starts a paragraph (it would be in the
    caption's words, and the group gone, when the file is read again)."""
    return (previous is not None and unit.join is not None
            and not (previous.has_caption and previous.caption_join is None))


def write_paragraphs(cursor: QTextCursor, units: Sequence[Unit], own: QTextBlockFormat) -> None:
    """``units`` as the paragraphs Markdown holds, from ``cursor`` (at the
    start of an empty paragraph): each picture's paragraph, its caption
    on it or after it; pictures that were one paragraph, one again."""
    cursor.setBlockFormat(own)
    previous: Optional[Unit] = None
    for unit in units:
        if joins_previous(previous, unit):
            for text, fmt in join_runs(unit.join):
                cursor.insertText(text, fmt)
        elif previous is not None:
            cursor.insertBlock(own, QTextCharFormat())
        previous = unit
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


def replace_paragraphs(cursor: QTextCursor, first: int, end: int, own: QTextBlockFormat,
                       units: Sequence[Unit]) -> None:
    """Write ``units`` in place of the paragraphs from ``first`` to
    ``end``: two or more as one group paragraph, one as its own
    paragraphs, none as an empty paragraph; ``own`` is the format of a
    paragraph of theirs outside a group."""
    cursor.setPosition(first)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    if len(units) >= 2:
        write_group(cursor, units, own)
    else:
        write_paragraphs(cursor, units, own)


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
