# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups in a document: pictures that follow each other, shown as one.

Two or more pictures with nothing but empty lines between them are a
group (STANDUP issue #115; the design is SPEC.md and EDITING.md of the
photo grid work). The Markdown stays what the author wrote: a picture
paragraph each, with its caption, an italic line, under it or in the
paragraph that follows. Every app that does not know groups shows the
pictures one under another, each with its line, in the author's order.

STANDUP's engine (photo_layout.py, vendored unchanged) decides how a
group is shown: rows of pictures at equal height, the whole width of the
text, a tall picture next to a stack of two, a panorama or a ``#wide``
picture on a row of its own. Every surface of this app uses it: the
editor, both previews, the HTML export, PDF and Print.

In a document a group is a run of borderless tables, flagged as a group
(a property of their format): one table per row of the mosaic, a stack as
a table of one column inside its cell. The tables only hold the pictures
and their captions as they were read; the sizes come from a handler for
the document's pictures (install), so a group follows the width of the
window without a change to the document, and so without a step in Undo.
A table each side of a row (a spacer) centres a row narrower than the
text.

- **fold** finds the groups in a document just read from Markdown and
  builds their tables, **unfold** gives the pictures and captions back as
  the paragraphs they were. Every writer works on an unfolded copy, so a
  group is written as it was read, to the byte.
- **narrow**: a read-only surface (the previews, PDF and Print) narrower
  than 600 pixels shows a group as STANDUP does on a phone, with each
  strip unrolled: pictures of a similar shape side by side when they still
  show at 5/8 of their size alone, any other picture on a line of its own.
  The editor keeps the mosaic at every width: its tables are part of the
  document, and Qt records every change to them as a step of Undo.
- **#wide** at the end of a picture's address asks for a picture as wide
  as the page (a drawing, a plan). In a group it gets a row of its own and
  the group goes on. It stays in the Markdown; the picture is loaded
  without it (without_wide).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QRectF, QSizeF, QUrl
from PySide6.QtGui import (
    QFontMetricsF,
    QImage,
    QPixmap,
    QPyTextObject,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextFormat,
    QTextFrameFormat,
    QTextLength,
    QTextTable,
    QTextTableFormat,
)

import media_files
import photo_layout
from doc_walk import LINE_SEPARATOR, OBJECT_REPLACEMENT
from i18n import _

# --------------------------------------------------------------------------- #
# Marks in the document                                                        #
# --------------------------------------------------------------------------- #

_USER = QTextFormat.Property.UserProperty
# A row table of a group: the group's number in its document (an int).
GROUP = _USER + 0x6A0
# How the group is arranged: MOSAIC (rows of the wide layout) or NARROW
# (the runs of a narrow column, strips unrolled).
ARRANGEMENT = _USER + 0x6A1
# A table of one column inside a row's cell: a stack of pictures.
STACK = _USER + 0x6A2
# A picture's paragraph in a group: the unit's number (an int), the
# object id that stands for STANDUP's data-id (SPEC 3.9).
UNIT = _USER + 0x6A3
# The text that joined a picture to the one before it, when the two were
# one paragraph ("![a](a.png) ![b](b.png)"); soft line breaks as SOFT.
JOIN = _USER + 0x6A4
# A caption's paragraph in a group. CAPTION_JOIN, when the caption was on
# its picture's paragraph, is the text between the two (SOFT for a soft
# line break); a caption read as a paragraph of its own has none.
CAPTION = _USER + 0x6A5
CAPTION_JOIN = _USER + 0x6A6
# A paragraph's own margins, kept while its place in a group sets them.
OWN_TOP = _USER + 0x6A7
OWN_BOTTOM = _USER + 0x6A8
# An empty paragraph Qt needs before, between and after tables.
STRUCTURE = _USER + 0x6A9
# The character of a spacer: LEAD or TRAIL side of its row.
SPACER_SIDE = _USER + 0x6AA

SPACER_OBJECT = int(QTextFormat.ObjectTypes.UserObject) + 0x6A
IMAGE_OBJECT = int(QTextFormat.ObjectTypes.ImageObject)

MOSAIC = "mosaic"
NARROW = "narrow"
LEAD = "lead"
TRAIL = "trail"

# How a soft line break is kept in JOIN and CAPTION_JOIN (markdown_writer
# carries soft breaks the same way between reading and writing).
SOFT = ""

# Below this width (CSS pixels) a read-only surface shows the runs (SPEC 2).
NARROW_BELOW = 600

# The share of the screen's height a mosaic row may take: STANDUP's
# --pg-cap, min(560px, 72svh) (backend/htmlgen/article.css). The wide and
# narrow caps are the engine's own (wideCap, phone capVh).
REGULAR_CAP_VH = 0.72

# A caption's paragraph sits this share of the text's font size under its
# picture (the readers: 0.6em of a caption at 0.833em).
CAPTION_GAP_EM = 0.5
# Room above and below a group, in the text's font size: a group stands
# apart from the paragraphs around it, as a figure does (the readers give
# it 2em of their larger type).
GROUP_MARGIN_EM = 1.0

# A picture's address ending in this asks for a picture as wide as the page.
WIDE_SUFFIX = "#wide"

# The text key a stand-in picture carries (a grey box drawn while the
# picture is not at hand): its shape is not the picture's.
PLACEHOLDER_KEY = "myeditor-placeholder"

# Whole slack, in pixels, a row leaves of the text's width: Qt rounds the
# width of each column up, and a row wider than its room is laid out again
# with every word breakable (qtextdocumentlayout.cpp, layoutTable).
_SLACK = 3


# --------------------------------------------------------------------------- #
# #wide and stand-ins                                                          #
# --------------------------------------------------------------------------- #

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


def mark_placeholder(image: QImage) -> QImage:
    """``image``, marked as a stand-in: its shape is not the picture's."""
    image.setText(PLACEHOLDER_KEY, "1")
    return image


def is_placeholder(image) -> bool:
    return isinstance(image, QImage) and image.text(PLACEHOLDER_KEY) == "1"


def picture_size(image) -> Optional[Tuple[int, int]]:
    """The pixel size of a picture at hand (a QImage or QPixmap), or None
    for a stand-in or nothing. The engine decides which sizes it takes."""
    if isinstance(image, QPixmap):
        image = image.toImage()
    if not isinstance(image, QImage) or image.isNull() or is_placeholder(image):
        return None
    return photo_layout.picture_size(image.width(), image.height())


