# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A document as plain text, its images written as the caller says.

What a tab opened as its text is saved and published as (Markdown shown
as its source, a .txt file): the text exactly as toPlainText gives it,
with each image where it stands.
"""

from __future__ import annotations

import photo_group
from doc_walk import LINE_SEPARATOR, OBJECT_REPLACEMENT, iter_block_runs, iter_blocks


def serialize_plain_with_images(doc, image_target) -> str:
    """Plain text with each image replaced by image_target(QTextImageFormat).

    The text half reproduces toPlainText exactly, which is why this can
    stand in for it everywhere images must survive: blocks joined with
    newlines, U+2028 line separators mapped to newlines, stray
    object-replacement characters in plain runs dropped.

    A target function returning None omits the image entirely, which is
    what a format with no way to carry one (.txt) needs. A photo group is
    written as the paragraphs it was read from.
    """
    doc = photo_group.unfolded(doc)
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
