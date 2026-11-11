# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The editor's rich text: every structure it can make, as plain functions.

The document is a QTextDocument, and Markdown is how it leaves the editor
(markdown_writer.py). So every structure made here is one that writer
already writes, and one Qt's own Markdown reader reads back the same way:
a heading is a block with a heading level, a list is a QTextList, a quote
is a block with a quote level. What the editor shows, Markdown can say,
and what was written comes back unchanged when the file is opened again.

The functions take a QTextCursor (or the document) and change nothing but
the text they are given, each change as one step on the undo stack. They
hold no state and know no widget, so the editor, the menus, the toolbar,
the slash menu and the Markdown shortcuts all call the same code, and a
test can call it without a window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

from PySide6.QtGui import (
    QFont, QFontDatabase, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
    QTextDocumentFragment, QTextFormat, QTextImageFormat, QTextLength, QTextListFormat,
    QTextTable, QTextTableFormat,
)

from doc_walk import doc_length, doc_offset, iter_blocks, text_index

# Inline styles, by name. Underline has no Markdown; it stays in local
# files only (markdown_writer.has_local_only_formatting).
BOLD = "bold"
ITALIC = "italic"
UNDERLINE = "underline"
STRIKE = "strike"
CODE = "code"
INLINE = (BOLD, ITALIC, UNDERLINE, STRIKE, CODE)

# The font inline code is shown in, as Qt's Markdown reader writes it.
CODE_FAMILIES = ["monospace"]


def normalize_after_markdown_load(doc) -> None:
    """Tidy what Qt's Markdown reader leaves behind.

    After a checklist the reader puts a checklist mark on every block that
    follows it (paragraphs, quotes, code), outside any list. The mark is
    invisible there, but it is in the block's format, so starting a list
    on such a paragraph would make a checked item out of nothing. Every
    block outside a list loses it; list items keep theirs.

    Headings get the room above and below them that the editor gives
    the ones typed here, bulleted lists the shape of their depth (disc,
    circle, square), as lists made here have: Qt's reader gives every
    depth a disc, and tables the grid of the ones inserted here.

    A table right after a code block gets an empty line at the top of its
    first header cell, with the header's weight on it: the cell's text sat
    a line low and plain. The empty line goes and the text gets the weight.

    Call after every ``setMarkdown`` whose result the person edits.
    """
    marker = QTextFormat.Property.BlockMarker
    stray = [block for block in iter_blocks(doc)
             if block.textList() is None and block.blockFormat().hasProperty(marker)]
    headings = [block for block in iter_blocks(doc) if block.blockFormat().headingLevel()]
    bullets = {}
    for block in iter_blocks(doc):
        text_list = block.textList()
        if text_list is not None and list_kind_of(text_list) == BULLET:
            bullets.setdefault(text_list.objectIndex(), text_list)
    reshaped = [text_list for text_list in bullets.values()
                if text_list.format().style()
                != list_format(BULLET, max(1, text_list.format().indent())).style()]
    tables = [table for table in tables_of(doc)
              if table.format().cellPadding() != table_format().cellPadding()]
    if tables_of(doc):
        unescape_cell_code(doc)
    low = _low_header_cells(doc)
    if not stray and not headings and not reshaped and not tables and not low:
        return
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for table in tables:
        fmt = table.format()
        fmt.merge(table_format())
        table.setFormat(fmt)
    for block in stray:
        fmt = block.blockFormat()
        fmt.clearProperty(marker)
        QTextCursor(block).setBlockFormat(fmt)
    # Headings get the room the editor gives the ones typed here.
    for block in headings:
        fmt = block.blockFormat()
        QTextCursor(block).setBlockFormat(heading_block_format(fmt, fmt.headingLevel()))
    for text_list in reshaped:
        fmt = text_list.format()
        fmt.setStyle(list_format(BULLET, max(1, fmt.indent())).style())
        text_list.setFormat(fmt)
    for cell in low:
        empty = doc.findBlock(cell.firstPosition())
        weight = QTextCharFormat()
        weight.setFontWeight(empty.charFormat().fontWeight())
        QTextCursor(empty).deleteChar()
        text = cell.firstCursorPosition()
        text.setPosition(cell.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
        text.mergeCharFormat(weight)
    cursor.endEditBlock()


def _low_header_cells(doc) -> list:
    """Header cells whose text Qt's Markdown reader put under an empty
    line (normalize_after_markdown_load)."""
    cells = []
    for table in tables_of(doc):
        for column in range(table.columns()):
            cell = table.cellAt(0, column)
            first = doc.findBlock(cell.firstPosition())
            if (not first.text() and first.next().isValid()
                    and first.next().position() <= cell.lastPosition()):
                cells.append(cell)
    return cells


# --------------------------------------------------------------------------- #
# Inline styles                                                                #
# --------------------------------------------------------------------------- #

def has_style(fmt: QTextCharFormat, style: str) -> bool:
    """Whether a piece of text carries ``style``."""
    if style == BOLD:
        return fmt.fontWeight() >= QFont.Weight.DemiBold
    if style == ITALIC:
        return fmt.fontItalic()
    if style == UNDERLINE:
        return fmt.fontUnderline()
    if style == STRIKE:
        return fmt.fontStrikeOut()
    if style == CODE:
        return fmt.fontFixedPitch()
    raise ValueError(f"unknown style {style!r}")


def style_format(style: str, on: bool) -> QTextCharFormat:
    """The change that turns ``style`` on or off, to merge into text."""
    fmt = QTextCharFormat()
    if style == BOLD:
        fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
    elif style == ITALIC:
        fmt.setFontItalic(on)
    elif style == UNDERLINE:
        fmt.setFontUnderline(on)
    elif style == STRIKE:
        fmt.setFontStrikeOut(on)
    elif style == CODE:
        fmt.setFontFixedPitch(on)
        # Off, the families are cleared by clear_style below: merging can
        # set a property but never take one away.
        if on:
            fmt.setFontFamilies(CODE_FAMILIES)
    else:
        raise ValueError(f"unknown style {style!r}")
    return fmt


def _iter_text_runs(cursor: QTextCursor) -> Iterator[Tuple[int, int, QTextCharFormat]]:
    """``(start, end, format)`` of each piece of text in the selection
    (images left out), cut to the selection, one after the other."""
    doc = cursor.document()
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    block = doc.findBlock(start)
    while block.isValid() and block.position() < end:
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                first = fragment.position()
                last = first + fragment.length()
                fmt = fragment.charFormat()
                if last > start and first < end and not fmt.isImageFormat():
                    yield max(first, start), min(last, end), fmt
            it += 1
        block = block.next()


def _text_runs(cursor: QTextCursor) -> List[Tuple[int, int, QTextCharFormat]]:
    """Every piece of text in the selection (see _iter_text_runs)."""
    return list(_iter_text_runs(cursor))


def holds_link_or_code(cursor: QTextCursor) -> bool:
    """Whether the selection holds any part of a link, inline code or a
    picture: words a typed style must not run across."""
    if "\ufffc" in cursor.selectedText():
        return True
    return any(fmt.isAnchor() or fmt.fontFixedPitch()
               for _start, _end, fmt in _iter_text_runs(cursor))


def selection_has(cursor: QTextCursor, style: str) -> bool:
    """Whether all of the selected text carries ``style``: a selection that
    is only partly bold counts as not bold, so the command makes it all
    bold (the way Pages and Google Docs do it)."""
    found = False
    for _start, _end, fmt in _iter_text_runs(cursor):
        if not has_style(fmt, style):
            return False
        found = True
    return found


# What the menus and the toolbar show about a selection is read from at
# most this many pieces of text, or paragraphs: far more than any
# selection someone reads at once, and a bound on the time one update
# takes while a long selection is being extended. Past it, a style that
# could not be checked all the way counts as not applied throughout (its
# button shows off), and the command decides from the same reading, so a
# button that shows off always turns its style on (over all of it).
STATE_BUDGET = 1500


def selection_state(cursor: QTextCursor, styles=INLINE, *,
                    budget: int = STATE_BUDGET) -> dict:
    """``{style: bool}``: for each style, whether all of the selected text
    carries it, read in one pass that stops as soon as every answer is
    known, or after ``budget`` pieces of text."""
    still = set(styles)
    seen = 0
    for _start, _end, fmt in _iter_text_runs(cursor):
        seen += 1
        if seen > budget:
            still.clear()
            break
        for style in list(still):
            if not has_style(fmt, style):
                still.discard(style)
        if not still:
            break
    return {style: seen > 0 and style in still for style in styles}


def toggle_style(cursor: QTextCursor, style: str, *, budget: int = STATE_BUDGET) -> bool:
    """Turn ``style`` on over the whole selection, or off when all of it
    has it already, read as the toolbar reads it (selection_state). Returns
    the new state. One step on the undo stack."""
    on = not selection_state(cursor, (style,), budget=budget)[style]
    cursor.beginEditBlock()
    cursor.mergeCharFormat(style_format(style, on))
    if not on and style == CODE:
        _clear_properties(cursor, (QTextFormat.Property.FontFamilies,
                                   QTextFormat.Property.FontFamily))
    cursor.endEditBlock()
    return on


def _clear_properties(cursor: QTextCursor, properties, *, keep=None) -> None:
    """Take ``properties`` off every piece of the selection; ``keep(fmt,
    property)`` may spare one."""
    doc = cursor.document()
    for start, end, fmt in _text_runs(cursor):
        changed = QTextCharFormat(fmt)
        for prop in properties:
            if keep is None or not keep(fmt, prop):
                changed.clearProperty(prop)
        if changed != fmt:
            piece = QTextCursor(doc)
            piece.setPosition(start)
            piece.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            piece.setCharFormat(changed)


# What Clear Formatting takes away: every inline style and color. A link
# stays a link and keeps its own look; a heading keeps its weight.
_FORMATTING = (
    QTextFormat.Property.FontWeight, QTextFormat.Property.FontItalic,
    QTextFormat.Property.FontUnderline, QTextFormat.Property.TextUnderlineStyle,
    QTextFormat.Property.FontStrikeOut, QTextFormat.Property.FontFixedPitch,
    QTextFormat.Property.FontFamilies, QTextFormat.Property.FontFamily,
    QTextFormat.Property.ForegroundBrush, QTextFormat.Property.BackgroundBrush,
)
_LINK_LOOK = (QTextFormat.Property.ForegroundBrush, QTextFormat.Property.FontUnderline,
              QTextFormat.Property.TextUnderlineStyle)


def clear_formatting(cursor: QTextCursor) -> None:
    """Clear Formatting over the selection, as one step on the undo stack."""
    doc = cursor.document()
    headings = set()
    block = doc.findBlock(cursor.selectionStart())
    while block.isValid() and block.position() <= cursor.selectionEnd():
        if block.blockFormat().headingLevel():
            headings.add(block.position())
        block = block.next()

    def keep(fmt, prop):
        if fmt.isAnchor() and prop in _LINK_LOOK:
            return True
        return False

    cursor.beginEditBlock()
    _clear_properties(cursor, _FORMATTING, keep=keep)
    # A heading is bold by its own format: that is not emphasis to clear.
    for position in headings:
        heading = QTextCursor(doc.findBlock(position))
        heading.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                             QTextCursor.MoveMode.KeepAnchor)
        within = QTextCursor(doc)
        within.setPosition(max(heading.selectionStart(), cursor.selectionStart()))
        within.setPosition(min(heading.selectionEnd(), cursor.selectionEnd()),
                           QTextCursor.MoveMode.KeepAnchor)
        within.mergeCharFormat(style_format(BOLD, True))
    cursor.endEditBlock()