# The size of a picture by its name in the document (its address, #wide
# included), or None while it is not known.
SizeLookup = Callable[[str], Optional[Tuple[int, int]]]


def resource_sizes(doc: QTextDocument) -> SizeLookup:
    """Sizes from the pictures ``doc`` holds, asked for as it asks to draw
    them. Only for a document a view shows: the view loads its pictures
    (by its own rules for local files and addresses); a document of its
    own would read a local name from the disk itself."""
    def size(name: str) -> Optional[Tuple[int, int]]:
        return picture_size(doc.resource(QTextDocument.ResourceType.ImageResource.value,
                                         QUrl(name)))
    return size


def table_sizes(found: Dict[str, object]) -> SizeLookup:
    """Sizes from pictures at hand by name (QImage, QSize or (w, h))."""
    def size(name: str) -> Optional[Tuple[int, int]]:
        value = found.get(name)
        if isinstance(value, (QImage, QPixmap)):
            return picture_size(value)
        if hasattr(value, "width") and hasattr(value, "height"):
            return photo_layout.picture_size(value.width(), value.height())
        if isinstance(value, tuple) and len(value) == 2:
            return photo_layout.picture_size(*value)
        return None
    return size


# --------------------------------------------------------------------------- #
# Units: a picture and its caption                                             #
# --------------------------------------------------------------------------- #

@dataclass
class Unit:
    """One picture of a group, with its caption: what the document held,
    as fragments with their formats, and how it was joined."""

    picture: QTextDocumentFragment
    name: str
    block_format: QTextBlockFormat
    caption: Optional[QTextDocumentFragment] = None
    caption_text: str = ""
    caption_format: Optional[QTextBlockFormat] = None
    # The text between the picture and a caption on its paragraph (None:
    # the caption was a paragraph of its own, or there is none).
    caption_join: Optional[str] = None
    # The text joining it to the unit before, when they were one paragraph.
    join: Optional[str] = None
    number: int = 0
    # The picture's description (its alt text).
    alt: str = ""

    @property
    def wide(self) -> bool:
        return is_wide(self.name)


def _alt(cf) -> str:
    return str(cf.toImageFormat().property(QTextFormat.Property.ImageAltText) or "")


def _is_plain_paragraph(block) -> bool:
    """A paragraph at the top of the document, outside lists, quotes,
    headings, code and tables: where a picture paragraph of a group is."""
    fmt = block.blockFormat()
    if block.textList() is not None or fmt.headingLevel():
        return False
    quote = fmt.property(QTextFormat.Property.BlockQuoteLevel)
    if isinstance(quote, int) and quote > 0:
        return False
    if fmt.property(QTextFormat.Property.BlockCodeFence) or fmt.hasProperty(
            QTextFormat.Property.BlockCodeLanguage):
        return False
    if fmt.hasProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth):
        return False
    return QTextCursor(block).currentTable() is None


def _pieces(block) -> List[Tuple[int, str, QTextCharFormat]]:
    """``(position, text, format)`` of each piece of ``block``."""
    out = []
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid():
            out.append((fragment.position(), fragment.text(), fragment.charFormat()))
        it += 1
    return out


def _is_picture(text: str, fmt: QTextCharFormat) -> bool:
    """A picture's character (not audio or video, which have a card)."""
    if text != OBJECT_REPLACEMENT or not fmt.isImageFormat():
        return False
    name = fmt.toImageFormat().name()
    return bool(name) and not media_files.is_audio_or_video(without_wide(name))


def _join_text(pieces) -> Optional[str]:
    """The text of pieces that join two parts of a paragraph (spaces and
    line breaks only), soft breaks as SOFT; None for anything else."""
    from markdown_writer import SOFT_BREAK_PROPERTY
    out = []
    for _position, text, fmt in pieces:
        if text.strip(" \t" + LINE_SEPARATOR):
            return None
        if fmt.boolProperty(SOFT_BREAK_PROPERTY):
            text = text.replace(LINE_SEPARATOR, SOFT)
        out.append(text)
    return "".join(out)


def _is_caption_pieces(pieces) -> bool:
    """Words in italic and nothing else (STANDUP's one emphasis): no
    picture, no upright text between them."""
    if not pieces or not "".join(text for _p, text, _f in pieces).strip():
        return False
    return all(OBJECT_REPLACEMENT not in text and fmt.fontItalic() and not fmt.isImageFormat()
               for _p, text, fmt in pieces)


def _split_chars(pieces):
    """The pieces one character a piece, for splitting a paragraph where
    a picture is (a picture is always one character)."""
    out = []
    for position, text, fmt in pieces:
        offset = 0
        for char in text:
            out.append((position + offset, char, fmt))
            offset += 2 if ord(char) > 0xFFFF else 1
    return out


def _fragment(doc, start: int, end: int) -> QTextDocumentFragment:
    cursor = QTextCursor(doc)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    return QTextDocumentFragment(cursor)


def _picture_units(doc, block) -> Optional[List[Unit]]:
    """The units of a picture paragraph: one picture with an optional
    caption on it, or two or more pictures with only spaces and line
    breaks between them; None for any other paragraph."""
    if not _is_plain_paragraph(block):
        return None
    pieces = _split_chars(_pieces(block))
    if not pieces or not _is_picture(pieces[0][1], pieces[0][2]):
        return None
    fmt = block.blockFormat()
    pictures = [n for n, (_p, char, cf) in enumerate(pieces) if _is_picture(char, cf)]

    def unit(n: int, join: Optional[str]) -> Unit:
        position, _char, cf = pieces[n]
        return Unit(_fragment(doc, position, position + 1), cf.toImageFormat().name(),
                    QTextBlockFormat(fmt), join=join, alt=_alt(cf))

    if len(pictures) == 1:
        rest = pieces[1:]
        if not rest:
            return [unit(0, None)]
        # The caption starts at the first character that does not join.
        start = 0
        while start < len(rest) and not rest[start][1].strip(" \t" + LINE_SEPARATOR):
            start += 1
        join = _join_text(_merge_chars(rest[:start]))
        words = rest[start:]
        if join is None or not _is_caption_pieces(_merge_chars(words)):
            return None
        found = unit(0, None)
        found.caption = _fragment(doc, words[0][0], block.position() + block.length() - 1)
        found.caption_text = "".join(char for _p, char, _f in words)
        found.caption_format = QTextBlockFormat(fmt)
        found.caption_join = join
        return [found]
    # Two or more pictures and nothing else (STANDUP's image run).
    units = []
    for k, n in enumerate(pictures):
        if k == 0:
            if n != 0:
                return None
            units.append(unit(n, None))
            continue
        join = _join_text(_merge_chars(pieces[pictures[k - 1] + 1:n]))
        if join is None:
            return None
        units.append(unit(n, join))
    if pictures[-1] != len(pieces) - 1:
        return None
    return units


