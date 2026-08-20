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


def serialize_plain_with_images(doc, image_target) -> str:
    """Plain text with each image replaced by image_target(QTextImageFormat).

    The text half reproduces toPlainText exactly, which is why this can
    stand in for it everywhere images must survive: blocks joined with
    newlines, U+2028 line separators mapped to newlines, stray
    object-replacement characters in plain runs dropped.

    A target function returning None omits the image entirely, which is
    what a format with no way to carry one (.txt) needs.
    """
    lines = []
    for block in iter_blocks(doc):
        parts = []
        for text, fmt in iter_block_runs(block):
            if fmt.isImageFormat():
                target = image_target(fmt.toImageFormat())
                if target:
                    parts.append(target)
                continue
            text = text.replace(OBJECT_REPLACEMENT, "")
            if text:
                parts.append(text.replace(LINE_SEPARATOR, "\n"))
        lines.append("".join(parts))
    return "\n".join(lines)


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
