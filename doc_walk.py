#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared QTextDocument traversal helpers for the exporters.

The editor stores bullets as literal text: lines prefixed with "• " and
indented in 4-space steps (see HtmlEditor._indent_level_and_has_bullet).
Inline formatting lives on QTextFragment char formats. Every handrolled
exporter (HTML, R Markdown) needs the same walk: blocks in order, each
block as a list of (text, format) runs, with the bullet marker stripped
without dragging its formatting along.

Image fragments carry the object-replacement character (U+FFFC) as text
and a char format where isImageFormat() is true; exporters decide how to
serialize them.
"""

import re

from PySide6.QtGui import QTextFormat

BULLET_MARKER = "• "

# Spaces per bullet nesting level. Mirrors tab_width in editor.py; kept as a
# constant here so exporters never import the widget class.
INDENT_STEP = 4

# Object-replacement character: the text an image fragment carries.
OBJECT_REPLACEMENT = "\ufffc"

# Qt uses U+2028 for Shift+Enter line breaks inside a block. toPlainText()
# maps it to a newline; fragment text does not, so any serializer that
# replaces toPlainText has to do the mapping itself.
LINE_SEPARATOR = "\u2028"

# A soft line break: lines of one paragraph that follow each other in the
# file ("hard-wrapped" prose, a picture on the line under its words). The
# editor holds it as a line separator whose character format carries this
# property, shows it as a new line, and writes it back as the plain line
# end it was, so a note keeps its line layout (markdown_writer). A line
# break typed here (Shift+Return) has no such property and is written as
# Markdown's hard break. (editor.HtmlEditor keeps the property off what is
# typed after one.)
SOFT_BREAK_PROPERTY = QTextFormat.Property.UserProperty + 0x5B1

# Words a person wrote in a picture's caption field are text, whatever
# characters they hold (EDITING 2.1): "*", "_" or "[x](y)" typed there is
# written escaped (markdown_writer.literal_text), so the file reads them
# back as typed. The caption's own formatting, from the file, stays
# Markdown. photo_caption.CaptionRun.typed carries it.
TYPED_TEXT_PROPERTY = QTextFormat.Property.UserProperty + 0x5B3


# Python counts a string in code points; a QTextDocument counts positions
# in UTF-16 units, where a character beyond U+FFFF (an emoji, a skin tone,
# a flag, a rare CJK character) takes two. A place found in a block's
# text by Python (a regular expression's match) is converted before it
# is added to the block's position, and a position in a block before it
# slices the block's text.
_BEYOND_BMP = re.compile("[\U00010000-\U0010FFFF]")


def doc_length(text: str) -> int:
    """How many document positions ``text`` takes (its UTF-16 units)."""
    return len(text) + len(_BEYOND_BMP.findall(text))


def doc_offset(text: str, index: int) -> int:
    """The offset in the document of ``text[index]`` (``text`` being a
    block's text, or its start)."""
    return doc_length(text[:index])


def text_index(text: str, offset: int) -> int:
    """The index in ``text`` of the document offset ``offset`` within it
    (``cursor.positionInBlock()`` in ``block.text()``); one in the middle
    of a character counts as that character's end."""
    if not _BEYOND_BMP.search(text):
        return min(max(offset, 0), len(text))
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def block_units(block) -> bytes:
    """``block``'s text as Qt holds it: its UTF-16 units, little-endian, a
    lone surrogate included, which PySide leaves out of ``block.text()``
    (so places found in that text would not be Qt's). Read unit by unit
    from the document only when the text holds fewer units (doc_length)
    than the block. Where whole characters are enough, doc_offset and
    text_index convert a place; this keeps every unit (mac_words)."""
    text = block.text()
    if doc_length(text) == block.length() - 1:
        return text.encode("utf-16-le", "surrogatepass")
    doc = block.document()
    return "".join(doc.characterAt(position) for position in range(
        block.position(), block.position() + block.length() - 1)).encode(
            "utf-16-le", "surrogatepass")


def iter_blocks(doc):
    """Yield the document's QTextBlocks in order."""
    block = doc.begin()
    while block.isValid():
        yield block
        block = block.next()


def iter_block_runs(block):
    """Yield (text, QTextCharFormat) for each fragment of a block, in order."""
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            yield frag.text(), frag.charFormat()
        it += 1


def parse_bullet_line(line: str) -> tuple[int, bool]:
    """Return (leading_spaces, has_bullet) for a block's text.

    Mirrors HtmlEditor._indent_level_and_has_bullet (editor.py) so exporters
    and the editor agree on what counts as a bullet line.
    """
    spaces = 0
    while spaces < len(line) and line[spaces] == ' ':
        spaces += 1
    return spaces, line[spaces:spaces + 2] == BULLET_MARKER


def bullet_depth(spaces: int) -> int:
    """Nesting depth (>= 1) for a bullet with the given leading spaces.

    First-level bullets normally sit at 4 spaces, but Backtab can leave a
    bullet at column 0; both map to depth 1. Ragged indents floor.
    """
    return max(1, spaces // INDENT_STEP)


def iter_image_names(doc):
    """Yield the name() of every image fragment, in document order.

    Duplicates are kept: a caller counting occurrences needs them, and a
    caller wanting unique names can build a set in one line.
    """
    for block in iter_blocks(doc):
        for _text, fmt in iter_block_runs(block):
            if fmt.isImageFormat():
                yield fmt.toImageFormat().name()


def image_positions(doc, name: str) -> list:
    """The position of every image fragment named ``name``, in order."""
    found = []
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if fmt.isImageFormat() and fmt.toImageFormat().name() == name:
                found.extend(range(frag.position(), frag.position() + frag.length()))
            it += 1
    return found


def skip_prefix(runs, n: int):
    """Drop the first n characters from a list of (text, fmt) runs.

    Used to strip the indentation plus bullet marker ("    • ") from a bullet
    block. The marker may straddle fragment boundaries (e.g. when the user
    toggled bold mid-line), so a straddling run is sliced and any formatting
    on the marker itself is discarded. Runs that become empty are dropped.
    """
    out = []
    remaining = n
    for text, fmt in runs:
        if remaining >= len(text):
            remaining -= len(text)
            continue
        if remaining:
            text = text[remaining:]
            remaining = 0
        if text:
            out.append((text, fmt))
    return out