def _merge_chars(chars):
    """One-character pieces back to pieces of one format each."""
    out = []
    for position, char, fmt in chars:
        if out and out[-1][2] == fmt:
            out[-1] = (out[-1][0], out[-1][1] + char, fmt)
        else:
            out.append((position, char, fmt))
    return out


def _caption_paragraph(block) -> bool:
    """A paragraph of italic words alone: the caption of the picture above."""
    return _is_plain_paragraph(block) and _is_caption_pieces(_pieces(block))


def _top_level(doc) -> List:
    """The top of the document in order: blocks, and None for a frame."""
    out = []
    it = doc.rootFrame().begin()
    while not it.atEnd():
        frame = it.currentFrame()
        out.append(None if frame is not None else it.currentBlock())
        it += 1
    return out


@dataclass
class _Found:
    """A group found in a document read from Markdown: its units and the
    paragraphs they take up."""

    units: List[Unit]
    first: int          # position of its first paragraph
    end: int            # end of its last paragraph (before the break)


def find_groups(doc) -> List[_Found]:
    """The runs of two or more units at the top of ``doc``, as STANDUP's
    md.py and designBlocks.js find them: a picture paragraph (with its
    caption on it, or in the italic paragraph that follows), then the next
    one, with nothing but empty lines between them in the Markdown."""
    found: List[_Found] = []
    run: List[Unit] = []
    bounds = [0, 0]

    def flush():
        if len(run) >= 2:
            found.append(_Found(list(run), bounds[0], bounds[1]))
        run.clear()

    items = _top_level(doc)
    i = 0
    while i < len(items):
        block = items[i]
        units = _picture_units(doc, block) if block is not None else None
        if units is None:
            flush()
            i += 1
            continue
        if not run:
            bounds[0] = block.position()
        bounds[1] = block.position() + block.length() - 1
        if len(units) == 1 and units[0].caption is None and units[0].join is None:
            following = items[i + 1] if i + 1 < len(items) else None
            if following is not None and _caption_paragraph(following):
                unit = units[0]
                unit.caption = _fragment(doc, following.position(),
                                         following.position() + following.length() - 1)
                unit.caption_text = following.text()
                unit.caption_format = QTextBlockFormat(following.blockFormat())
                bounds[1] = following.position() + following.length() - 1
                i += 1
        run.extend(units)
        i += 1
    flush()
    return found


# --------------------------------------------------------------------------- #
# The engine's items                                                           #
# --------------------------------------------------------------------------- #

def item(size: Optional[Tuple[int, int]], caption: str, wide: bool) -> dict:
    """One picture as the engine takes it: its pixel size (0 when not
    known), its caption's length in code points, and #wide."""
    w, h = size if size else (0, 0)
    return {"w": w, "h": h, "caption": len(caption), "wide": bool(wide)}


def items_of(units: Sequence[Unit], sizes: Optional[SizeLookup]) -> List[dict]:
    return [item(sizes(unit.name) if sizes else None, unit.caption_text, unit.wide)
            for unit in units]


# --------------------------------------------------------------------------- #
# Building the tables (fold)                                                   #
# --------------------------------------------------------------------------- #

_numbers = itertools.count(1)


def _plain_table_format(columns: int) -> QTextTableFormat:
    fmt = QTextTableFormat()
    fmt.setColumns(columns)
    fmt.setBorder(0)
    fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_None)
    fmt.setCellSpacing(0)
    fmt.setCellPadding(0)
    fmt.setMargin(0)
    fmt.setWidth(QTextLength(QTextLength.Type.VariableLength, 0))
    fmt.setColumnWidthConstraints([])
    return fmt


def _spacer_format(side: str) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setObjectType(SPACER_OBJECT)
    fmt.setProperty(SPACER_SIDE, side)
    return fmt


def _structural(cursor: QTextCursor) -> None:
    """Mark the paragraph at ``cursor`` as one Qt needs around a table."""
    fmt = QTextBlockFormat()
    fmt.setProperty(STRUCTURE, True)
    fmt.setTopMargin(0)
    fmt.setBottomMargin(0)
    cursor.setBlockFormat(fmt)


def _placed(fmt: QTextBlockFormat, top: float) -> QTextBlockFormat:
    """``fmt`` with the margins its place in a group gives it, its own kept."""
    out = QTextBlockFormat(fmt)
    if not out.hasProperty(OWN_TOP):
        out.setProperty(OWN_TOP, fmt.topMargin())
        out.setProperty(OWN_BOTTOM, fmt.bottomMargin())
    out.setTopMargin(top)
    out.setBottomMargin(0)
    return out


def _write_unit(cursor: QTextCursor, unit: Unit, caption_gap: float) -> None:
    """A unit in a cell: its picture's paragraph, then its caption's."""
    fmt = _placed(unit.block_format, 0)
    fmt.setProperty(UNIT, unit.number)
    # Its line never breaks: Qt then keeps the picture's column at least
    # as wide as the picture, and a caption wider than its picture wraps
    # under it (Qt gives a column of words their whole width otherwise,
    # taken from the pictures beside it).
    fmt.setNonBreakableLines(True)
    if unit.join is not None:
        fmt.setProperty(JOIN, unit.join)
    else:
        fmt.clearProperty(JOIN)
    fmt.clearProperty(CAPTION)
    fmt.clearProperty(CAPTION_JOIN)
    cursor.insertFragment(unit.picture)
    cursor.setBlockFormat(fmt)
    if unit.caption is None:
        return
    caption = _placed(unit.caption_format or unit.block_format, caption_gap)
    caption.clearProperty(UNIT)
    caption.clearProperty(JOIN)
    caption.setProperty(CAPTION, True)
    if unit.caption_join is not None:
        caption.setProperty(CAPTION_JOIN, unit.caption_join)
    else:
        caption.clearProperty(CAPTION_JOIN)
    cursor.insertBlock(caption, QTextCharFormat())
    cursor.insertFragment(unit.caption)
    cursor.setBlockFormat(caption)


