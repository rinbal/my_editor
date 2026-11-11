# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finding the photo groups of a document, and folding them.

Two or more pictures with nothing but empty lines between them are a
group, as STANDUP's md.py and designBlocks.js find them: a picture
paragraph (with its caption on it, or in the italic paragraph that
follows), then the next one. find_runs reads the paragraphs at the top
of a document for them (a paragraph in a list, a quote, a heading, code
or a table is never part of one), and fold makes each group the one
paragraph photo_group holds it as.

A document is read once for all its groups (10,000 of them in the time
it takes to read the Markdown again), or between two places for the few
paragraphs an edit touched (photo_group_edit).
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtGui import QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument

import media_files
from doc_walk import (LINE_SEPARATOR, OBJECT_REPLACEMENT, SOFT_BREAK_PROPERTY,
                      TYPED_TEXT_PROPERTY, doc_length)
from photo_group import (
    GROUP,
    SOFT,
    CaptionRun,
    Unit,
    block_segments,
    is_plain_paragraph,
    own_format,
    replace_paragraphs,
    unit_of_picture,
)

# --------------------------------------------------------------------------- #
# Reading a paragraph                                                          #
# --------------------------------------------------------------------------- #

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
    return bool(name) and not _is_media_address(name)


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
                            fmt.boolProperty(SOFT_BREAK_PROPERTY),
                            fmt.boolProperty(TYPED_TEXT_PROPERTY))
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


def _is_media_address(name: str) -> bool:
    """Whether ``name`` is audio or video by its ending (media_files).
    Asked for every picture of a document: an ending that stands plainly
    at the end of the name is looked up once."""
    dot = name.rfind(".")
    ending = name[dot:]
    if dot < 1 or name[dot - 1] in "/\\." or "?" in name or "#" in name \
            or "/" in ending or "\\" in ending:
        return media_files.is_audio_or_video(name)
    found = _media_endings.get(ending)
    if found is None:
        found = _media_endings[ending] = media_files.is_audio_or_video(name)
    return found


_media_endings: Dict[str, bool] = {}


def picture_units(block, text: Optional[str] = None,
                  fmt: Optional[QTextBlockFormat] = None) -> Optional[List[Unit]]:
    """The units of a picture paragraph: one picture with an optional
    caption on it, or two or more pictures with only spaces and line
    breaks between them; None for any other paragraph. (``text`` and
    ``fmt``: the paragraph's, when the caller has them.)"""
    if text is None:
        text = block.text()
    if not text or text[0] != OBJECT_REPLACEMENT or not is_plain_paragraph(block, fmt):
        return None
    if len(text) == 1:
        # One character, the common case: no other piece to look at.
        picture = block.begin().fragment().charFormat()
        if not picture.isImageFormat():
            return None
        unit = unit_of_picture(picture)
        return [unit] if unit.name and not _is_media_address(unit.name) else None
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
    if not frames and length is len:
        # Paragraphs alone, a position a character: no loop to find them.
        texts = raw.split("\u2029")
        starts = itertools.accumulate((len(text) + 1 for text in texts), initial=0)
        return list(zip(starts, texts))
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
            units = picture_units(block, text, fmt) if lone else None
        if units is None:
            run = None
            continue
        end = position + block.length() - 1
        if not group and len(units) == 1 and not units[0].caption and i < count:
            below = items[i][1]
            # (Looked at only when it can be a caption: it has words and
            # no picture.)
            caption = (caption_paragraph(doc.findBlock(items[i][0]), below)
                       if below and OBJECT_REPLACEMENT not in below else None)
            if caption is not None:
                units[0].caption = caption
                end = items[i][0] + doc_length(below)
                i += 1
        if run is None or not lone:
            run = Found([], position, end, format=own_format(block) if group else fmt)
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
# Folding                                                                      #
# --------------------------------------------------------------------------- #

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
            replace_paragraphs(cursor, run.first, run.end, run.format, run.units)
    cursor.endEditBlock()
    return len(runs)