def typing_format_without(fmt: QTextCharFormat) -> QTextCharFormat:
    """The format to type on with after Clear Formatting with nothing
    selected: the one at the caret, with every style and color taken off
    (a link stays a link)."""
    clean = QTextCharFormat(fmt)
    for prop in _FORMATTING:
        if not (fmt.isAnchor() and prop in _LINK_LOOK):
            clean.clearProperty(prop)
    return clean


# --------------------------------------------------------------------------- #
# Paragraph styles: Body and Heading 1 to 3                                    #
# --------------------------------------------------------------------------- #

BODY = 0
# How much larger a heading's text is, the way Qt's Markdown reader sizes
# it (QTextCharFormat.FontSizeAdjustment): a document typed here and one
# opened from a .md file look the same.
HEADING_SIZE = {1: 3, 2: 2, 3: 1, 4: 0, 5: -1, 6: -1}

# The paragraph styles the menus offer, smallest first: what Format >
# Bigger and Smaller step through (step_style).
STYLES_BY_SIZE = (0, 3, 2, 1)


def step_style(level: int, *, larger: bool) -> Optional[int]:
    """The style one size larger (or smaller) than heading ``level`` (0 for
    body text), among the styles the menus offer; None when there is none.
    A heading they do not offer (4 to 6, from a file) is between Body and
    Heading 3: one larger is Heading 3, one smaller is Body."""
    if level not in STYLES_BY_SIZE:
        return STYLES_BY_SIZE[1] if larger else STYLES_BY_SIZE[0]
    index = STYLES_BY_SIZE.index(level) + (1 if larger else -1)
    return STYLES_BY_SIZE[index] if 0 <= index < len(STYLES_BY_SIZE) else None


def _iter_blocks_of(cursor: QTextCursor):
    """The blocks the cursor's selection touches (the caret's block
    without one), in order, one after the other. None for a null cursor
    (an editor's cursor is one while its whole document is replaced)."""
    if cursor.isNull():
        return
    doc = cursor.document()
    block = doc.findBlock(cursor.selectionStart())
    last = doc.findBlock(cursor.selectionEnd())
    while block.isValid():
        yield block
        if block == last:
            break
        block = block.next()


def _blocks_of(cursor: QTextCursor):
    """The blocks the cursor's selection touches (see _iter_blocks_of)."""
    return list(_iter_blocks_of(cursor))


def heading_level(cursor: QTextCursor) -> int:
    """The paragraph style under the cursor: 0 for Body, 1 to 6 for a
    heading, -1 when the selection spans different ones."""
    level = None
    for block in _iter_blocks_of(cursor):
        this = block.blockFormat().headingLevel()
        if level is not None and this != level:
            return -1
        level = this
    return -1 if level is None else level


def heading_char_format(level: int) -> QTextCharFormat:
    """The text format of a heading of ``level`` (merged into its text)."""
    fmt = QTextCharFormat()
    fmt.setProperty(QTextFormat.Property.FontSizeAdjustment, HEADING_SIZE.get(level, 0))
    fmt.setFontWeight(QFont.Weight.Bold)
    return fmt


# The weight words had of their own before their paragraph became a
# heading, which sets every word in its bold: given back when the
# paragraph is Body again, so bold words stay bold (0: none of their own).
OWN_WEIGHT = QTextFormat.Property.UserProperty + 0x3D1


def body_char_format(fmt: QTextCharFormat, *, own_weight: bool = True) -> QTextCharFormat:
    """``fmt`` as Body text: no heading size, no heading weight, and the
    weight the words had of their own (unless ``own_weight`` is False:
    what is typed next starts in the plain weight)."""
    body = QTextCharFormat(fmt)
    body.clearProperty(QTextFormat.Property.FontSizeAdjustment)
    own = body.property(OWN_WEIGHT) if own_weight else None
    if isinstance(own, int) and not isinstance(own, bool) and own > 0:
        body.setFontWeight(own)
    else:
        body.clearProperty(QTextFormat.Property.FontWeight)
    body.clearProperty(OWN_WEIGHT)
    return body


# Room above and below a heading. Margins are how the editor shows it;
# they are not Markdown, so nothing of them is written.
HEADING_MARGINS = (12, 4)


def heading_block_format(fmt, level: int):
    fmt.setHeadingLevel(level)
    top, bottom = HEADING_MARGINS if level else (0, 0)
    fmt.setTopMargin(top)
    fmt.setBottomMargin(bottom)
    return fmt


def set_heading(cursor: QTextCursor, level: int) -> None:
    """Make the paragraphs under the cursor Body (0) or a heading of
    ``level``, as one step on the undo stack. Choosing the style a
    paragraph already has turns it back into Body, the way the toolbar's
    heading buttons work in standup and Google Docs.

    A heading is a paragraph of its own: a list item or a quoted line
    made a heading leaves its list or quote (Markdown readers drop the
    quote of a quoted heading, and a heading inside a list is no heading
    to them)."""
    if level and heading_level(cursor) == level:
        level = BODY
    doc = cursor.document()
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    for block in _blocks_of(cursor):
        if is_code_block(block):
            # A paragraph style chosen on a line of code: it is text again.
            leave_code(block)
        if level:
            if block.textList() is not None:
                leave_list(block)
            if quote_depth(block):
                set_quote_depth(block, 0)
        fmt = heading_block_format(block.blockFormat(), level)
        whole = QTextCursor(block)
        whole.setBlockFormat(fmt)
        whole.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                           QTextCursor.MoveMode.KeepAnchor)
        for start, end, char in (_text_runs(whole) if whole.hasSelection() else []):
            piece = QTextCursor(doc)
            piece.setPosition(start)
            piece.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            piece.setCharFormat(restyled(char, level))
        # What is typed into the paragraph next (an empty one, or at its end).
        whole.setBlockCharFormat(restyled(block.charFormat(), level))
    edit.endEditBlock()