def _caption_gap(doc) -> float:
    return round(CAPTION_GAP_EM * _font_pixels(doc), 2)


def _font_pixels(doc) -> float:
    font = doc.defaultFont()
    if font.pixelSize() > 0:
        return float(font.pixelSize())
    return font.pointSizeF() * 96.0 / 72.0


@dataclass
class _Row:
    """One table of a group: its cells (each a list of unit indices, two
    or more for a stack) and the engine's name for it."""

    cells: List[List[int]]
    name: str = ""
    mode: str = ""          # a narrow row: single, row or strip


def mosaic_rows(items: List[dict]) -> List[_Row]:
    return [_Row([[cell] if isinstance(cell, int) else list(cell) for cell in row["cells"]],
                 row["name"]) for row in photo_layout.layout(items)]


def narrow_rows(items: List[dict]) -> List[_Row]:
    """The runs of a narrow column, strips unrolled: a run that reads as a
    row is one row; every other picture stands on a line of its own."""
    result = photo_layout.layout(items)
    rows = []
    for segment in photo_layout.phone_segments(result, items):
        if segment["mode"] == "row":
            rows.append(_Row([[i] for i in segment["figures"]], mode="row"))
        else:
            rows.extend(_Row([[i]], mode=segment["mode"]) for i in segment["figures"])
    return rows


def _build(cursor: QTextCursor, units: List[Unit], rows: List[_Row], arrangement: str,
           number: int, gap: float, caption_gap: float, margin: float) -> None:
    """The tables of one group at ``cursor`` (an empty paragraph, which
    stays before them), ``margin`` above and below them."""
    _structural(cursor)
    for r, row in enumerate(rows):
        columns = len(row.cells) + 2
        fmt = _plain_table_format(columns)
        fmt.setProperty(GROUP, number)
        fmt.setProperty(ARRANGEMENT, arrangement)
        fmt.setTopMargin(gap if r else margin)
        if r == len(rows) - 1:
            fmt.setBottomMargin(margin)
        table = cursor.insertTable(1, columns, fmt)
        for c in range(columns):
            cell = table.cellAt(0, c)
            at = cell.firstCursorPosition()
            if c == 0 or c == columns - 1:
                _structural(at)
                at.insertText(OBJECT_REPLACEMENT, _spacer_format(LEAD if c == 0 else TRAIL))
                continue
            members = row.cells[c - 1]
            if c < columns - 2:
                cell_fmt = cell.format().toTableCellFormat()
                cell_fmt.setRightPadding(gap)
                cell.setFormat(cell_fmt)
            if len(members) == 1:
                _write_unit(at, units[members[0]], caption_gap)
                continue
            _structural(at)
            stack_fmt = _plain_table_format(1)
            stack_fmt.setProperty(STACK, True)
            stack = at.insertTable(len(members), 1, stack_fmt)
            for m, index in enumerate(members):
                member = stack.cellAt(m, 0)
                if m:
                    member_fmt = member.format().toTableCellFormat()
                    member_fmt.setTopPadding(gap)
                    member.setFormat(member_fmt)
                _write_unit(member.firstCursorPosition(), units[index], caption_gap)
            _structural(_after(stack))
        cursor = _after(table)
        _structural(cursor)


def _after(frame) -> QTextCursor:
    """A cursor in the paragraph after ``frame``. (Placed, not moved there:
    moving by a block asks the layout for the block's lines, and in an
    edit block that lays the document out again for every group. And from
    the frame's own cursor: a cursor made from ``frame.document()`` leaves
    the caller's handle on an editor's document dead in PySide.)"""
    cursor = frame.lastCursorPosition()
    cursor.setPosition(frame.lastPosition() + 1)
    return cursor