def restyled(fmt: QTextCharFormat, level: int) -> QTextCharFormat:
    """``fmt`` in the paragraph style ``level`` (0 for Body). Words that
    become heading words keep note of their own weight (OWN_WEIGHT)."""
    if not level:
        return body_char_format(fmt)
    heading = QTextCharFormat(fmt)
    if not heading.hasProperty(OWN_WEIGHT):
        own = fmt.fontWeight() if fmt.hasProperty(QTextFormat.Property.FontWeight) else 0
        heading.setProperty(OWN_WEIGHT, int(own))
    heading.clearProperty(QTextFormat.Property.FontSizeAdjustment)
    heading.clearProperty(QTextFormat.Property.FontWeight)
    heading.merge(heading_char_format(level))
    return heading


# --------------------------------------------------------------------------- #
# Lists                                                                        #
# --------------------------------------------------------------------------- #

BULLET = "bullet"
NUMBER = "number"
# Deepest nesting a command makes. Markdown has no limit; readers have.
MAX_LIST_DEPTH = 6

_BULLET_STYLES = (QTextListFormat.Style.ListDisc, QTextListFormat.Style.ListCircle,
                  QTextListFormat.Style.ListSquare)
_ORDERED = {QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha,
            QTextListFormat.Style.ListUpperAlpha, QTextListFormat.Style.ListLowerRoman,
            QTextListFormat.Style.ListUpperRoman}


def list_kind_of(text_list) -> str:
    """BULLET or NUMBER for a QTextList (Markdown knows only these two)."""
    return NUMBER if text_list.format().style() in _ORDERED else BULLET


def list_format(kind: str, depth: int) -> QTextListFormat:
    """A list of ``kind`` nested ``depth`` deep. Bullets change shape with
    depth (disc, circle, square), as in Pages and every browser."""
    fmt = QTextListFormat()
    if kind == NUMBER:
        fmt.setStyle(QTextListFormat.Style.ListDecimal)
    else:
        fmt.setStyle(_BULLET_STYLES[(depth - 1) % len(_BULLET_STYLES)])
    fmt.setIndent(depth)
    return fmt


def list_kind(cursor: QTextCursor) -> str:
    """The list under the cursor: BULLET, NUMBER, "" for none, "mixed"
    when the selection holds more than one kind (or list and not)."""
    kinds = {list_kind_of(b.textList()) if b.textList() is not None else ""
             for b in _blocks_of(cursor)}
    if not kinds:
        return ""
    return kinds.pop() if len(kinds) == 1 else "mixed"


def leave_list(block) -> None:
    """Take a paragraph out of its list; it stays where it is, as Body text
    at the left margin (Qt would keep the list's indent as its own)."""
    text_list = block.textList()
    if text_list is not None:
        text_list.remove(block)
    fmt = block.blockFormat()
    fmt.setIndent(0)
    fmt.clearProperty(QTextFormat.Property.BlockMarker)
    QTextCursor(block).setBlockFormat(fmt)


def _list_to_join(block, depth: int, kind: str):
    """The list a paragraph moved to ``depth`` belongs to: the one an item
    before it already has at that depth (only deeper items between)."""
    previous = block.previous()
    while previous.isValid() and previous.textList() is not None \
            and previous.textList().format().indent() > depth:
        previous = previous.previous()
    if (previous.isValid() and previous.textList() is not None
            and previous.textList().format().indent() == depth
            and list_kind_of(previous.textList()) == kind):
        return previous.textList()
    return None


def _move_to_depth(block, depth: int, kind: str) -> None:
    target = _list_to_join(block, depth, kind)
    if target is not None:
        if block.textList() is not target:
            target.add(block)
    else:
        QTextCursor(block).createList(list_format(kind, depth))
    _no_own_indent(block)


def _no_own_indent(block) -> None:
    fmt = block.blockFormat()
    if fmt.indent():
        fmt.setIndent(0)
        QTextCursor(block).setBlockFormat(fmt)


def _item_run(block):
    """The first and last block of the run of list items ``block`` is in."""
    first = last = block
    while first.previous().isValid() and first.previous().textList() is not None:
        first = first.previous()
    while last.next().isValid() and last.next().textList() is not None:
        last = last.next()
    return first, last


def _tidy_lists(doc, around) -> None:
    """Items next to each other at the same depth and of the same kind are
    one list, as Markdown reads them: a list a command made right after
    another, or the children left behind when an item moved, join it.
    Only the runs of items ``around`` touches are looked at."""
    runs = []
    for block in around:
        if block.isValid() and block.textList() is not None:
            run = _item_run(block)
            if run not in runs:
                runs.append(run)
    for first, last in runs:
        open_lists = {}
        block = first
        while block.isValid():
            text_list = block.textList()
            depth = text_list.format().indent()
            for deeper in [d for d in open_lists if d > depth]:
                del open_lists[deeper]
            current = open_lists.get(depth)
            if (current is not None and current is not text_list
                    and list_kind_of(current) == list_kind_of(text_list)):
                current.add(block)
                text_list = current
            open_lists[depth] = text_list
            if block == last:
                break
            block = block.next()


def toggle_list(cursor: QTextCursor, kind: str) -> None:
    """Bulleted List or Numbered List over the paragraphs under the
    cursor, as one step on the undo stack:

    - all of them checklist items, and Bulleted List: their boxes go, and
      they are bulleted items (a checklist is not shown as a bulleted
      list, so the command makes it one);
    - all of them in lists of this kind: they leave the list;
    - all of them in lists, some of another kind: those lists change
      kind in place, nesting kept;
    - otherwise they become one list of this kind (a heading becomes
      Body first: a list item is not a heading)."""
    # Code is code: its lines are no list items.
    blocks = [b for b in _blocks_of(cursor) if not is_code_block(b)]
    if not blocks:
        return
    doc = cursor.document()
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    current = list_kind(cursor)
    all_items = all(b.textList() is not None for b in blocks)
    if kind == BULLET and all(is_task(b) for b in blocks):
        for block in blocks:
            set_tick(block, QTextBlockFormat.MarkerType.NoMarker)
    elif current == kind:
        for block in blocks:
            leave_list(block)
    elif all_items:
        changed = []
        for block in blocks:
            text_list = block.textList()
            if any(text_list is seen for seen in changed):
                continue
            changed.append(text_list)
            text_list.setFormat(list_format(kind, text_list.format().indent()))
    else:
        for block in blocks:
            if block.blockFormat().headingLevel():
                set_heading(QTextCursor(block), BODY)
            if block.textList() is not None:
                leave_list(block)
        new_list = QTextCursor(blocks[0]).createList(list_format(kind, 1))
        for block in blocks:
            if block.textList() is not new_list:
                new_list.add(block)
            _no_own_indent(block)
    _tidy_lists(doc, [blocks[0].previous(), *blocks, blocks[-1].next()])
    edit.endEditBlock()


# --------------------------------------------------------------------------- #
# Checklists                                                                   #
# --------------------------------------------------------------------------- #

UNTICKED = QTextBlockFormat.MarkerType.Unchecked
TICKED = QTextBlockFormat.MarkerType.Checked


def is_task(block) -> bool:
    """Whether a paragraph is a checklist item: a list item with a box,
    ticked or not (Markdown's "- [ ]" and "- [x]")."""
    return block.textList() is not None and block.blockFormat().marker() in (UNTICKED, TICKED)


def set_tick(block, marker) -> None:
    """Give a list item a box (UNTICKED or TICKED), or none
    (QTextBlockFormat.MarkerType.NoMarker)."""
    fmt = block.blockFormat()
    fmt.setMarker(marker)
    QTextCursor(block).setBlockFormat(fmt)


def task_blocks(cursor: QTextCursor) -> list:
    """The checklist items under the cursor."""
    return [b for b in _blocks_of(cursor) if is_task(b)]


def toggle_ticks(cursor: QTextCursor) -> Optional[bool]:
    """Mark as Checked: tick the checklist items under the cursor, or, all
    ticked already, untick them. One undo step; the new state, or None
    when no checklist item is there."""
    tasks = task_blocks(cursor)
    if not tasks:
        return None
    ticked = not all(b.blockFormat().marker() == TICKED for b in tasks)
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    for block in tasks:
        set_tick(block, TICKED if ticked else UNTICKED)
    edit.endEditBlock()
    return ticked


def toggle_checklist(cursor: QTextCursor) -> None:
    """Checklist over the paragraphs under the cursor, as one step on the
    undo stack: when all of them are checklist items, they become plain
    list items again; otherwise each becomes one, unticked unless it was
    ticked, and a paragraph outside a list joins a bulleted list first (a
    heading becomes Body: a list item is not a heading). Code, dividers
    and table cells are left as they are."""
    blocks = [b for b in _blocks_of(cursor) if not is_code_block(b) and not is_divider(b)
              and QTextCursor(b).currentTable() is None]
    if not blocks:
        return
    doc = cursor.document()
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    if all(is_task(b) for b in blocks):
        for block in blocks:
            set_tick(block, QTextBlockFormat.MarkerType.NoMarker)
    else:
        for block in blocks:
            if block.textList() is None:
                if block.blockFormat().headingLevel():
                    set_heading(QTextCursor(block), BODY)
                _move_to_depth(block, 1, BULLET)
            if not is_task(block):
                set_tick(block, UNTICKED)
        _tidy_lists(doc, [blocks[0].previous(), *blocks, blocks[-1].next()])
    edit.endEditBlock()


def change_indent(cursor: QTextCursor, delta: int) -> bool:
    """Increase (+1) or decrease (-1) the nesting of the list items under
    the cursor, as one step on the undo stack. An item at the top level
    that is decreased leaves the list. False when there is no list item
    under the cursor (nothing done)."""
    items = [b for b in _blocks_of(cursor) if b.textList() is not None]
    if not items:
        return False
    doc = cursor.document()
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    for block in items:
        text_list = block.textList()
        kind = list_kind_of(text_list)
        depth = text_list.format().indent() + delta
        if depth < 1:
            leave_list(block)
        else:
            _move_to_depth(block, min(depth, MAX_LIST_DEPTH), kind)
    _tidy_lists(doc, [items[0].previous(), *items, items[-1].next()])
    edit.endEditBlock()
    return True


# --------------------------------------------------------------------------- #
# Quotes and dividers                                                          #
# --------------------------------------------------------------------------- #

# How far one level of quote moves a paragraph in, as Qt's Markdown reader
# places it: a quote typed here and one read from a .md file look alike.
QUOTE_INDENT = 40


def quote_depth(block) -> int:
    level = block.blockFormat().property(QTextFormat.Property.BlockQuoteLevel)
    return level if isinstance(level, int) and level > 0 else 0


def set_quote_depth(block, depth: int) -> None:
    fmt = block.blockFormat()
    if depth > 0:
        fmt.setProperty(QTextFormat.Property.BlockQuoteLevel, depth)
        fmt.setLeftMargin(QUOTE_INDENT * depth)
    else:
        fmt.clearProperty(QTextFormat.Property.BlockQuoteLevel)
        fmt.setLeftMargin(0)
    QTextCursor(block).setBlockFormat(fmt)


def in_quote(cursor: QTextCursor) -> bool:
    """Whether every paragraph under the cursor is quoted."""
    found = False
    for block in _iter_blocks_of(cursor):
        if not quote_depth(block):
            return False
        found = True
    return found


@dataclass(frozen=True)
class ParagraphState:
    """What the menus and the toolbar show about the paragraphs under the
    cursor: their style (-1 for mixed), their list (BULLET, NUMBER, "" or
    "mixed") and whether all of them are quoted."""

    heading: int = -1
    list_kind: str = ""
    quoted: bool = False
    tasks: bool = False         # all of them checklist items
    code: Optional[str] = None  # all code: their language ("" none, "mixed"); else None


def paragraph_state(cursor: QTextCursor, *, budget: int = STATE_BUDGET) -> ParagraphState:
    """The paragraphs' state, read in one pass that stops once every
    answer is known, or after ``budget`` paragraphs (STATE_BUDGET: past it,
    the state reads as mixed)."""
    level = None
    kind = None
    quoted = True
    tasks = True
    code = ...
    seen = 0
    for block in _iter_blocks_of(cursor):
        seen += 1
        if seen > budget:
            return ParagraphState(-1, "mixed", False, False, None)
        this_level = block.blockFormat().headingLevel()
        this_kind = list_kind_of(block.textList()) if block.textList() is not None else ""
        level = this_level if level is None or level == this_level else -1
        kind = this_kind if kind is None or kind == this_kind else "mixed"
        quoted = quoted and bool(quote_depth(block))
        tasks = tasks and is_task(block)
        language = code_language(block)
        if language is None:
            code = None
        elif code is ...:
            code = language
        elif code is not None and code != language:
            code = "mixed"
        if level == -1 and kind == "mixed" and not quoted and not tasks and code is None:
            break
    if not seen:
        return ParagraphState()
    return ParagraphState(level, kind, quoted, tasks, None if code is ... else code)


def toggle_quote(cursor: QTextCursor) -> None:
    """Quote the paragraphs under the cursor, or, when all of them are
    quoted already, take the quote away. One step on the undo stack. A
    heading quoted becomes Body first: Markdown readers drop the quote of
    a quoted heading."""
    blocks = _blocks_of(cursor)
    if not blocks:
        return
    unquote = in_quote(cursor)
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    for block in blocks:
        if unquote:
            set_quote_depth(block, 0)
        elif not quote_depth(block) and not is_divider(block):
            if block.blockFormat().headingLevel():
                set_heading(QTextCursor(block), BODY)
            set_quote_depth(block, 1)
    edit.endEditBlock()


def is_divider(block) -> bool:
    return block.blockFormat().hasProperty(
        QTextFormat.Property.BlockTrailingHorizontalRulerWidth)


def _divider_format() -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    # What Qt's Markdown reader gives "---": a rule across the page.
    fmt.setProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth, 1)
    return fmt


def insert_divider(cursor: QTextCursor) -> None:
    """A divider (a horizontal rule, ``---``) after the paragraph at the
    cursor, or in its place when it is empty, and an empty paragraph after
    it where the cursor goes. One step on the undo stack."""
    cursor.beginEditBlock()
    block = cursor.block()
    if block.text() or is_divider(block):
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock(_divider_format(), QTextCharFormat())
    else:
        cursor.setBlockFormat(_divider_format())
        cursor.setBlockCharFormat(QTextCharFormat())
    after = cursor.block().next()
    if after.isValid() and not after.text() and not is_divider(after) \
            and after.textList() is None:
        cursor.setPosition(after.position())
    else:
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
    cursor.endEditBlock()


def insert_own_paragraph(cursor: QTextCursor, text: str = "",
                         image: Optional[QTextImageFormat] = None) -> None:
    """``text``, or the object ``image`` (audio or video), as a Body
    paragraph of its own (readers show a note, an article or a player as a
    card only when it stands alone): the paragraph at the cursor when it is
    empty, else a new one after it; after a table or a block of code when
    the cursor is in one. An empty paragraph follows, where the cursor
    goes. One step on the undo stack."""
    cursor.beginEditBlock()
    if cursor.currentTable() is not None:
        leave_table(cursor)
    elif is_code_block(cursor.block()):
        last = cursor.block()
        while last.next().isValid() and is_code_block(last.next()):
            last = last.next()
        cursor.setPosition(last.position() + last.length() - 1)
    block = cursor.block()
    if block.text() or _has_own_style(block):
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
    if image is not None:
        cursor.insertImage(image)
    else:
        cursor.insertText(text, QTextCharFormat())
    after = cursor.block().next()
    if after.isValid() and not after.text() and not _has_own_style(after) \
            and QTextCursor(after).currentTable() is None:
        cursor.setPosition(after.position())
    else:
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
    cursor.endEditBlock()


def remove_divider(block) -> None:
    """The divider becomes an empty paragraph again."""
    QTextCursor(block).setBlockFormat(QTextBlockFormat())


# --------------------------------------------------------------------------- #
# Tables                                                                       #
# --------------------------------------------------------------------------- #
# A table is Markdown's: one header row (its words bold, by the header's
# own format, which the writer leaves out), cells of one line each (a
# Return in a cell is a line separator, written as a space), no merged
# cells and no alignment.