def fold(doc: QTextDocument, sizes: Optional[SizeLookup] = None, *,
         narrow: bool = False) -> int:
    """Build the tables of every group in ``doc``, a document just read
    from Markdown; how many groups there are. ``sizes`` gives the pictures'
    sizes (by default the ones ``doc`` holds or the lookup it was given
    with install); ``narrow`` arranges them for a narrow column."""
    # The pictures are read inside the edit block too: a cursor placed
    # outside one asks the layout where it stands, and on a shown editor
    # that lays out every paragraph the fold is about to replace.
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    groups = find_groups(doc)
    if not groups:
        cursor.endEditBlock()
        return 0
    if sizes is None:
        sizes = sizes_of(doc)
    gap = float(photo_layout.CONSTANTS["phone" if narrow else "regular"]["gap"])
    caption_gap = _caption_gap(doc)
    margin = round(GROUP_MARGIN_EM * _font_pixels(doc), 2)
    for group in reversed(groups):
        units = group.units
        for unit in units:
            unit.number = next(_numbers)
        items = items_of(units, sizes)
        rows = narrow_rows(items) if narrow else mosaic_rows(items)
        cursor.setPosition(group.first)
        cursor.setPosition(group.end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        _build(cursor, units, rows, NARROW if narrow else MOSAIC, next(_numbers), gap,
               caption_gap, margin)
    cursor.endEditBlock()
    return len(groups)


# --------------------------------------------------------------------------- #
# Reading the tables back (unfold)                                             #
# --------------------------------------------------------------------------- #

def is_group_table(frame) -> bool:
    return isinstance(frame, QTextTable) and frame.format().hasProperty(GROUP)


def lays_out_group(table) -> bool:
    """Whether ``table`` is a row of a group or a stack in one."""
    return is_group_table(table) or (isinstance(table, QTextTable)
                                     and table.format().boolProperty(STACK))


def is_structure(block) -> bool:
    return block.blockFormat().boolProperty(STRUCTURE)


def has_groups(doc) -> bool:
    return any(is_group_table(frame) for frame in doc.rootFrame().childFrames())


def _own_format(fmt: QTextBlockFormat) -> QTextBlockFormat:
    """A paragraph's format as it was before it went into a group."""
    out = QTextBlockFormat(fmt)
    if out.hasProperty(OWN_TOP):
        out.setTopMargin(float(out.property(OWN_TOP)))
        out.setBottomMargin(float(out.property(OWN_BOTTOM)))
    for prop in (UNIT, JOIN, CAPTION, CAPTION_JOIN, OWN_TOP, OWN_BOTTOM, STRUCTURE):
        out.clearProperty(prop)
    if fmt.hasProperty(UNIT):
        # Set in a group only (_write_unit): a picture paragraph never has
        # it otherwise (code blocks, which do, are never in a group).
        out.clearProperty(QTextFormat.Property.BlockNonBreakableLines)
    return out


def _blocks_in(doc, frame) -> List:
    """Every paragraph of ``doc`` inside ``frame`` (a table or a cell), in
    order. (A frame is never asked for its document: PySide would make the
    document a child of the frame's wrapper, and freeing the wrapper would
    free the document.)"""
    out = []
    block = doc.findBlock(frame.firstPosition())
    while block.isValid() and block.position() <= frame.lastPosition():
        out.append(block)
        block = block.next()
    return out


def table_units(doc, table) -> List[Unit]:
    """The units in one row table of ``doc`` (or a piece of one), in order."""
    units: List[Unit] = []
    for block in _blocks_in(doc, table):
        fmt = block.blockFormat()
        if fmt.hasProperty(UNIT):
            pieces = _pieces(block)
            picture = next(((p, cf) for p, text, cf in pieces if _is_picture(text, cf)), None)
            if picture is None:
                continue
            join = fmt.property(JOIN)
            units.append(Unit(_fragment(doc, picture[0], picture[0] + 1),
                              picture[1].toImageFormat().name(), _own_format(fmt),
                              join=str(join) if join is not None else None,
                              number=int(fmt.property(UNIT)), alt=_alt(picture[1])))
        elif fmt.boolProperty(CAPTION) and units:
            unit = units[-1]
            join = fmt.property(CAPTION_JOIN)
            if block.length() > 1:
                unit.caption = _fragment(doc, block.position(), block.position() + block.length() - 1)
                unit.caption_text = block.text()
                unit.caption_format = _own_format(fmt)
                unit.caption_join = str(join) if join is not None else None
    return units


@dataclass
class Group:
    """A group of a document: where its tables are, from the paragraph Qt
    keeps before the first to the one after the last, and its units, read
    out. (No table is kept: Python must not hold a table that an edit
    deletes, or freeing it later frees what is no longer there.)"""

    start: int
    end: int
    number: int
    arrangement: str
    units: List[Unit]
    rows: int


def groups_in(doc) -> List[Group]:
    """The groups of ``doc``: its flagged tables at the top, consecutive
    ones of one number a group."""
    out: List[Group] = []
    items = []
    it = doc.rootFrame().begin()
    while not it.atEnd():
        frame = it.currentFrame()
        items.append(frame if frame is not None else it.currentBlock())
        it += 1
    i = 0
    while i < len(items):
        frame = items[i]
        if not is_group_table(frame):
            i += 1
            continue
        number = int(frame.format().property(GROUP))
        tables = [frame]
        j = i + 1
        while j < len(items):
            nxt = items[j]
            if isinstance(nxt, QTextTable):
                if is_group_table(nxt) and int(nxt.format().property(GROUP)) == number:
                    tables.append(nxt)
                    j += 1
                    continue
                break
            if is_structure(nxt) and nxt.length() == 1 and j + 1 < len(items) and \
                    is_group_table(items[j + 1]) and \
                    int(items[j + 1].format().property(GROUP)) == number:
                j += 1
                continue
            break
        before = items[i - 1] if i > 0 else None
        after = items[j] if j < len(items) and not isinstance(items[j], QTextTable) else None
        start = (before.position() if before is not None and is_structure(before)
                 and before.length() == 1 else tables[0].firstPosition() - 1)
        end = (after.position() + after.length() - 1 if after is not None and is_structure(after)
               and after.length() == 1 else tables[-1].lastPosition() + 1)
        out.append(Group(start, end, number, str(frame.format().property(ARRANGEMENT) or MOSAIC),
                         [unit for table in tables for unit in table_units(doc, table)], len(tables)))
        i = j
    items.clear()
    return out


def _insert_join(cursor: QTextCursor, join: str) -> None:
    from markdown_writer import SOFT_BREAK_PROPERTY
    for char in join:
        fmt = QTextCharFormat()
        if char == SOFT:
            fmt.setProperty(SOFT_BREAK_PROPERTY, True)
            char = LINE_SEPARATOR
        cursor.insertText(char, fmt)


def write_paragraphs(cursor: QTextCursor, units: Sequence[Unit]) -> None:
    """``units`` as the paragraphs Markdown holds, from ``cursor`` (at the
    start of an empty paragraph): each picture's paragraph, its caption
    on it or after it; pictures that were one paragraph, one again."""
    first = True
    for unit in units:
        if unit.join is not None and not first:
            _insert_join(cursor, unit.join)
        elif not first:
            cursor.insertBlock(_own_format(unit.block_format), QTextCharFormat())
        first = False
        cursor.insertFragment(unit.picture)
        cursor.setBlockFormat(_own_format(unit.block_format))
        if unit.caption is None:
            continue
        if unit.caption_join is not None:
            _insert_join(cursor, unit.caption_join)
            cursor.insertFragment(unit.caption)
            continue
        caption_format = _own_format(unit.caption_format or unit.block_format)
        cursor.insertBlock(caption_format, QTextCharFormat())
        cursor.insertFragment(unit.caption)
        cursor.setBlockFormat(caption_format)


def unfold(doc: QTextDocument) -> int:
    """Turn every group of ``doc`` back into its paragraphs; how many."""
    groups = groups_in(doc)
    if not groups:
        return 0
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for group in reversed(groups):
        units = group.units
        cursor.setPosition(group.start)
        cursor.setPosition(group.end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.setBlockFormat(QTextBlockFormat())
        write_paragraphs(cursor, units)
    cursor.endEditBlock()
    return len(groups)


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
    paragraphs: what a copy gives other apps and a paste here reads."""
    doc = QTextDocument()
    QTextCursor(doc).insertFragment(fragment)
    if not has_groups(doc):
        return fragment
    unfold(doc)
    return QTextDocumentFragment(doc)


def paragraphs_document(units: Sequence[Unit]) -> QTextDocument:
    """A document holding ``units`` as their paragraphs, and nothing else:
    what a writer writes for a group."""
    doc = QTextDocument()
    write_paragraphs(QTextCursor(doc), units)
    return doc


def refold(doc: QTextDocument, sizes: Optional[SizeLookup] = None, *,
           narrow: bool = False) -> int:
    """Lay every group of ``doc`` out again (new sizes, another width):
    unfold, then fold. For a document nothing edits (a preview, a copy for
    paper); in the editor it would be a step of Undo."""
    unfold(doc)
    return fold(doc, sizes, narrow=narrow)


def arrangement_of(doc) -> Optional[str]:
    """MOSAIC or NARROW for a document with groups, None without."""
    for frame in doc.rootFrame().childFrames():
        if is_group_table(frame):
            return str(frame.format().property(ARRANGEMENT) or MOSAIC)
    return None


# --------------------------------------------------------------------------- #
# Geometry                                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Metrics:
    """The sizes a group is laid out with, in the layout's pixels."""

    width: float            # the room for a row
    gap: float
    cap: float              # tallest row (a #wide row: wide_cap)
    wide_cap: float
    line: float             # one caption line under a picture, with its gap
    caption_min: float      # a narrow captioned picture's least width


@dataclass(frozen=True)
class Place:
    """Where an object of a row goes: its box in the line (width, height),
    and the picture's own box inside it (offset, width, height)."""

    width: float
    height: float
    x: float = 0.0
    picture_width: float = 0.0
    picture_height: float = 0.0


def _number(properties: dict, key: str) -> float:
    return float(properties.get(key, "0"))


def mosaic_places(cells: List[List[int]], items: List[dict], metrics: Metrics) -> Tuple[
        List[List[Place]], float]:
    """The places of one mosaic row's pictures (cell by cell, a stack's
    from the top) and the row's width, by the readers' CSS (SPEC 3.4): the
    row is ``hr = min(cap + rc*line, (100% - rg*gap + rl*line)/rs)`` tall
    and a cell ``r*(hr - k*gap - c*line)`` wide, from the engine's cell
    properties. A #wide picture's cell is the whole width, the picture in
    its middle at most wide_cap tall."""
    engine_cells = [cell[0] if len(cell) == 1 else list(cell) for cell in cells]
    props = photo_layout.cell_properties({"cells": engine_cells}, 0, items)
    W, g, line = metrics.width, metrics.gap, metrics.line
    places: List[List[Place]] = []
    if len(cells) == 1 and len(cells[0]) == 1 and items[cells[0][0]].get("wide") is True:
        ratio = photo_layout.ratio_of(items[cells[0][0]])
        pw = min(W, metrics.wide_cap * ratio)
        return [[Place(W, pw / ratio, (W - pw) / 2, pw, pw / ratio)]], W
    first = props[0]
    rs, rg, rl, rc = (_number(first, key) for key in ("--rs", "--rg", "--rl", "--rc"))
    hr = min(metrics.cap + rc * line, (W - rg * g + rl * line) / rs) if rs > 0 else 0.0
    for cell, prop in zip(cells, props):
        r, k, c = (_number(prop, key) for key in ("--r", "--k", "--c"))
        cw = max(1.0, r * (hr - k * g - c * line))
        out = []
        for index in cell:
            h = cw / photo_layout.ratio_of(items[index])
            out.append(Place(cw, h, 0.0, cw, h))
        places.append(out)
    return places, rs * hr + rg * g - rl * line


def narrow_places(row: List[List[int]], mode: str, items: List[dict], metrics: Metrics) -> Tuple[
        List[List[Place]], float]:
    """The places of one narrow row (SPEC 5, the narrow block of the CSS):
    a run that reads as a row at one height, side by side; a picture alone
    as wide as the column and never taller than the cap (a #wide one: the
    wide cap), its figure at least caption_min wide when it has a caption."""
    W, g = metrics.width, metrics.gap
    indices = [cell[0] for cell in row]
    if mode == "row" and len(indices) > 1:
        sum_r = 0.0
        for index in indices:
            sum_r += photo_layout.ratio_of(items[index])
        H = min(metrics.cap, (W - (len(indices) - 1) * g) / sum_r)
        places = [[Place(photo_layout.ratio_of(items[i]) * H, H, 0.0,
                         photo_layout.ratio_of(items[i]) * H, H)] for i in indices]
        return places, sum(p[0].width for p in places) + (len(indices) - 1) * g
    index = indices[0]
    ratio = photo_layout.ratio_of(items[index])
    wide = items[index].get("wide") is True
    cap = metrics.wide_cap if wide else metrics.cap
    pw = min(W, cap * ratio)
    figure = W if wide else (max(pw, min(metrics.caption_min, W)) if items[index].get("caption")
                             else pw)
    return [[Place(figure, pw / ratio, (figure - pw) / 2, pw, pw / ratio)]], figure


# --------------------------------------------------------------------------- #
# The handler that sizes a group's pictures                                    #
# --------------------------------------------------------------------------- #

def _row_table_at(doc, position: int):
    """The row table of a group around ``position``, or None. (Asked while
    Qt lays the document out: a cursor placed here would ask for the
    layout of its line, and so for this object's size again.)"""
    frame = doc.frameAt(position)
    if isinstance(frame, QTextTable) and frame.format().boolProperty(STACK):
        frame = frame.parentFrame()
    return frame if is_group_table(frame) else None


def _row_cells(doc, table) -> Tuple[List[List[int]], List[Unit], Dict[int, Tuple[int, int]],
                               List[Tuple[int, str]]]:
    """A row table read for its geometry: the cells as unit indices, the
    units, each picture's position to (cell, member), and the spacers'
    positions and sides."""
    cells: List[List[int]] = []
    units: List[Unit] = []
    where: Dict[int, Tuple[int, int]] = {}
    spacers: List[Tuple[int, str]] = []
    columns = table.columns()
    for c in range(columns):
        cell = table.cellAt(0, c)
        members: List[int] = []
        for block in _blocks_in(doc, cell):
            fmt = block.blockFormat()
            if fmt.hasProperty(UNIT):
                for position, text, cf in _pieces(block):
                    if _is_picture(text, cf):
                        where[position] = (len(cells), len(members))
                        members.append(len(units))
                        units.append(Unit(QTextDocumentFragment(), cf.toImageFormat().name(),
                                          fmt, alt=_alt(cf)))
                        break
            elif fmt.boolProperty(CAPTION) and units and members:
                units[-1].caption_text = block.text()
            else:
                for position, text, cf in _pieces(block):
                    if cf.objectType() == SPACER_OBJECT:
                        spacers.append((position, str(cf.property(SPACER_SIDE))))
        if members:
            cells.append(members)
    return cells, units, where, spacers


def _whole(place: Place) -> Place:
    """``place`` a whole number of pixels wide (Qt rounds a column's width
    up), its picture keeping its shape."""
    width = float(int(place.width))
    if width == place.width or place.width <= 0:
        return place
    scale = width / place.width
    return Place(width, place.height * scale, place.x * scale, place.picture_width * scale,
                 place.picture_height * scale)


def _fitted(target: QRectF, doc, fmt) -> QRectF:
    """``target`` narrowed to its picture's own shape, centred in it. The
    engine lays a picture out at 3:2 when it does not take its size (a
    shape past its limits, photo_layout.picture_size): it is drawn whole
    inside that box, never stretched to it."""
    image = doc.resource(QTextDocument.ResourceType.ImageResource.value,
                         QUrl(fmt.toImageFormat().name()))
    if isinstance(image, QPixmap):
        image = image.toImage()
    if (not isinstance(image, QImage) or image.isNull() or is_placeholder(image)
            or target.width() <= 0 or target.height() <= 0):
        return target
    ratio = image.width() / image.height()
    room = target.width() / target.height()
    if abs(ratio - room) <= 0.01 * ratio:
        return target
    if ratio > room:
        height = target.width() / ratio
        return QRectF(target.x(), target.y() + (target.height() - height) / 2,
                      target.width(), height)
    width = target.height() * ratio
    return QRectF(target.x() + (target.width() - width) / 2, target.y(), width, target.height())


class _Handler(QPyTextObject):
    """Sizes and draws the pictures of a group (and its spacers); every
    other picture goes to Qt's own handler, as before."""

    def __init__(self, layout, original, sizes: Optional[SizeLookup]) -> None:
        super().__init__(layout)
        self._original = original
        self.sizes = sizes
        # The height of the screen (or page) the document is read on, in
        # the layout's pixels: no row is taller than its share of it.
        self.screen_height: Optional[float] = None
        self._cache: Dict[Tuple, Dict[int, Place]] = {}

    # Qt asks for each object's size whenever it lays its line out (also
    # after the width changed), and has it drawn into the box it gave.

    def intrinsicSize(self, doc, position, fmt):
        place = self._place(doc, position, fmt)
        if place is not None:
            return QSizeF(place.width, place.height)
        if fmt.objectType() == SPACER_OBJECT:
            return QSizeF(0, 0)
        if self._original is not None:
            return self._original.intrinsicSize(doc, position, fmt)
        return QSizeF(0, 0)

    def drawObject(self, painter, rect, doc, position, fmt):
        if fmt.objectType() == SPACER_OBJECT:
            return
        place = self._place(doc, position, fmt)
        if place is not None and self._original is not None:
            target = QRectF(rect.x() + place.x, rect.y() + (rect.height() - place.picture_height),
                            place.picture_width, place.picture_height)
            self._original.drawObject(painter, _fitted(target, doc, fmt), doc, position, fmt)
            return
        if self._original is not None:
            self._original.drawObject(painter, rect, doc, position, fmt)

    def metrics(self, doc, table) -> Metrics:
        device = doc.documentLayout().paintDevice()
        scale = device.logicalDpiY() / 96.0 if device is not None else 1.0
        width = doc.textWidth()
        if width <= 0:
            width = doc.pageSize().width()
        # The root frame's margins hold the document's margin.
        root = doc.rootFrame().frameFormat()
        room = width - (root.leftMargin() + root.rightMargin()
                        + 2 * (root.padding() + root.border())) * scale
        narrow = str(table.format().property(ARRANGEMENT) or MOSAIC) == NARROW
        constants = photo_layout.CONSTANTS

        def cap(px: float, share: float) -> float:
            if self.screen_height is None or self.screen_height <= 0:
                return px * scale
            return min(px * scale, share * self.screen_height)
        wide_cap = cap(constants["wideCap"]["px"], constants["wideCap"]["vh"])
        line = 0.0
        caption = _first_caption(doc, table)
        if caption is not None:
            font = caption.begin().fragment().charFormat().font().resolve(doc.defaultFont()) \
                if caption.length() > 1 else doc.defaultFont()
            font_metrics = QFontMetricsF(font, device) if device is not None else QFontMetricsF(font)
            line = caption.blockFormat().topMargin() * scale + font_metrics.lineSpacing()
        if narrow:
            phone = constants["phone"]
            return Metrics(max(1.0, room - _SLACK), phone["gap"] * scale,
                           cap(phone["capPx"], phone["capVh"]), wide_cap, line,
                           phone["captionMin"] * scale)
        regular = constants["regular"]
        return Metrics(max(1.0, room - _SLACK), regular["gap"] * scale,
                       cap(regular["cap"], REGULAR_CAP_VH), wide_cap, line,
                       constants["phone"]["captionMin"] * scale)

    def _place(self, doc, position: int, fmt) -> Optional[Place]:
        if fmt.objectType() != SPACER_OBJECT and not (
                fmt.isImageFormat() and fmt.toImageFormat().name()):
            return None
        table = _row_table_at(doc, position)
        if table is None:
            return None
        metrics = self.metrics(doc, table)
        key = (table.firstPosition(), doc.revision(), metrics)
        places = self._cache.get(key)
        if places is None:
            places = self._row_places(doc, table, metrics)
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[key] = places
        return places.get(position)

    def _row_places(self, doc, table, metrics: Metrics) -> Dict[int, Place]:
        cells, units, where, spacers = _row_cells(doc, table)
        if not cells:
            return {}
        items = [item(self.sizes(unit.name) if self.sizes else None, unit.caption_text,
                      unit.wide) for unit in units]
        local_cells = cells
        narrow = str(table.format().property(ARRANGEMENT) or MOSAIC) == NARROW
        if narrow:
            mode = "row" if len(cells) > 1 else "single"
            places, _width = narrow_places(local_cells, mode, items, metrics)
        else:
            places, _width = mosaic_places(local_cells, items, metrics)
        places = [[_whole(place) for place in cell] for cell in places]
        out: Dict[int, Place] = {}
        for position, (c, m) in where.items():
            out[position] = places[c][m]
        width = sum(cell[0].width for cell in places) + (len(places) - 1) * metrics.gap
        free = max(0.0, metrics.width - width)
        lead = max(1.0, int(free / 2))
        for position, side in spacers:
            out[position] = Place(lead if side == LEAD else max(1.0, free - lead), 0.0)
        return out

    def forget(self) -> None:
        """The pictures' sizes changed: measure again at the next layout."""
        self._cache.clear()


def _first_caption(doc, table):
    for block in _blocks_in(doc, table):
        if block.blockFormat().boolProperty(CAPTION):
            return block
    return None


# Set on a document once install gave its layout a handler: a document
# that never had one (read for an export, a check or a copy) is not given
# a layout by asking, which would lay out every change made to it after.
_INSTALLED = "photoGroupHandler"


def _handler(doc) -> Optional[_Handler]:
    if not doc.property(_INSTALLED):
        return None
    found = doc.documentLayout().handlerForObject(IMAGE_OBJECT)
    return found if isinstance(found, _Handler) else None


def install(doc: QTextDocument, sizes: Optional[SizeLookup] = None) -> None:
    """Have ``doc``'s layout size the pictures of its groups (its other
    pictures as before), with the sizes ``sizes`` gives (without it, every
    picture is taken for 3:2 until it is known). Again for a new layout."""
    layout = doc.documentLayout()
    found = _handler(doc)
    if found is not None:
        found.sizes = sizes if sizes is not None else found.sizes
        found.forget()
        return
    original = layout.handlerForObject(IMAGE_OBJECT)
    handler = _Handler(layout, original, sizes)
    layout.registerHandler(IMAGE_OBJECT, handler)
    layout.registerHandler(SPACER_OBJECT, handler)
    doc.setProperty(_INSTALLED, True)


def set_screen_height(doc: QTextDocument, height: Optional[float]) -> None:
    """The height ``doc`` is read on (a view's, a page's), in its layout's
    pixels: a row takes at most its share of it, as on a reader's screen.
    The groups are laid out again when it changed."""
    found = _handler(doc)
    if found is None or found.screen_height == height:
        return
    found.screen_height = height
    relayout(doc)


def sizes_of(doc: QTextDocument) -> Optional[SizeLookup]:
    """The lookup ``doc``'s pictures are sized with (install), if any."""
    found = _handler(doc)
    return found.sizes if found is not None else None


def relayout(doc: QTextDocument) -> None:
    """Sizes changed (a picture arrived): lay the groups out again, with
    nothing in the document changed."""
    found = _handler(doc)
    if found is not None:
        found.forget()
    if has_groups(doc):
        doc.markContentsDirty(0, doc.characterCount())


def picture_boxes(doc: QTextDocument) -> List[Tuple[int, str, QRectF]]:
    """Where each picture of the groups of ``doc`` is drawn, in the
    document's coordinates: (position, name, box), in the author's order.
    (For hit testing, and for the checks that a picture shows whole.)"""
    found = _handler(doc)
    if found is None:
        return []
    layout = doc.documentLayout()
    out = []
    for group in groups_in(doc):
        block = doc.findBlock(group.start)
        while block.isValid() and block.position() <= group.end:
            fmt = block.blockFormat()
            text_layout = block.layout()
            if fmt.hasProperty(UNIT) and text_layout is not None and text_layout.lineCount():
                for position, text, cf in _pieces(block):
                    if not _is_picture(text, cf):
                        continue
                    place = found._place(doc, position, cf)
                    if place is None:
                        break
                    line = text_layout.lineForTextPosition(position - block.position())
                    rect = layout.blockBoundingRect(block)
                    x = rect.left() + line.cursorToX(position - block.position())[0] + place.x
                    y = rect.top() + line.y() + line.ascent() - place.picture_height
                    out.append((position, cf.toImageFormat().name(),
                                QRectF(x, y, place.picture_width, place.picture_height)))
                    break
            block = block.next()
    return out


def picture_at(doc: QTextDocument, point) -> Optional[Tuple[int, str]]:
    """The group picture drawn at ``point`` (the document's coordinates):
    its position and name, or None."""
    for position, name, box in picture_boxes(doc):
        if box.contains(point):
            return position, name
    return None


def picture_words_at(doc: QTextDocument, position: int) -> Optional[str]:
    """What a screen reader says for the group picture at ``position`` (the
    caret before or after it): "Photo 2 of 5. Loch", as the exported page's
    viewer says it; None away from one. (The text holds a picture as one
    character, which a screen reader cannot read.)"""
    block = doc.findBlock(position)
    if not block.blockFormat().hasProperty(UNIT):
        return None
    picture = next(((p, cf) for p, text, cf in _pieces(block) if _is_picture(text, cf)), None)
    table = _row_table_at(doc, position)
    if picture is None or table is None or not picture[0] <= position <= picture[0] + 1:
        return None
    number = table.format().property(GROUP)
    tables = [frame for frame in doc.rootFrame().childFrames()
              if is_group_table(frame) and frame.format().property(GROUP) == number]
    units = [b.position() for t in tables for b in _blocks_in(doc, t)
             if b.blockFormat().hasProperty(UNIT)]
    if block.position() not in units:
        return None
    words = _("Photo {k} of {n}").format(k=units.index(block.position()) + 1, n=len(units))
    alt = _alt(picture[1])
    return f"{words}. {alt}" if alt else words


def _selected_group_picture(cursor: QTextCursor):
    """The format of the group picture ``cursor`` selects, alone, or None."""
    if not cursor.hasSelection() or cursor.selectionEnd() - cursor.selectionStart() != 1:
        return None
    doc = cursor.document()
    block = doc.findBlock(cursor.selectionStart())
    if not block.blockFormat().hasProperty(UNIT):
        return None
    for position, text, fmt in _pieces(block):
        if position <= cursor.selectionStart() < position + len(text) and _is_picture(text, fmt):
            return fmt
    return None


def group_picture_at_cursor(cursor: QTextCursor) -> Optional[str]:
    """The name of the group picture ``cursor`` selects, alone, or None."""
    fmt = _selected_group_picture(cursor)
    return fmt.toImageFormat().name() if fmt is not None else None


def group_picture_description_at_cursor(cursor: QTextCursor) -> str:
    """The description of the group picture ``cursor`` selects, or ""."""
    fmt = _selected_group_picture(cursor)
    return _alt(fmt) if fmt is not None else ""