# --------------------------------------------------------------------------- #
# Mentions                                                                     #
# --------------------------------------------------------------------------- #
# A mention is a link to a person's Nostr address (``nostr:npub1…`` or
# ``nostr:nprofile1…``) whose words are "@" and their name. Markdown writes
# it as the bare address, which every Nostr reader turns into the name
# (NIP-27); read back, link_references gives it its name again.

MENTION_HREF = re.compile(r"nostr:(?:npub|nprofile)1[02-9ac-hj-np-z]+")
# A bare address in written text: on its own, not the end of a word or a
# part of a web address.
_REFERENCE_TEXT = re.compile(r"(?<![\w/=?#&:])nostr:((?:npub|nprofile)1[02-9ac-hj-np-z]+)")
_MENTION_NAME_MAX = 64
# The two invisible characters a name may keep: they join emoji and letters.
_JOINERS = "\u200c\u200d"


def is_mention(href: str) -> bool:
    return bool(MENTION_HREF.fullmatch(href or ""))


def mention_format(href: str) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setAnchor(True)
    fmt.setAnchorHref(href)
    return fmt


def mention_words(name: str) -> str:
    """What a mention of ``name`` shows: "@" and the name on one line (a
    line break or a run of spaces is one space), at most 64 characters of
    it, without the invisible characters that could turn the text around
    it or hide in it (a profile's name is anyone's text)."""
    kept = "".join(" " if char.isspace() else char for char in name
                   if char.isspace() or char.isprintable() or char in _JOINERS)
    words = " ".join(kept.split()).lstrip("@")
    if len(words) > _MENTION_NAME_MAX:
        words = words[:_MENTION_NAME_MAX - 1].rstrip() + "\u2026"
    return "@" + words


def insert_mention(cursor: QTextCursor, href: str, name: str) -> None:
    """The mention of ``name`` (its link ``href``) at the cursor, in place
    of the selection, then a space unless one follows. One undo step; the
    cursor ends after it, outside the link."""
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    if cursor.hasSelection():
        cursor.removeSelectedText()
    plain = without_link(cursor.charFormat())
    cursor.insertText(mention_words(name), mention_format(href))
    # (The end of a paragraph reads as a separator, not as a space.)
    following = cursor.document().characterAt(cursor.position())
    if following not in (" ", "\t", "\u00a0"):
        cursor.insertText(" ", plain)
    else:
        cursor.movePosition(QTextCursor.MoveOperation.NextCharacter)
    edit.endEditBlock()


def mention_at(cursor: QTextCursor) -> Optional[Tuple[int, int]]:
    """(start, end) of the mention the caret is in or touches from either
    side, or None (also with a selection)."""
    if cursor.hasSelection():
        return None
    found = link_range(cursor)
    if found is None or not is_mention(found[2]):
        return None
    return found[0], found[1]


def plain_text_of(fragment: QTextDocumentFragment, *, as_written: bool = False) -> str:
    """The plain text of ``fragment`` as another app should get it: each
    mention as its ``nostr:`` address, which every Nostr app reads as the
    person ("@Name" alone names nobody there). ``as_written`` keeps its
    no-break spaces (Qt's plain text makes them spaces), as a note that
    goes out does."""
    doc = QTextDocument()
    QTextCursor(doc).insertFragment(fragment)
    mentions = []
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            piece = it.fragment()
            if piece.isValid() and is_mention(piece.charFormat().anchorHref()):
                inside = QTextCursor(doc)
                inside.setPosition(piece.position() + 1)
                found = link_range(inside)
                if found is not None and found not in mentions:
                    mentions.append(found)
            it += 1
    for start, end, href in reversed(mentions):
        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(href)
    if as_written:
        return doc.toRawText().replace("\u2029", "\n").replace("\u2028", "\n")
    return doc.toPlainText()


def link_references(doc, label_for) -> int:
    """Each bare mention in ``doc`` (``nostr:npub1…`` written as text, as
    Markdown keeps a mention) made a mention again: a link whose words are
    "@" and ``label_for(entity)``. Not in code, not in a link already, not
    touching a word before it, and not where ``label_for`` gives None (the
    address names no person: it does not decode). Returns how many."""
    found = []
    for block in iter_blocks(doc):
        if is_code_block(block):
            continue
        text = block.text()
        for match in _REFERENCE_TEXT.finditer(text):
            # (A place in the text is converted to one in the document: an
            # emoji before it takes two positions there.)
            start = block.position() + doc_offset(text, match.start())
            probe = QTextCursor(block)
            probe.setPosition(start + 1)
            fmt = probe.charFormat()
            if fmt.isAnchor() or has_style(fmt, CODE):
                continue
            label = label_for(match.group(1))
            if label is None:
                continue
            found.append((start, doc_length(match.group(0)), match.group(0), label))
    if not found:
        return 0
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    for position, length, href, label in reversed(found):
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat(cursor.charFormat())
        fmt.merge(mention_format(href))
        cursor.insertText(mention_words(label), fmt)
    edit.endEditBlock()
    return len(found)


# --------------------------------------------------------------------------- #
# Footnotes                                                                    #
# --------------------------------------------------------------------------- #
# Footnotes are kept as the text Markdown writes for them: a mark ("[^1]")
# where the note belongs, and a paragraph that defines it ("[^1]: The
# source.") at the end. Qt has no footnotes, and text survives every way
# a document travels (files, drafts, backups, the clipboard); the
# highlighter gives them their look, and the preview draws them the way
# the reading apps do.

# A mark, and a paragraph that is a definition, as Markdown readers find them.
FOOTNOTE_MARK = re.compile(r"\[\^([^\]\s]+)\]")
FOOTNOTE_DEFINITION = re.compile(r"\s{0,3}\[\^([^\]\s]+)\]:")


def _prose_blocks(doc) -> Iterator:
    """The paragraphs of ``doc`` that are text, not code."""
    for block in iter_blocks(doc):
        if not is_code_block(block):
            yield block


def _in_code_words(block, index: int) -> bool:
    """Whether the character at ``index`` of ``block``'s text is code."""
    cursor = QTextCursor(block)
    cursor.setPosition(block.position() + doc_offset(block.text(), index) + 1)
    return has_style(cursor.charFormat(), CODE)


def footnote_definition(doc, label: str):
    """The paragraph that defines footnote ``label``, or None."""
    for block in _prose_blocks(doc):
        found = FOOTNOTE_DEFINITION.match(block.text())
        if found is not None and found.group(1) == label:
            return block
    return None


def footnote_marks(doc, label: Optional[str] = None) -> List[Tuple[int, str]]:
    """(position, label) of every footnote mark in ``doc`` (only those of
    ``label`` when given), in order: not the label of a definition, not
    in code."""
    marks = []
    for block in _prose_blocks(doc):
        text = block.text()
        definition = FOOTNOTE_DEFINITION.match(text)
        start = definition.end() if definition is not None else 0
        for found in FOOTNOTE_MARK.finditer(text, start):
            if (label is None or found.group(1) == label) \
                    and not _in_code_words(block, found.start()):
                marks.append((block.position() + doc_offset(text, found.start()),
                              found.group(1)))
    return marks


def next_footnote_label(doc) -> str:
    """The label of a new footnote: one more than the highest number used."""
    numbers = [int(label) for _position, label in footnote_marks(doc) if label.isdigit()]
    for block in _prose_blocks(doc):
        found = FOOTNOTE_DEFINITION.match(block.text())
        if found is not None and found.group(1).isdigit():
            numbers.append(int(found.group(1)))
    return str(max(numbers, default=0) + 1)


def insert_footnote(cursor: QTextCursor) -> bool:
    """A footnote: its mark ("[^2]", the next number) at the cursor, and a
    paragraph that defines it at the end of the document (after an empty
    one, unless the last paragraph defines a footnote already), where the
    cursor goes to write the note. One step on the undo stack. False
    where a mark would be code or cannot stand (a code block)."""
    if is_code_block(cursor.block()) or is_divider(cursor.block()):
        return False
    doc = cursor.document()
    label = next_footnote_label(doc)
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    if cursor.hasSelection():
        cursor.setPosition(cursor.selectionEnd())
    words = without_link(cursor.charFormat())
    words.clearProperty(QTextFormat.Property.FontFixedPitch)
    words.clearProperty(QTextFormat.Property.FontFamilies)
    words.clearProperty(QTextFormat.Property.FontFamily)
    cursor.insertText(f"[^{label}]", words)
    end = QTextCursor(doc)
    end.movePosition(QTextCursor.MoveOperation.End)
    last = end.block()
    if QTextCursor(last).currentTable() is not None:
        end.insertBlock(QTextBlockFormat(), QTextCharFormat())
    elif last.text() and not FOOTNOTE_DEFINITION.match(last.text()):
        end.insertBlock(QTextBlockFormat(), QTextCharFormat())
        end.insertBlock(QTextBlockFormat(), QTextCharFormat())
    elif last.text():
        end.insertBlock(QTextBlockFormat(), QTextCharFormat())
    else:
        end.setBlockFormat(QTextBlockFormat())
    end.insertText(f"[^{label}]: ", QTextCharFormat())
    edit.endEditBlock()
    cursor.setPosition(end.position())
    return True


def footnote_jump(cursor: QTextCursor) -> Optional[int]:
    """Where Go to Note (or a Command-click) leads: from a mark to the
    words of its note, from anywhere in a note (but a mark in it) back to
    the first mark of that note. None when the cursor is on neither."""
    block = cursor.block()
    if is_code_block(block):
        return None
    doc = cursor.document()
    text = block.text()
    offset = text_index(text, cursor.positionInBlock())
    definition = FOOTNOTE_DEFINITION.match(text)
    for found in FOOTNOTE_MARK.finditer(text, definition.end() if definition else 0):
        if found.start() <= offset <= found.end() \
                and not _in_code_words(block, found.start()):
            target = footnote_definition(doc, found.group(1))
            if target is None:
                return None
            note = target.text()
            words = FOOTNOTE_DEFINITION.match(note)
            rest = note[words.end():]
            return target.position() + doc_offset(
                note, words.end() + len(rest) - len(rest.lstrip(" ")))
    if definition is not None:
        marks = footnote_marks(doc, definition.group(1))
        return marks[0][0] if marks else None
    return None


def orphan_notes(doc) -> List[str]:
    """The labels of notes no mark in ``doc`` refers to."""
    marked = {label for _position, label in footnote_marks(doc)}
    orphans = []
    for block in _prose_blocks(doc):
        found = FOOTNOTE_DEFINITION.match(block.text())
        if found is not None and found.group(1) not in marked \
                and found.group(1) not in orphans:
            orphans.append(found.group(1))
    return orphans


def footnote_text(doc, label: str) -> str:
    """The words of footnote ``label``'s definition ("" for none)."""
    block = footnote_definition(doc, label)
    if block is None:
        return ""
    return block.text()[FOOTNOTE_DEFINITION.match(block.text()).end():].strip()


def table_format() -> QTextTableFormat:
    """How a table looks in the editor: a thin grid across the column."""
    fmt = QTextTableFormat()
    fmt.setBorder(1)
    fmt.setCellPadding(6)
    fmt.setCellSpacing(0)
    fmt.setBorderCollapse(True)
    fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
    return fmt


def _header_format(header: bool) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFontWeight(QFont.Weight.Bold if header else QFont.Weight.Normal)
    return fmt


def _style_row(table, row: int) -> None:
    """The cells of ``row`` in the header's style (row 0) or the body's:
    their words, and what is typed into them."""
    header = row == 0
    weight = _header_format(header)
    for column in range(table.columns()):
        cell = table.cellAt(row, column)
        first = cell.firstCursorPosition()
        first.setBlockCharFormat(weight)
        words = QTextCursor(first)
        words.setPosition(cell.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
        if words.hasSelection():
            words.mergeCharFormat(weight)


def cell_text(cell) -> str:
    """The words of a table cell."""
    cursor = QTextCursor(cell.firstCursorPosition())
    cursor.setPosition(cell.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
    return cursor.selectedText().replace("\u2029", " ").replace("\u2028", " ").strip()


def tables_of(doc) -> List[QTextTable]:
    """Every table in ``doc``, in order."""
    found: List[QTextTable] = []

    def walk(frame) -> None:
        for child in frame.childFrames():
            if isinstance(child, QTextTable):
                found.append(child)
            walk(child)
    walk(doc.rootFrame())
    return found


def insert_table(cursor: QTextCursor, rows: int, columns: int) -> Optional[QTextTable]:
    """A table of ``rows`` (the header row included) and ``columns``, as a
    block of its own: in place of an empty paragraph, else after the
    paragraph at the cursor, never inside code, a divider or another
    table. The cursor goes into its first cell. One step on the undo
    stack; None when there is no place for one."""
    rows, columns = max(1, rows), max(1, columns)
    block = cursor.block()
    if cursor.currentTable() is not None or is_code_block(block) or is_divider(block):
        return None
    doc = cursor.document()
    edit = QTextCursor(doc)
    edit.beginEditBlock()
    at = QTextCursor(block)
    at.movePosition(QTextCursor.MoveOperation.EndOfBlock)
    table = at.insertTable(rows, columns, table_format())
    previous = block.previous()
    if not block.text() and previous.isValid() and QTextCursor(previous).currentTable() is None:
        # In an empty paragraph, Qt keeps the paragraph before the table:
        # it goes, so the table takes its place.
        joint = QTextCursor(previous)
        joint.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        joint.setPosition(block.position(), QTextCursor.MoveMode.KeepAnchor)
        joint.removeSelectedText()
    _style_row(table, 0)
    after = QTextCursor(doc)
    after.setPosition(table.lastPosition() + 1)
    if not after.block().text():
        # What follows the table is a Body paragraph, whatever the
        # paragraph it was split from was.
        after.setBlockFormat(QTextBlockFormat())
        after.setBlockCharFormat(QTextCharFormat())
    edit.endEditBlock()
    cursor.setPosition(table.cellAt(0, 0).firstPosition())
    return table


def _place(cursor: QTextCursor, table, row: int, column: int) -> None:
    row = max(0, min(row, table.rows() - 1))
    column = max(0, min(column, table.columns() - 1))
    cursor.setPosition(table.cellAt(row, column).firstPosition())


def add_table_row(cursor: QTextCursor, *, below: bool) -> bool:
    """A row above or below the one at the cursor, which goes to its first
    cell, ready to be filled. A row added above the header is the new
    header. One undo step."""
    table = cursor.currentTable()
    if table is None:
        return False
    cell = table.cellAt(cursor)
    row = cell.row() + (1 if below else 0)
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    table.insertRows(row, 1)
    _style_row(table, row)
    if row == 0:
        _style_row(table, 1)              # the old header is a row now
    edit.endEditBlock()
    _place(cursor, table, row, 0)
    return True


def add_table_column(cursor: QTextCursor, *, after: bool) -> bool:
    """A column before or after the one at the cursor, which goes into
    it. One undo step."""
    table = cursor.currentTable()
    if table is None:
        return False
    cell = table.cellAt(cursor)
    column = cell.column() + (1 if after else 0)
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    table.insertColumns(column, 1)
    header = table.cellAt(0, column).firstCursorPosition()
    header.setBlockCharFormat(_header_format(True))
    for row in range(1, table.rows()):
        table.cellAt(row, column).firstCursorPosition().setBlockCharFormat(
            _header_format(False))
    edit.endEditBlock()
    _place(cursor, table, cell.row(), column)
    return True


def delete_table(cursor: QTextCursor) -> bool:
    """The table at the cursor goes; the cursor goes where it stood."""
    table = cursor.currentTable()
    if table is None:
        return False
    start = table.firstPosition() - 1
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    table.removeRows(0, table.rows())
    edit.endEditBlock()
    cursor.setPosition(min(max(0, start), cursor.document().characterCount() - 1))
    return True


def delete_table_row(cursor: QTextCursor) -> bool:
    """The row at the cursor goes (the last one takes the table with it).
    The header's row gone, the next row is the header. One undo step."""
    table = cursor.currentTable()
    if table is None:
        return False
    if table.rows() == 1:
        return delete_table(cursor)
    cell = table.cellAt(cursor)
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    table.removeRows(cell.row(), 1)
    if cell.row() == 0:
        _style_row(table, 0)
    edit.endEditBlock()
    _place(cursor, table, cell.row(), cell.column())
    return True


def delete_table_column(cursor: QTextCursor) -> bool:
    """The column at the cursor goes (the last one takes the table with
    it). One undo step."""
    table = cursor.currentTable()
    if table is None:
        return False
    if table.columns() == 1:
        return delete_table(cursor)
    cell = table.cellAt(cursor)
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    table.removeColumns(cell.column(), 1)
    edit.endEditBlock()
    _place(cursor, table, cell.row(), cell.column())
    return True


def next_table_cell(cursor: QTextCursor, step: int) -> bool:
    """Into the next cell (step 1, adding a row after the last cell, one
    undo step) or the previous one (-1). False outside a table."""
    table = cursor.currentTable()
    if table is None:
        return False
    cell = table.cellAt(cursor)
    index = cell.row() * table.columns() + cell.column() + step
    if index < 0:
        index = 0
    elif index >= table.rows() * table.columns():
        edit = QTextCursor(cursor.document())
        edit.beginEditBlock()
        table.appendRows(1)
        _style_row(table, table.rows() - 1)
        edit.endEditBlock()
    _place(cursor, table, index // table.columns(), index % table.columns())
    return True


def next_table_row(cursor: QTextCursor) -> bool:
    """Into the cell below (a new row after the last one, one undo step).
    False outside a table."""
    table = cursor.currentTable()
    if table is None:
        return False
    cell = table.cellAt(cursor)
    if cell.row() + 1 >= table.rows():
        edit = QTextCursor(cursor.document())
        edit.beginEditBlock()
        table.appendRows(1)
        _style_row(table, table.rows() - 1)
        edit.endEditBlock()
    _place(cursor, table, cell.row() + 1, cell.column())
    return True


def unescape_cell_code(doc) -> None:
    """A pipe in code in a table cell, as Qt's reader leaves it: with the
    backslash GFM asks for there (the writer writes `a\\|b`), which GFM
    readers drop. Dropped here too, so the code reads as written."""
    edits = []
    for table in tables_of(doc):
        for row in range(table.rows()):
            for column in range(table.columns()):
                cell = table.cellAt(row, column)
                it = cell.begin()
                while not it.atEnd():
                    block = it.currentBlock()
                    if block.isValid():
                        frag = block.begin()
                        while not frag.atEnd():
                            fragment = frag.fragment()
                            if (fragment.isValid() and fragment.charFormat().fontFixedPitch()
                                    and "\\|" in fragment.text()):
                                edits.append((fragment.position(), fragment.length(),
                                              fragment.text().replace("\\|", "|"),
                                              QTextCharFormat(fragment.charFormat())))
                            frag += 1
                    it += 1
    for position, length, text, fmt in reversed(edits):
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text, fmt)


def leave_table(cursor: QTextCursor) -> bool:
    """Out of the table, into a new paragraph right below it (or the empty
    one already there). False outside a table."""
    table = cursor.currentTable()
    if table is None:
        return False
    doc = cursor.document()
    after = QTextCursor(doc)
    after.setPosition(table.lastPosition() + 1)
    block = after.block()
    if block.text() or QTextCursor(block).currentTable() is not None \
            or block.textList() is not None or block.blockFormat().headingLevel():
        after.beginEditBlock()
        after.insertBlock(QTextBlockFormat(), QTextCharFormat())
        after.movePosition(QTextCursor.MoveOperation.PreviousBlock)
        after.endEditBlock()
    cursor.setPosition(after.position())
    return True


# --------------------------------------------------------------------------- #
# Links                                                                        #
# --------------------------------------------------------------------------- #

_LINK_PROPERTIES = (QTextFormat.Property.IsAnchor, QTextFormat.Property.AnchorHref,
                    QTextFormat.Property.AnchorName)


def link_range(cursor: QTextCursor):
    """``(start, end, href)`` of the link at the caret (touching it from
    either side) or holding the whole selection; None when there is none.
    Pieces of one link in different styles count as one link."""
    if cursor.isNull():
        return None
    doc = cursor.document()
    block = doc.findBlock(cursor.selectionStart())
    pieces = []
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid():
            fmt = fragment.charFormat()
            pieces.append((fragment.position(), fragment.position() + fragment.length(),
                           fmt.anchorHref() if fmt.isAnchor() else ""))
        it += 1
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    for index, (first, last, href) in enumerate(pieces):
        if not href:
            continue
        touches = (first <= start <= last and first <= end <= last) if start != end else \
            first <= start <= last
        if not touches:
            continue
        low, high = index, index
        while low > 0 and pieces[low - 1][2] == href and pieces[low - 1][1] == pieces[low][0]:
            low -= 1
        while high + 1 < len(pieces) and pieces[high + 1][2] == href \
                and pieces[high + 1][0] == pieces[high][1]:
            high += 1
        return pieces[low][0], pieces[high][1], href
    return None


def set_link(cursor: QTextCursor, text: str, href: str) -> None:
    """Make the selection a link to ``href``, as one step on the undo
    stack. With ``text`` that differs from the selection (or with nothing
    selected), the words are replaced by ``text``, in the style of the
    first selected character; otherwise their bold or italic stays."""
    cursor.beginEditBlock()
    anchor = QTextCharFormat()
    anchor.setAnchor(True)
    anchor.setAnchorHref(href)
    selected = cursor.selectedText()
    if text and text != selected:
        style = QTextCharFormat(cursor.charFormat()) if not cursor.hasSelection() else \
            _first_char_format(cursor)
        for prop in _LINK_PROPERTIES:
            style.clearProperty(prop)
        style.merge(anchor)
        cursor.insertText(text, style)
        cursor.setPosition(cursor.position() - doc_length(text), QTextCursor.MoveMode.KeepAnchor)
    elif cursor.hasSelection():
        cursor.mergeCharFormat(anchor)
    cursor.endEditBlock()


def _first_char_format(cursor: QTextCursor) -> QTextCharFormat:
    probe = QTextCursor(cursor.document())
    probe.setPosition(cursor.selectionStart() + 1)
    return QTextCharFormat(probe.charFormat())


def remove_link(cursor: QTextCursor) -> bool:
    """Take the link at the cursor away; the words stay, plain. One step
    on the undo stack. False when there is no link there."""
    found = link_range(cursor)
    if found is None:
        return False
    start, end, _href = found
    whole = QTextCursor(cursor.document())
    whole.setPosition(start)
    whole.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    whole.beginEditBlock()
    # A link read from Markdown also carries the link color: that goes
    # with it, or the words would keep a color of their own.
    _clear_properties(whole, (*_LINK_PROPERTIES, QTextFormat.Property.ForegroundBrush,
                              QTextFormat.Property.FontUnderline,
                              QTextFormat.Property.TextUnderlineStyle),
                      keep=lambda fmt, prop: not fmt.isAnchor())
    whole.endEditBlock()
    return True


def without_link(fmt: QTextCharFormat) -> QTextCharFormat:
    """``fmt`` with no link: what is typed right after a link is not part
    of it."""
    plain = QTextCharFormat(fmt)
    for prop in (*_LINK_PROPERTIES, QTextFormat.Property.ForegroundBrush,
                 QTextFormat.Property.FontUnderline, QTextFormat.Property.TextUnderlineStyle):
        plain.clearProperty(prop)
    return plain


# --------------------------------------------------------------------------- #
# Code blocks and pasted documents                                             #
# --------------------------------------------------------------------------- #

def is_code_block(block) -> bool:
    """Whether the paragraph is a line of a code block (fenced or indented,
    as Qt's Markdown reader marks them)."""
    fmt = block.blockFormat()
    return bool(fmt.property(QTextFormat.Property.BlockCodeFence)) or fmt.hasProperty(
        QTextFormat.Property.BlockCodeLanguage)


CODE_FENCE = "`"


def code_language(block) -> Optional[str]:
    """The language a code line's block names ("" for none), or None for a
    paragraph that is no code."""
    if not is_code_block(block):
        return None
    return str(block.blockFormat().property(QTextFormat.Property.BlockCodeLanguage) or "")


def _code_key(block):
    """What makes lines one code block to the Markdown writer: the same
    fence, language and quote; None for a paragraph that is no code."""
    language = code_language(block)
    if language is None:
        return None
    fence = block.blockFormat().property(QTextFormat.Property.BlockCodeFence)
    return str(fence or CODE_FENCE), language, quote_depth(block)


def same_code_block(one, other) -> bool:
    """Whether two lines are lines of one code block (given they are next
    to each other)."""
    key = _code_key(one)
    return key is not None and key == _code_key(other)


def code_run(block):
    """The first and last line of the code block ``block`` is a line of:
    the lines next to it with the same fence, language and quote, which
    Markdown writes as one fenced block."""
    key = _code_key(block)
    first = last = block
    while first.previous().isValid() and _code_key(first.previous()) == key:
        first = first.previous()
    while last.next().isValid() and _code_key(last.next()) == key:
        last = last.next()
    return first, last


def _code_text_format() -> QTextCharFormat:
    """The text format of a code line, as Qt's Markdown reader gives it
    (the system's fixed font); the editor shows it in its code font."""
    fmt = QTextCharFormat()
    fmt.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return fmt


def _restyle_runs(block, fmt: QTextCharFormat) -> None:
    """Every piece of text in ``block`` in exactly ``fmt`` (pictures stay)."""
    whole = QTextCursor(block)
    whole.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    if whole.hasSelection():
        for start, end, _fmt in _text_runs(whole):
            piece = QTextCursor(block.document())
            piece.setPosition(start)
            piece.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            piece.setCharFormat(fmt)
    QTextCursor(block).setBlockCharFormat(fmt)


def code_line_format(fmt: QTextBlockFormat, language: str) -> QTextBlockFormat:
    """``fmt`` made the format of a line of a fenced code block in
    ``language``, as Qt's Markdown reader makes it (so the writer fences
    it and names the language)."""
    fmt.setProperty(QTextFormat.Property.BlockCodeFence, CODE_FENCE)
    fmt.setProperty(QTextFormat.Property.BlockCodeLanguage, language)
    fmt.setNonBreakableLines(True)
    fmt.clearProperty(QTextFormat.Property.BlockMarker)
    fmt.setTopMargin(0)
    fmt.setBottomMargin(0)
    return fmt


def _links_as_markdown(block) -> None:
    """Each link in ``block`` written out as its Markdown (``[words](address)``),
    so code made of the paragraph keeps where the link went."""
    found = []
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid() and fragment.charFormat().isAnchor() \
                and fragment.charFormat().anchorHref():
            found.append((fragment.position(), fragment.length(), fragment.text(),
                          fragment.charFormat().anchorHref()))
        it += 1
    # Pieces of one link, one after the other, are one link.
    merged = []
    for position, length, words, href in found:
        if merged and merged[-1][3] == href and merged[-1][0] + merged[-1][1] == position:
            start, size, before, _href = merged[-1]
            merged[-1] = (start, size + length, before + words, href)
        else:
            merged.append((position, length, words, href))
    for position, length, words, href in reversed(merged):
        cursor = QTextCursor(block.document())
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(f"[{words}]({href})", without_link(cursor.charFormat()))


def make_code_block(cursor: QTextCursor, language: str = "") -> None:
    """The paragraphs under the cursor become the lines of one code block
    in ``language`` (as a fence names it; "" for none), as one step on the
    undo stack. A heading becomes Body and a list item leaves its list
    first; the words lose their styles, since a code block holds plain
    text and is written as it is. Dividers and table cells stay."""
    blocks = [b for b in _blocks_of(cursor) if not is_divider(b)
              and QTextCursor(b).currentTable() is None]
    if not blocks:
        return
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    text = _code_text_format()
    for block in blocks:
        if block.blockFormat().headingLevel():
            set_heading(QTextCursor(block), BODY)
        if block.textList() is not None:
            leave_list(block)
        QTextCursor(block).setBlockFormat(code_line_format(block.blockFormat(), language))
        _links_as_markdown(block)
        _restyle_runs(block, text)
    edit.endEditBlock()


def set_code_language(cursor: QTextCursor, language: str) -> None:
    """The code blocks under the cursor, each whole, in ``language``
    ("" for none), as one step on the undo stack."""
    lines = []
    for block in _blocks_of(cursor):
        if is_code_block(block) and block not in lines:
            first, last = code_run(block)
            line = first
            while line.isValid():
                lines.append(line)
                if line == last:
                    break
                line = line.next()
    if not lines:
        return
    edit = QTextCursor(cursor.document())
    edit.beginEditBlock()
    for line in lines:
        fmt = line.blockFormat()
        fmt.setProperty(QTextFormat.Property.BlockCodeLanguage, language)
        QTextCursor(line).setBlockFormat(fmt)
    edit.endEditBlock()


def leave_code(block) -> None:
    """A line of code becomes a Body paragraph; its words stay, plain."""
    fmt = block.blockFormat()
    fmt.clearProperty(QTextFormat.Property.BlockCodeFence)
    fmt.clearProperty(QTextFormat.Property.BlockCodeLanguage)
    fmt.setNonBreakableLines(False)
    QTextCursor(block).setBlockFormat(fmt)
    _restyle_runs(block, QTextCharFormat())


def _has_own_style(block) -> bool:
    """Whether a paragraph is more than Body text: a heading, a list item,
    a quote, a code line or a divider."""
    return bool(block.blockFormat().headingLevel() or block.textList() is not None
                or quote_depth(block) or is_code_block(block) or is_divider(block))


def _stands_alone(block) -> bool:
    """Whether a paragraph cannot share its line with other words: a line
    of code, a divider."""
    return is_code_block(block) or is_divider(block)


def _restyle(doc, start: int, end: int, level: int) -> None:
    """The text from ``start`` to ``end`` in the paragraph style ``level``."""
    if end <= start:
        return
    span = QTextCursor(doc)
    span.setPosition(start)
    span.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    for first, last, fmt in _text_runs(span):
        piece = QTextCursor(doc)
        piece.setPosition(first)
        piece.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
        piece.setCharFormat(restyled(fmt, level))


def _take_style(target, first) -> None:
    """The empty paragraph ``target`` became ``first``, the first pasted
    paragraph: it takes its style, list included."""
    fmt = QTextBlockFormat(first.blockFormat())
    fmt.clearProperty(QTextFormat.Property.ObjectIndex)    # the list is set below
    QTextCursor(target).setBlockFormat(fmt)
    pasted_list = first.textList()
    if pasted_list is None:
        return
    following = first.next()
    joined = None
    if (following.isValid() and following.textList() is not None
            and following.textList().objectIndex() == pasted_list.objectIndex()):
        # The next pasted item came from the same list: join its copy.
        joined = target.next().textList()
    if joined is not None:
        joined.add(target)
    else:
        QTextCursor(target).createList(pasted_list.format())


def _one_line(source):
    """``source`` as one line of text, its paragraphs joined by spaces:
    what a table cell can hold."""
    line = QTextDocument()
    out = QTextCursor(line)
    for block in iter_blocks(source):
        if not block.text().strip():
            continue
        if not out.atStart():
            out.insertText(" ", QTextCharFormat())
        level = block.blockFormat().headingLevel()
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            it += 1
            if fragment.isValid():
                fmt = fragment.charFormat()
                out.insertText(fragment.text(), restyled(fmt, 0) if level else fmt)
    return line


def insert_document(cursor: QTextCursor, source) -> None:
    """Insert the document ``source`` at the cursor in place of the
    selection, as one step on the undo stack, the way typing it would:

    - its first paragraph goes on from the words before the caret and its
      last runs into the words after it, each in the style of the
      paragraph it joins (a heading pasted into a sentence becomes words
      of that sentence);
    - into an empty paragraph, the first pasted paragraph comes with its
      own style, so a heading stays a heading and an item an item;
    - code and dividers never run into words: pasted inside a paragraph,
      they split it and stand on lines of their own;
    - pasted list items join a list of the same kind they land next to;
    - into a table cell it comes as one line, which is what a cell holds;
    - pasted on a divider, it goes below it.

    The cursor ends after what was pasted."""
    doc = cursor.document()
    cursor.beginEditBlock()
    if cursor.hasSelection():
        cursor.removeSelectedText()
    if is_divider(cursor.block()):
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
    if cursor.currentTable() is not None:
        source = _one_line(source)
    first = source.begin()
    if cursor.block().text():
        # A code line or a divider at either end of what is pasted gets a
        # line of its own: split the paragraph there.
        if _stands_alone(source.lastBlock()) and not cursor.atBlockEnd():
            cursor.insertBlock()
            cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        if _stands_alone(first) and not cursor.atBlockStart():
            cursor.insertBlock()
    target = cursor.block()
    empty = not target.text()
    level = target.blockFormat().headingLevel()
    start = cursor.position()
    cursor.insertFragment(QTextDocumentFragment(source))
    end = cursor.position()
    landed = doc.findBlock(start)
    last = doc.findBlock(end)
    if empty and _has_own_style(first):
        _take_style(landed, first)
    elif first.blockFormat().headingLevel() != level:
        # The first pasted words joined a paragraph of another style.
        _restyle(doc, start, min(end, landed.position() + landed.length() - 1), level)
    if last != landed:
        # The words after the caret went on in the last pasted paragraph.
        tail_level = last.blockFormat().headingLevel()
        if tail_level != level:
            _restyle(doc, end, last.position() + last.length() - 1, tail_level)
    around = [landed.previous()]
    block = landed
    while block.isValid():
        around.append(block)
        if block == last:
            break
        block = block.next()
    around.append(last.next())
    _tidy_lists(doc, around)
    cursor.setPosition(end)
    cursor.endEditBlock()
