#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One way from the editor's document to Markdown, for every place that needs it.

Saving a .md file, publishing an article and saving a draft all write the
same Markdown, from :func:`document_to_markdown`. A short note (kind 1) is
plain text by convention, so :func:`document_to_note_text` writes the same
walk without markup: lists keep their markers, links keep their address,
and nothing shows as stray asterisks in an app that does not render
Markdown.

What is written follows what the editor shows ("Markdown first"):

- Bold, italic, strikethrough, inline code and links become their
  Markdown. Underline and colors have no Markdown and stay in local
  files only; they are dropped here.
- Headings, block quotes (with the lists and headings inside them),
  fenced and indented code, horizontal rules, tables and task lists that
  a .md file brought in are written back as such.
- Lists: Qt's own lists (from a .md file) and the editor's typed bullets
  ("    • item", see doc_walk) both become "- item", nested by depth.
- Lines: a paragraph that came from Markdown (it carries paragraph
  spacing) ends with a blank line, and so does every line typed one
  under the other: a blank line is the break every Nostr reader shows
  alike (NIP-23 asks for no hard line breaks inside a paragraph). Only
  Shift+Enter, an explicit line break, is written as one.
- Typed text is Markdown: what is typed is written as typed, so
  "## Heading", "[label](url)", a footnote mark ("[^1]", and its
  "[^1]: note") or a code fence typed by hand reach Nostr as such (the
  preview shows what they become). A web address
  that holds a character Markdown would read as emphasis is written as
  ``<address>``, which every reader keeps intact.
- A link that shows its own address (a pasted web address, an email) is
  written bare too, where readers find it by themselves: a picture or a
  video whose address stands alone on its line plays in the apps that
  look for one.

The clipboard needs the other reading of typed text: what the document
shows, letter for letter. ``document_to_markdown(doc, literal=True)``
escapes every character of the text that a reader would take for
Markdown (emphasis, code, a link, a tag, an entity, a heading or list
at the start of a line, an address it would link by itself), so the
text reads back as the same text and only the document's structure is
Markdown. The editor's own copy and a pasted web page are written so.

Images are written by the caller's ``image_target(QTextImageFormat)``,
which returns the text to put in the image's place (``![alt](url)`` or a
bare URL) or None to leave it out. The caller decides where an image
points (a Blossom URL, a sidecar file); this module never does.
"""

from __future__ import annotations

import hashlib
import html.entities
import logging
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtGui import (
    QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat,
    QTextImageFormat, QTextListFormat, QTextTable,
)

from doc_walk import (
    ANGLED_TEXT_PROPERTY,
    APART_PROPERTY,
    CODE_INFO_PROPERTY,
    BARE_TEXT_PROPERTY,
    BULLET_MARKER,
    ENTITY_TEXT_PROPERTY,
    ESCAPED_TEXT_PROPERTY,
    LINE_SEPARATOR,
    OBJECT_REPLACEMENT,
    SOFT_BREAK_PROPERTY,
    TYPED_TEXT_PROPERTY,
    apart,
    apart_mark,
    bullet_depth,
    doc_length,
    iter_block_runs,
    iter_blocks,
    iter_frame,
    parse_bullet_line,
    skip_prefix,
    table_rows,
)
import photo_group
import photo_group_find
from markdown_scan import (DELIMITER_ROW, EMPTY_ITEM_LINE, ESCAPABLE, FENCE_LINE, LIST_LINE,
                           autolinks, bracketed, descriptions, emphasized_code, empty_items,
                           ends_in_break, entities, escapes, fence_words, hard_breaks,
                           lossy_definition, misread_blocks, pictures, set_apart, titled_link)
from rich_text import code_info, normalize_after_markdown_load, same_code_block

log = logging.getLogger(__name__)

ImageTarget = Callable[[object], Optional[str]]

# How Markdown is read back into the editor (.md files, article drafts):
# GitHub's dialect without its underline extension, which reads
# ``_word_`` as underline (a local-only format this writer leaves out)
# where every Nostr reader shows italics, and without Qt's own front matter
# reading, which takes any leading "---" block for front matter and drops
# it: a file's front matter is read by front_matter.py (only a block with a
# key), and a document that opens with a divider keeps its first section.
_MD_FLAG_UNDERLINE = 0x4000
_QT_FRONT_MATTER = 0x100000
READ_FEATURES = QTextDocument.MarkdownFeature(
    QTextDocument.MarkdownFeature.MarkdownDialectGitHub.value
    & ~_MD_FLAG_UNDERLINE & ~_QT_FRONT_MATTER)

# How a paste is read: the same, but a tag is text. Text that shows a tag
# ("<div>", "ssh <user>@<host>") is about one, and HTML is no structure a
# document here can hold.
PASTE_FEATURES = QTextDocument.MarkdownFeature(
    READ_FEATURES.value | QTextDocument.MarkdownFeature.MarkdownNoHTML.value)

MARKDOWN = "markdown"
NOTE = "note"

# How a picture's description was spelled in the Markdown it was read from,
# kept on the picture when the writer would spell it otherwise ("Fish &amp;
# Chips" for "Fish & Chips"): while the description is what that spelling
# says, it is written as it was, so a file changes only where it was edited.
ALT_SPELLING_PROPERTY = QTextFormat.Property.UserProperty + 0x5B2
# How a soft line break (doc_walk.SOFT_BREAK_PROPERTY) travels through the
# writer, between reading the document and writing the lines (a character
# of Unicode's private area).
_SOFT = "\ue0b1"
# What marks a soft break while Qt reads the Markdown: a space at the end
# of a line made a hard break, which becomes the soft break once read. A
# space (Unicode's Zs), so a star beside it reads as at a line's end: the
# word joiner it was is none, and "*(Wort)*" or "__fett__" ending a
# wrapped line showed their stars. A file is read with the first one it
# holds nowhere (_free_mark); one that holds all of them is not held.
_SOFT_MARKS = ("\u2005", "\u2006", "\u2004", "\u2008")

# Indent per nesting level of a list. Four columns is past the content of
# both "- " and "1. " markers, and short of the four extra columns that
# would turn a nested item into a code block.
LIST_INDENT = 4

_ORDERED_STYLES = {
    QTextListFormat.Style.ListDecimal,
    QTextListFormat.Style.ListLowerAlpha,
    QTextListFormat.Style.ListUpperAlpha,
    QTextListFormat.Style.ListLowerRoman,
    QTextListFormat.Style.ListUpperRoman,
}

# A Nostr reference: a person, a note or an article. Written bare, whatever
# words its link shows (W2): that is the form every Nostr reader turns into
# a name or a card the same way; njump and others drop a labelled link to
# it ([label](nostr:...)) or show its address.
_NOSTR_REFERENCE = re.compile(r"nostr:(?:npub|nprofile|note|nevent|naddr)1[02-9ac-hj-np-z]+",
                              re.IGNORECASE)


def _is_nostr_reference(href: str) -> bool:
    return bool(_NOSTR_REFERENCE.fullmatch(href or ""))


def _apart(reference: str, before: str, following: Optional["_Span"]) -> str:
    """``reference`` with a space where a word would otherwise touch it,
    so readers find where it starts and ends."""
    if before and before[-1].isalnum():
        reference = " " + reference
    if following is not None and following.text[:1].isalnum():
        reference += " "
    return reference


# A bare web address, and the characters that make a Markdown reader see
# emphasis or code inside one.
_WEB_ADDRESS = re.compile(r"(?<![<(\w])(?:https?|wss?)://[^\s<>]+", re.IGNORECASE)
_MARKUP_IN_ADDRESS = set("_*~`")


# --------------------------------------------------------------------------- #
# The walk: the document as a list of blocks                                  #
# --------------------------------------------------------------------------- #

@dataclass
class _Span:
    text: str = ""
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    href: str = ""
    raw: bool = False          # an image's text from the caller, written as is
    literal: bool = False      # text to be read back as text (see literal_text)
    escaped: bool = False      # escaped in the file: written escaped again
    bare: bool = False         # a "<" bare in the file's link words: written bare again
    spelled: str = ""          # the entity the file wrote for it (ENTITY_TEXT_PROPERTY)
    angled: bool = False       # an address the file wrote in angle brackets: so again


@dataclass
class _Block:
    kind: str                  # paragraph, heading, item, quote, code, rule, table, empty
    spans: List[_Span] = field(default_factory=list)
    level: int = 0             # heading level, quote level, list depth
    ordered: bool = False
    number: int = 1
    task: Optional[bool] = None
    spaced: bool = False       # carries paragraph spacing (came from Markdown)
    indent: str = ""           # leading spaces typed before the text
    language: str = ""         # what a code block's fence says (rich_text.code_info)
    code_text: str = ""
    rows: List[List[List[_Span]]] = field(default_factory=list)
    bullet_text: str = ""      # a typed bullet line, exactly as typed
    group: int = -1            # which list an item belongs to
    other_marker: bool = False  # an item written with "*" or ")" (_set_list_markers)
    quote: int = 0             # how deep in block quotes it sits
    apart: Optional[int] = None  # set apart from the block before: the quotes that end (apart)
    literal: bool = False      # text to be read back as text (see literal_text)


def _is_bold(fmt) -> bool:
    return fmt.fontWeight() >= QFont.Weight.DemiBold


def _is_code(fmt) -> bool:
    return bool(fmt.fontFixedPitch())


def _spans_of(runs, image_target: Optional[ImageTarget]) -> List[_Span]:
    spans: List[_Span] = []
    for text, fmt in runs:
        if fmt.isImageFormat():
            if image_target is None:
                continue
            target = image_target(fmt.toImageFormat())
            if target:
                # A picture keeps the link on it ("[![a](p.png)](u)"); an
                # address standing bare (audio, video, a note's picture)
                # is the reader's to show, and stays bare.
                linked = fmt.isAnchor() and target.startswith("![")
                spans.append(_Span(text=_as_spelled(fmt, target), raw=True,
                                   href=fmt.anchorHref() if linked else ""))
            continue
        text = text.replace(OBJECT_REPLACEMENT, "")
        if not text:
            continue
        if fmt.boolProperty(SOFT_BREAK_PROPERTY):
            text = text.replace(LINE_SEPARATOR, _SOFT)
        href = fmt.anchorHref() if fmt.isAnchor() else ""
        spans.append(_Span(text=text, bold=_is_bold(fmt), italic=fmt.fontItalic(),
                           strike=fmt.fontStrikeOut(), code=_is_code(fmt),
                           href=href or "", literal=fmt.boolProperty(TYPED_TEXT_PROPERTY),
                           escaped=fmt.boolProperty(ESCAPED_TEXT_PROPERTY),
                           bare=fmt.boolProperty(BARE_TEXT_PROPERTY),
                           spelled=str(fmt.property(ENTITY_TEXT_PROPERTY) or ""),
                           angled=fmt.boolProperty(ANGLED_TEXT_PROPERTY)))
    return _merged(spans)


def _as_spelled(fmt, target: str) -> str:
    """``target`` (a picture as Markdown) with its description spelled as
    the file spelled it (ALT_SPELLING_PROPERTY), while that spelling still
    says the picture's description."""
    spelling = fmt.property(ALT_SPELLING_PROPERTY)
    if not spelling:
        return target
    alt = str(fmt.property(QTextFormat.Property.ImageAltText) or "")
    written = f"![{_description_markdown(alt)}]("
    if not target.startswith(written) or not _reads_back(spelling, alt):
        return target
    return f"![{spelling}](" + target[len(written):]


def _plain_weight(span: _Span) -> _Span:
    span.bold = False
    return span


def _merged(spans: List[_Span]) -> List[_Span]:
    """Join neighbours that read the same, so one bold word split across
    two fragments is not written as two bold words."""
    out: List[_Span] = []
    for span in spans:
        if (out and not span.raw and not out[-1].raw
                and (out[-1].bold, out[-1].italic, out[-1].strike, out[-1].code, out[-1].href,
                     out[-1].literal, out[-1].escaped, out[-1].bare, out[-1].spelled,
                     out[-1].angled)
                == (span.bold, span.italic, span.strike, span.code, span.href, span.literal,
                    span.escaped, span.bare, span.spelled, span.angled)):
            out[-1].text += span.text
        else:
            out.append(span)
    return out


def _block_runs(block):
    return list(iter_block_runs(block))


def _paragraph_block(block, image_target) -> _Block:
    return _paragraph_of(block.blockFormat(), _block_runs(block), block.text(), image_target)


def _paragraph_of(fmt, runs, text: str, image_target) -> _Block:
    """A paragraph in the format ``fmt`` holding ``runs`` (``text`` being
    their text), as the block it is written as."""
    spaced = fmt.topMargin() > 0 or fmt.bottomMargin() > 0
    spaces, is_bullet = parse_bullet_line(text)
    if is_bullet:
        return _Block("item", _spans_of(skip_prefix(runs, spaces + len(BULLET_MARKER)),
                                        image_target),
                      level=bullet_depth(spaces), spaced=spaced, bullet_text=text)
    if not text.strip(OBJECT_REPLACEMENT + " \t") and not any(
            f.isImageFormat() for _t, f in runs):
        return _Block("empty", spaced=spaced)
    indent = ""
    if spaces:
        indent = " " * spaces
        runs = skip_prefix(runs, spaces)
    heading = fmt.headingLevel()
    if heading:
        # A heading is bold by its own format; that is not emphasis.
        spans = _merged([_plain_weight(span) for span in _spans_of(runs, image_target)])
        return _Block("heading", spans, level=heading, spaced=True)
    quote = fmt.property(QTextFormat.Property.BlockQuoteLevel)
    spans = _spans_of(runs, image_target)
    if not spans and not text.strip(OBJECT_REPLACEMENT + " \t"):
        # Only objects, none of them written here (audio or video not
        # uploaded yet): no paragraph, rather than an empty one.
        return _Block("empty", spaced=spaced)
    if isinstance(quote, int) and quote > 0:
        return _Block("quote", spans, level=quote, spaced=spaced)
    return _Block("paragraph", spans, spaced=spaced, indent=indent)


def _unit_paragraphs(units, fmt) -> List[list]:
    """The paragraphs ``units`` were read from, each as its (text, format)
    runs: a picture's paragraph, its caption on it or in the next; pictures
    that were one paragraph, one again (photo_group.write_paragraphs writes
    the same into a document)."""
    paragraphs: List[list] = []
    previous = None
    for unit in units:
        if photo_group.joins_previous(previous, unit):
            paragraphs[-1].extend(photo_group.join_runs(
                unit.join, photo_group.link_between(previous, unit)))
        else:
            paragraphs.append([])
        previous = unit
        paragraphs[-1].append((OBJECT_REPLACEMENT, unit.picture_format()))
        if not unit.has_caption:
            continue
        if unit.caption_join is not None:
            paragraphs[-1].extend(photo_group.join_runs(unit.caption_join))
        else:
            paragraphs.append([])
        paragraphs[-1].extend(unit.caption_runs())
    return paragraphs


def _group_blocks(block, segments, image_target) -> List[_Block]:
    """A paragraph that holds units (a photo group's) as the paragraphs it
    was read from; anything else an edit left beside them, as paragraphs
    of its own between them."""
    fmt = QTextBlockFormat(block.blockFormat())
    fmt.clearProperty(photo_group.GROUP)
    blocks = []
    for _position, segment in segments:
        if isinstance(segment[0], photo_group.Unit):
            paragraphs = _unit_paragraphs(segment, fmt)
        else:
            paragraphs = [[(text, piece) for _at, text, piece in segment]]
        for runs in paragraphs:
            blocks.append(_paragraph_of(fmt, runs, "".join(text for text, _f in runs),
                                        image_target))
    return blocks


def _list_block(block, text_list, image_target) -> _Block:
    style = text_list.format().style()
    marker = block.blockFormat().marker()
    task = None
    if marker == QTextBlockFormat.MarkerType.Checked:
        task = True
    elif marker == QTextBlockFormat.MarkerType.Unchecked:
        task = False
    start = text_list.format().start() if hasattr(text_list.format(), "start") else 1
    return _Block("item", _spans_of(_block_runs(block), image_target),
                  level=max(1, text_list.format().indent()),
                  ordered=style in _ORDERED_STYLES,
                  number=max(1, start) + text_list.itemNumber(block),
                  task=task, spaced=block.blockFormat().bottomMargin() > 0,
                  group=text_list.objectIndex())


def _table_block(table: QTextTable, image_target) -> _Block:
    rows = []
    for number, cells in enumerate(table_rows(table)):
        row = []
        for blocks in cells:
            spans: List[_Span] = []
            for block in blocks:
                if spans:
                    spans.append(_Span(text=" "))
                spans.extend(_spans_of(_block_runs(block), image_target))
            if number == 0:
                # The header row is bold by its own format.
                spans = [_plain_weight(span) for span in spans]
            row.append(_merged(spans))
        rows.append(row)
    return _Block("table", rows=rows, spaced=True)


def _blocks_of_frame(frame, image_target) -> List[_Block]:
    blocks: List[_Block] = []
    for block in iter_frame(frame):
        if isinstance(block, QTextTable):
            blocks.append(_table_block(block, image_target))
            continue
        # A photo group is written as the paragraphs it was read from:
        # its pictures, each with its caption (photo_group).
        segments = photo_group.block_segments(block)
        if segments is not None:
            blocks.extend(_group_blocks(block, segments, image_target))
        elif blocks and blocks[-1].kind == "code" and same_code_block(block.previous(), block):
            # Qt keeps each line of a code block as a block of its own.
            blocks[-1].code_text += "\n" + code_text(block)
        else:
            blocks.append(_one_block(block, image_target))
    return blocks


def _one_block(block, image_target) -> _Block:
    written = _block_kind(block, image_target)
    written.apart = apart(block)
    quote = block.blockFormat().property(QTextFormat.Property.BlockQuoteLevel)
    if isinstance(quote, int) and quote > 0 and written.kind != "quote":
        # A list, heading or code block inside a quote stays in it.
        written.quote = quote
    return written


def _block_kind(block, image_target) -> _Block:
    fmt = block.blockFormat()
    if fmt.hasProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth):
        return _Block("rule", spaced=True)
    fence = fmt.property(QTextFormat.Property.BlockCodeFence)
    # Indented code carries a language property (empty) and no fence.
    if fence or fmt.hasProperty(QTextFormat.Property.BlockCodeLanguage):
        return _Block("code", language=code_info(block), code_text=code_text(block),
                      spaced=True)
    text_list = block.textList()
    if text_list is not None:
        return _list_block(block, text_list, image_target)
    return _paragraph_block(block, image_target)


def code_text(block) -> str:
    """A line of a code block as it is written: a line break in it a new
    line, a picture not at all."""
    return block.text().replace(LINE_SEPARATOR, "\n").replace(OBJECT_REPLACEMENT, "")


def document_blocks(doc, image_target: Optional[ImageTarget] = None) -> List[_Block]:
    """The document as the blocks both writers share. Exposed for tests."""
    return _blocks_of_frame(doc.rootFrame(), image_target)


# --------------------------------------------------------------------------- #
# Markdown                                                                     #
# --------------------------------------------------------------------------- #

def _protected(text: str) -> list:
    """The web addresses in ``text`` holding a character a Markdown reader
    would take for emphasis or code (matches of _WEB_ADDRESS)."""
    return [match for match in _WEB_ADDRESS.finditer(text)
            if any(ch in _MARKUP_IN_ADDRESS for ch in match.group(0))]


def _protect_addresses(text: str) -> str:
    """Typed text as typed, except a web address holding a character a
    Markdown reader would take for emphasis or code: that one is written
    as ``<address>``, which keeps it intact everywhere."""
    out, start = [], 0
    for match in _protected(text):
        out += [text[start:match.start()], f"<{match.group(0)}>"]
        start = match.end()
    return "".join(out) + text[start:]


# What a reader takes for Markdown in text: anywhere, the characters of
# emphasis, code, links, strikethrough, tags and entities, the colon of
# an address ("https://") and the @ of an email, which it would link by
# itself, and the dot of "www."; at the start of a line, a heading, a
# list item, a divider or an underline that makes a heading.
_MARKUP_ANYWHERE = re.compile(r"[\\`*_~\[\]<>&@]|:(?=//)|(?<=\b[wW][wW][wW])\.")
_MARKUP_AT_LINE_START = re.compile(r"^(\s*)(?:([#+=-])|(\d+)([.)]))")


def literal_text(text: str) -> str:
    """``text`` written so a Markdown reader reads it back as the same
    text: every character it would take for Markdown escaped."""
    text = _MARKUP_ANYWHERE.sub(lambda m: "\\" + m.group(0), text)
    found = _MARKUP_AT_LINE_START.match(text)
    if found is not None:
        lead, mark, digits, closer = found.groups()
        marker = "\\" + mark if mark else digits + "\\" + closer
        text = lead + marker + text[found.end():]
    return text


def _destination(href: str) -> str:
    """A link's address as Markdown accepts it: in ``<…>`` when it holds
    a space or a parenthesis, so it is never cut short or changed."""
    if any(ch in href for ch in " ()"):
        return "<" + href.replace("<", "%3C").replace(">", "%3E") + ">"
    return href


def code_span(text: str) -> str:
    """``text`` as inline code: in more backticks than it holds in a row,
    and apart from them where it starts or ends with one."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or (
        text.startswith(" ") and text.endswith(" ") and text.strip()) else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _wrap(text: str, marker: str) -> str:
    """``**text**`` with the spaces kept outside the markers, which is
    where Markdown needs them for the emphasis to count, and a soft line
    break at its ends too (it takes the format of the words before it):
    "*Wort*" ending a wrapped line was written "*Wort" and the next line
    began with the star, so the emphasis was gone."""
    edges = " " + _SOFT
    core = text.strip(edges)
    if not core:
        return text
    lead = text[:len(text) - len(text.lstrip(edges))]
    trail = text[len(text.rstrip(edges)):]
    return f"{lead}{marker}{core}{marker}{trail}"


def _own_address(words: str, href: str) -> str:
    """The address a link shows when its words are its own address (a web
    address, ``www.…`` or an email), as the words spell it; else ""."""
    if words == href:
        return words
    if href == "http://" + words and words.lower().startswith("www."):
        return words
    if href == "mailto:" + words and "@" in words:
        return words
    return ""


@lru_cache(maxsize=4096)
def _reads_back_whole(address: str, href: str) -> bool:
    """Whether Qt's reader (the editor's, the preview's, every reopen)
    reads ``address``, written bare, back as one link to ``href`` over all
    of it. It takes only some characters in a bare address: one with a
    percent sign, a port, an @, a + or an = is not linked at all, one with
    a comma or ! is cut there."""
    doc = QTextDocument()
    doc.setMarkdown(address, READ_FEATURES)
    block = doc.begin()
    runs = list(iter_block_runs(block))
    return (doc.blockCount() == 1 and block.text() == address and bool(runs)
            and all(fmt.isAnchor() and fmt.anchorHref() == href for _text, fmt in runs))


def _autolink(address: str, href: str, before: str, following: Optional[_Span],
              angled: bool = False) -> str:
    """A link that shows its own address, written so every reader links it.

    Bare, the way it was typed, where a reader finds it by itself: a web
    address or an email between spaces or at a line's start, with no
    character a reader would take for emphasis, and only one that Qt's
    reader takes back whole. Readers that look for a bare address (a
    picture or a video alone on its line becomes a player) find it there.
    Anywhere else it is written ``<address>``, which every reader keeps
    whole; and where the file wrote it so (``angled``).
    """
    is_email = "@" in address and "://" not in address
    findable = (address.lower().startswith(("https://", "http://", "www.")) or is_email)
    if (not angled and findable and not any(ch in _MARKUP_IN_ADDRESS for ch in address)
            and (not before or before[-1].isspace() or before[-1] in ("(", _SOFT))
            and _ends_a_bare_address(address, following)
            and _reads_back_whole(address, href)):
        return address
    return f"<{address}>" if is_email else f"<{href}>"


def _ends_a_bare_address(address: str, following: Optional[_Span]) -> bool:
    """Whether the text after a bare address leaves it whole: nothing, a
    space, or punctuation that readers leave out of an address."""
    if following is None:
        return True
    text = following.text
    if text[0] == _SOFT:
        return True             # the line's end
    if following.raw or following.href or following.bold or following.italic \
            or following.strike or following.code:
        return False
    if text[0].isspace():
        return True
    rest = text.lstrip(".,:;!?" + ("" if "(" in address or ")" in address else ")"))
    return len(rest) < len(text) and (not rest or rest[0].isspace() or rest[0] == _SOFT)


def _caption_start(spans: List[_Span]) -> Optional[int]:
    """Where the words in italic that end a paragraph start, when nothing
    but pictures and spaces comes before them: a caption, under a picture
    or on its line. Written as one italic stretch ("*The **old** jetty*"),
    it reads back as it was, and STANDUP reads it as a caption (italic
    words alone: "*The* ***old***" holds upright spaces). None for words
    in one style throughout, which are one stretch anyway, and for any
    other paragraph."""
    start = len(spans)
    while start > 0 and spans[start - 1].italic and not spans[start - 1].raw:
        start -= 1
    if len(spans) - start < 2 or not all(span.raw or not span.text.strip()
                                         for span in spans[:start]):
        return None
    return start


# Each link _inline_markdown wrote that shows its own address: the spans
# it took up (from, to) and whether it was written bare (bare_words).
_Seen = List[Tuple[int, int, bool]]


def _inline_markdown(spans: List[_Span], hard_break: str = "  \n",
                     seen: Optional[_Seen] = None) -> str:
    caption = _caption_start(spans)
    in_caption: Optional[_Seen] = [] if seen is not None else None
    if caption is not None:
        words = _inline_markdown([replace(span, italic=False) for span in spans[caption:]],
                                 hard_break, in_caption)
        spans = spans[:caption] + [_Span(text=_wrap(words, "*"), raw=True)]
    out = []
    i = 0
    while i < len(spans):
        span = spans[i]
        if span.raw and not span.href:
            out.append(span.text)
            i += 1
            continue
        if span.href:
            # One link around every neighbour that points at the same
            # place, a picture among them too.
            first = i
            group = []
            while i < len(spans) and spans[i].href == span.href:
                group.append(spans[i])
                i += 1
            following = spans[i] if i < len(spans) else None
            pictured = any(s.raw for s in group)
            if _is_nostr_reference(span.href) and not span.literal and not pictured:
                # Bare, for readers, in the styles all its words have; a
                # copy keeps the words it shows.
                bare = _styled([_Span(text=span.href, bold=all(s.bold for s in group),
                                      italic=all(s.italic for s in group),
                                      strike=all(s.strike for s in group))])
                out.append(_apart(bare, "".join(out), following))
                continue
            words = "".join(s.text for s in group).replace(LINE_SEPARATOR, " ").replace(
                _SOFT, " ")
            address = "" if pictured else _own_address(words.strip(), span.href)
            angled = bool(address) and all(s.angled for s in group)
            if angled and not span.code and len({_style(s) for s in group}) == 1:
                # In its angle brackets, as the file had it, in its emphasis.
                out.append(_styled([_Span(
                    text=_autolink(address, span.href, "", None, True),
                    bold=span.bold, italic=span.italic, strike=span.strike)]))
                continue
            if address and not any(s.bold or s.italic or s.strike for s in group):
                written = _autolink(address, span.href, "".join(out), following, angled)
                if seen is not None:
                    seen.append((first, i, written == address))
                out.append(written)
                continue
            label = "".join(_styled(run, label=True) for run in _style_runs(group))
            out.append(f"[{label.replace(LINE_SEPARATOR, ' ')}]({_destination(span.href)})")
            continue
        run = [span]
        i += 1
        while (i < len(spans) and not spans[i].raw and not spans[i].href
               and _style(spans[i]) == _style(span)):
            run.append(spans[i])
            i += 1
        out.append(_styled(run))
    if seen is not None and caption is not None:
        seen.extend((caption + start, caption + end, bare) for start, end, bare in in_caption)
    # A line break at the very end of a block breaks nothing, and Markdown
    # drops it; trailing spaces there mean nothing either. Nor does one at
    # its very start (Return before a line break leaves one there): written,
    # it was an item with spaces for words.
    text = "".join(out).rstrip(LINE_SEPARATOR + _SOFT + " ").lstrip(LINE_SEPARATOR + _SOFT)
    # A soft break is the line end it was; where a hard one is a space (a
    # heading), so is a soft one.
    soft = "\n" if hard_break.endswith("\n") else hard_break
    return text.replace(LINE_SEPARATOR, hard_break).replace(_SOFT, soft)


def bare_words(block) -> List[Tuple[int, int]]:
    """Where in ``block``'s text the writer writes words as they stand, so
    that a reader looking for a bare web address in the file looks there:
    plain words (not code, not text typed or escaped to stay text, not an
    address written ``<…>``) and links that show their own address,
    written bare. As (from, to) in the block's text; none in a photo
    group's paragraph or a table, which are not written block by block."""
    if (photo_group.block_segments(block) is not None
            or QTextCursor(block).currentTable() is not None):
        return []
    spans = _one_block(block, _image_as_read).spans
    text = block.text()
    shown = "".join(OBJECT_REPLACEMENT if span.raw else span.text.replace(_SOFT, LINE_SEPARATOR)
                    for span in spans)
    if not text.endswith(shown):
        return []                       # words the walk changed: none claimed
    seen: _Seen = []
    _inline_markdown(spans, seen=seen)
    bare = {index for first, last, written_bare in seen if written_bare
            for index in range(first, last)}
    out: List[Tuple[int, int]] = []
    at = len(text) - len(shown)
    for index, span in enumerate(spans):
        length = 1 if span.raw else len(span.text)
        if index in bare:
            out.append((at, at + length))
        elif not (span.raw or span.code or span.href or span.literal or span.escaped):
            start = 0
            for match in _protected(span.text):
                out.append((at + start, at + match.start()))
                start = match.end()
            out.append((at + start, at + length))
        at += length
    return [(start, end) for start, end in out if start < end]


def _style(span: _Span) -> Tuple[bool, bool, bool, bool]:
    """What a span's marks are: neighbours in one style are written inside
    them once, though one holds text a file escaped ("**a \\* b**", not
    "**a** **\\*** **b**")."""
    return span.bold, span.italic, span.strike, span.code


def _style_runs(spans: List[_Span]) -> List[List[_Span]]:
    """``spans`` cut where the style changes (_style), a picture a run of
    its own."""
    runs: List[List[_Span]] = []
    for span in spans:
        if runs and not span.raw and not runs[-1][0].raw \
                and _style(runs[-1][0]) == _style(span):
            runs[-1].append(span)
        else:
            runs.append([span])
    return runs


def _words(span: _Span, label: bool) -> str:
    """A span's text as Markdown, without its style: what the file escaped
    escaped as it was, literal text escaped, else web addresses kept text
    and, in a link's label, a bracket or a "<" (read as HTML) escaped,
    which would end or change the link when the file is read again; a
    "<" the file wrote bare there bare again (BARE_TEXT_PROPERTY: it is
    read as the words it is), elsewhere escaped; characters nobody sees
    as the entity the file wrote for them, while they are what it says."""
    if span.spelled:
        made = _entity_text(span.spelled)
        times = len(span.text) // len(made)
        if span.text == made * times:
            return span.spelled * times
    if span.bare:
        return span.text if label else span.text.replace("<", "\\<")
    if span.escaped:
        # (Only the punctuation: a word typed in its format stays words.)
        return "".join("\\" + char if char in ESCAPABLE else char for char in span.text)
    if span.literal:
        return LINE_SEPARATOR.join(literal_text(part) for part in span.text.split(LINE_SEPARATOR))
    words = LINE_SEPARATOR.join(_protect_addresses(part)
                                for part in span.text.split(LINE_SEPARATOR))
    return re.sub(r"([\[\]<])", r"\\\1", words) if label else words


def _styled(spans: List[_Span], label: bool = False) -> str:
    """Neighbouring ``spans`` in one style as Markdown (_style_runs)."""
    first = spans[0]
    if first.raw:
        return "".join(span.text for span in spans)
    if first.code:
        parts = "".join(span.text for span in spans).split(LINE_SEPARATOR)
        return LINE_SEPARATOR.join(code_span(p) for p in parts if p)
    pieces = []
    for text in "".join(_words(span, label) for span in spans).split(LINE_SEPARATOR):
        if first.strike:
            text = _wrap(text, "~~")
        if first.italic:
            text = _wrap(text, "*")
        if first.bold:
            text = _wrap(text, "**")
        pieces.append(text)
    return LINE_SEPARATOR.join(pieces)


# A pipe and the backslashes before it, when they escape none of it.
_BARE_PIPE = re.compile(r"(?<!\\)((?:\\\\)*)\|")


def _table_markdown(block: _Block) -> str:
    return pipe_table([[_inline_markdown(spans, hard_break=" ") for spans in row]
                       for row in block.rows])


def pipe_table(rows: Sequence[Sequence[str]]) -> str:
    """A table as Markdown (a pipe table), its first row the header:
    ``rows`` of cells, each a cell's words as Markdown on one line."""
    if not rows:
        return ""
    width = max(len(row) for row in rows)

    def cell(text: str) -> str:
        # Every pipe escaped that is not yet (one a file escaped is).
        return _BARE_PIPE.sub(r"\1\\|", text).replace("\n", " ").strip() or " "

    lines = []
    for index, row in enumerate(rows):
        cells = [cell(text) for text in row] + [" "] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def fenced_code(text: str, language: str = "") -> str:
    """``text`` as a fenced code block, its fence longer than any run of
    backticks in it."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text}\n{fence}"


def _markdown_lines(block: _Block) -> str:
    text = _block_markdown(block)
    if not block.quote:
        return text
    prefix = "> " * block.quote
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.split("\n"))


# A line a reader takes for a divider (a thematic break): three or more of
# one of its characters, and spaces.
_DIVIDER = re.compile(r"([-*_])(?:[ \t]*\1){2,}[ \t]*")


def _block_markdown(block: _Block) -> str:
    if block.kind == "heading":
        return "#" * min(6, block.level) + " " + _inline_markdown(block.spans, " ").strip()
    if block.kind == "rule":
        return "---"
    if block.kind == "code":
        return fenced_code(block.code_text, block.language)
    if block.kind == "table":
        return _table_markdown(block)
    if block.kind == "quote":
        prefix = "> " * block.level
        body = _inline_markdown(block.spans)
        return "\n".join(prefix + line if line else prefix.rstrip()
                         for line in body.split("\n"))
    if block.kind == "item":
        if block.ordered:
            marker = f"{block.number}{')' if block.other_marker else '.'}"
        else:
            marker = "*" if block.other_marker else "-"
        if block.task is not None:
            marker += " [x]" if block.task else " [ ]"
        pad = " " * (LIST_INDENT * (block.level - 1))
        body = _inline_markdown(block.spans)
        cont = " " * (len(pad) + len(marker) + 1)
        lines = body.split("\n")
        if _DIVIDER.fullmatch(lines[0]) or _DIVIDER.fullmatch(f"{marker} {lines[0]}"):
            # "- --" is a divider to a reader, not an item that says "--",
            # and "1. ***" an item that holds one: their first character
            # escaped, they are the item's words.
            lines[0] = re.sub(r"\S", lambda first: "\\" + first.group(0), lines[0], count=1)
        # (Nothing after the marker of an item with nothing in it; the
        # two spaces of a line break stay.)
        return "\n".join([f"{pad}{marker} {lines[0]}" if lines[0] else pad + marker]
                         + [cont + line for line in lines[1:]])
    # paragraph, as typed
    indent = block.indent
    if block.literal:
        # Spaces a reader would drop, or read as code, kept as spaces that
        # do not break. (Qt's reader drops a "&#32;".)
        indent = "&nbsp;" * len(indent)
    return indent + _inline_markdown(block.spans)


def document_to_markdown(doc, image_target: Optional[ImageTarget] = None, *,
                         literal: bool = False) -> str:
    """The document as Markdown, for a .md file, an article or a draft.
    ``literal``: its text is written to be read back as text (the
    clipboard), not as the Markdown typed into it."""
    blocks = document_blocks(doc, image_target)
    if literal:
        _make_literal(blocks)
    return _written(_laid_out(blocks))


def _laid_out(blocks: List[_Block]) -> List[Tuple[str, _Block]]:
    """The blocks that are written (all but empty lines), each with what
    goes between it and the one before (nothing before the first), the
    lists given their markers."""
    _set_list_markers(blocks)
    out: List[Tuple[str, _Block]] = []
    previous: Optional[_Block] = None
    for block in blocks:
        if block.kind != "empty":
            out.append((_separator(previous, block) if out else "", block))
        previous = block
    return out


def _written(laid_out: List[Tuple[str, _Block]]) -> str:
    text = "".join(between + _markdown_lines(block) for between, block in laid_out)
    return text.rstrip("\n") + ("\n" if laid_out else "")


def _shape(laid_out: List[Tuple[str, _Block]]) -> list:
    """What of its blocks the writer goes by besides their words: what
    each is, how it is marked and what parts it from the one before. Two
    readings the writer would write differently differ in it, though they
    show the same (holds_faithfully)."""
    return [(between, block.kind, block.level, block.ordered, block.number, block.task,
             block.other_marker, block.language, block.quote)
            for between, block in laid_out]


def _make_literal(blocks: List[_Block]) -> None:
    for block in blocks:
        block.literal = True
        for span in block.spans:
            span.literal = True
        for row in block.rows:
            for cell in row:
                for span in cell:
                    span.literal = True


def _new_list(previous: _Block, block: _Block) -> bool:
    return previous.group != block.group and previous.level == block.level


def _set_list_markers(blocks: List[_Block]) -> None:
    """Give a list that follows another of its kind at its depth, with only
    empty lines between, the other marker ("*" for "-", ")" for "."): with
    the same one a reader takes the two for one list ("- a\n\n- b")."""
    last_at: dict = {}                      # depth: the last item written at it
    for block in blocks:
        if block.kind == "empty":
            continue
        if block.kind != "item" or block.apart:
            last_at.clear()                 # (a paragraph, a heading, a quote's end ends a list)
        if block.kind != "item":
            continue
        for level in [level for level in last_at if level > block.level]:
            del last_at[level]
        last = last_at.get(block.level)
        if last is not None and last.ordered == block.ordered and last.quote == block.quote:
            block.other_marker = last.other_marker != (last.group != block.group)
        last_at[block.level] = block


def _says_nothing(item: _Block) -> bool:
    """Whether a list item has nothing in it, not even a task's box."""
    return item.task is None and not _inline_markdown(item.spans)


def _quote_depth(block: _Block) -> int:
    return block.level if block.kind == "quote" else block.quote


def _separator(previous: _Block, block: _Block) -> str:
    """What goes between two written blocks: a line break, or a blank line."""
    if previous.kind == "empty":
        return "\n\n"
    if block.apart is not None:
        # The blank line the file had there, in the quotes the two stay in
        # (those they stand in now, but the ones that ended there): it ends
        # a code block or a quote, or sets a list's items apart.
        depth = max(0, min(_quote_depth(previous), _quote_depth(block)) - block.apart)
        return "\n" + ("> " * depth).rstrip() + "\n"
    if previous.kind == "item" and block.kind == "item":
        # A different list at the same depth is a list of its own, by its
        # other marker (_set_list_markers): a blank line would end a quote
        # around both, and spread out the item around both.
        if _new_list(previous, block):
            return "\n" if previous.quote == block.quote else "\n\n"
        if block.level > previous.level and _says_nothing(block) and not _says_nothing(previous):
            # An empty item cannot follow words of a paragraph: right under
            # them it would join them ("- a\n    -" is a heading "a"). A
            # blank line ends them first, in the quote the items are in.
            return "\n" + ("> " * block.quote).rstrip() + "\n"
        return "\n"
    depth = min(_quote_depth(previous), _quote_depth(block))
    if depth:
        # Two parts of one quote stay in it.
        return "\n" + ("> " * depth).rstrip() + "\n"
    # Lines typed one under the other become paragraphs of their own: a
    # blank line is the one break every Nostr reader shows the same way
    # (NIP-23 asks for no hard line breaks inside a paragraph).
    return "\n\n"


# --------------------------------------------------------------------------- #
# Plain text for a short note                                                  #
# --------------------------------------------------------------------------- #

def _inline_note(spans: List[_Span]) -> str:
    out = []
    i = 0
    while i < len(spans):
        span = spans[i]
        if span.href and not span.raw:
            group = []
            while i < len(spans) and spans[i].href == span.href and not spans[i].raw:
                group.append(spans[i].text)
                i += 1
            label = "".join(group).replace(LINE_SEPARATOR, " ").replace(_SOFT, " ").strip()
            href = span.href
            if _is_nostr_reference(href):
                following = spans[i] if i < len(spans) else None
                out.append(_apart(href, "".join(out), following))
            elif not label or label == href or label.rstrip("/") == href.rstrip("/"):
                out.append(href)
            else:
                out.append(f"{label} ({href})")
            continue
        out.append(span.text)
        i += 1
    return "".join(out).replace(LINE_SEPARATOR, "\n").replace(_SOFT, "\n")


def _note_lines(block: _Block) -> str:
    if block.kind == "rule":
        return "---"
    if block.kind == "code":
        return block.code_text
    if block.kind == "table":
        return "\n".join(" | ".join(_inline_note(spans).strip() for spans in row)
                         for row in block.rows)
    if block.kind == "quote":
        return "\n".join("> " * block.level + line
                         for line in _inline_note(block.spans).split("\n"))
    if block.kind == "item":
        if block.bullet_text:
            # Typed bullets stay exactly as typed, the way they look.
            return block.bullet_text.split(BULLET_MARKER, 1)[0] + BULLET_MARKER + \
                _inline_note(block.spans)
        marker = f"{block.number}." if block.ordered else BULLET_MARKER.strip()
        if block.task is not None:
            marker = ("[x]" if block.task else "[ ]") if not block.ordered else marker
        pad = "  " * (block.level - 1)
        return f"{pad}{marker} {_inline_note(block.spans)}"
    if block.kind == "heading":
        return _inline_note(block.spans).strip()
    return block.indent + _inline_note(block.spans)


def document_to_note_text(doc, image_target: Optional[ImageTarget] = None) -> str:
    """The document as plain text for a short note: what it says, with list
    markers and link addresses, and no Markdown markup. Empty lines stay
    exactly as typed; paragraphs that came from Markdown or HTML keep the
    blank line between them."""
    lines: List[str] = []
    previous: Optional[_Block] = None
    for block in document_blocks(doc, image_target):
        if block.kind == "empty":
            lines.append("")
        else:
            if (previous is not None and previous.kind != "empty" and previous.spaced
                    and block.spaced
                    and not (previous.kind == "item" and block.kind == "item"
                             and not _new_list(previous, block))):
                lines.append("")
            lines.append(_note_lines(block))
        previous = block
    return "\n".join(lines).strip("\n")


def has_local_only_formatting(doc) -> bool:
    """Whether the document holds what Markdown cannot carry: underline,
    text color or highlight. A link's own color and underline are how it
    looks, not formatting, so links do not count."""
    return any(_local_only(fmt) for block in iter_blocks(doc)
               for _text, fmt in iter_block_runs(block))


def remove_local_only_formatting(doc) -> None:
    """Take what has_local_only_formatting finds off ``doc``: a document
    saved as Markdown without it then shows what the file holds."""
    found = []
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and _local_only(fragment.charFormat()):
                found.append((fragment.position(), fragment.length(), fragment.charFormat()))
            it += 1
    cursor = QTextCursor(doc)
    for position, length, fmt in found:
        plain = QTextCharFormat(fmt)
        for words in _LOCAL_ONLY:
            plain.clearProperty(words)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(plain)


# What of a format Markdown cannot carry (has_local_only_formatting).
_LOCAL_ONLY = (QTextFormat.Property.FontUnderline, QTextFormat.Property.TextUnderlineStyle,
               QTextFormat.Property.ForegroundBrush, QTextFormat.Property.BackgroundBrush)


def _local_only(fmt) -> bool:
    """Whether a piece in ``fmt`` holds what Markdown cannot carry
    (has_local_only_formatting); a picture and a link never."""
    if fmt.isImageFormat() or fmt.isAnchor():
        return False
    return (fmt.fontUnderline() or fmt.hasProperty(QTextFormat.Property.ForegroundBrush)
            or fmt.hasProperty(QTextFormat.Property.BackgroundBrush))


# Markdown the editor's document cannot hold, so reading it in would lose
# something: a footnote whose note goes on in a second paragraph (the
# editor keeps footnotes as their text, one paragraph each), a tab where a
# line starts (lists and code are indented with spaces), code inside a
# list item (it moves out of the item), an escaped footnote mark, raw
# HTML, a paragraph continued inside a list
# item, and lines that wrap inside a paragraph (a draft saved as plain
# text by an earlier version). Also (below): a tab inside fenced code, a
# column's alignment, emphasis in a table's header
# row, a link title (markdown_scan.titled_link), a link reference
# definition in a quote or a list item or with a title
# (markdown_scan.lossy_definition), emphasis around nothing but code
# (markdown_scan.emphasized_code), blocks Qt's reader reads otherwise
# than they are (markdown_scan.misread_blocks: a heading or a divider
# inside a list item or a quote, a list set in on the line that opens a
# quote); these four outside code.
_LOSSY = [
    re.compile(r"^ {0,3}\[\^[^\]]+\]:.*\n(?:[ \t]*\n)+(?: {4}|\t)\S",
               re.MULTILINE),                              # a note of two paragraphs
    re.compile(r"^\t", re.MULTILINE),                     # a tab where a line starts
    re.compile(r"^ {2,}(?:```|~~~)", re.MULTILINE),        # code inside a list item
    re.compile(r"\\\[\^"),                                 # an escaped footnote mark
    # raw HTML (also _raw_html): a "<" after "](" that opens no whole address
    re.compile(r"\]\(<(?![^<>\n]*>\))"),
    # The line and the paragraph separator, which the document holds as
    # its own line break and paragraph end.
    re.compile("[\u2028\u2029]"),
]
# Raw HTML: a "<" that opens a tag or a comment, not an escaped "\<", nor
# a link's whole address in <…> (one with a space).
_RAW_HTML = re.compile(r"(?<!\\)(?<!\]\()<(?:[A-Za-z][\w-]*[\s/>]|/[A-Za-z]|!--)")


def _raw_html(markdown: str) -> bool:
    """Whether ``markdown`` holds raw HTML Qt's reader would drop: any but
    a tag in a link's words the editor keeps as its words (_label_tags)."""
    found = _RAW_HTML.search(markdown)
    if found is None:
        return False
    kept = {start for start, _end in _label_tags(markdown)}
    return any(tag.start() not in kept for tag in _RAW_HTML.finditer(markdown, found.start()))


_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|>|#|\||```|~~~|    |\t)")


def _soft_breaks(markdown: str) -> Tuple[bool, List[int]]:
    """Where lines of one paragraph follow each other (soft line breaks):
    ``(lossy, lines)``, ``lines`` the numbers of the lines a soft break
    ends. ``lossy`` when a break is one the editor cannot hold: inside an
    indented paragraph or a list item, or a list
    item continued after a blank line (Qt joins the first and moves the
    second out of the list), or where a line may be a table's."""
    lines = markdown.split("\n")
    # Lines in fenced code, the fences among them: code keeps its lines.
    coded, fence = [], ""
    for line in lines:
        found_fence = FENCE_LINE.match(line)
        if fence:
            coded.append(True)
            if found_fence and found_fence.group(1)[0] == fence[0] \
                    and len(found_fence.group(1)) >= len(fence) \
                    and not found_fence.group(2).strip():
                fence = ""
        elif found_fence:
            coded.append(True)
            fence = found_fence.group(1)
        else:
            coded.append(False)
    found: List[int] = []
    for index in range(1, len(lines)):
        line, before = lines[index], lines[index - 1]
        if coded[index] or coded[index - 1]:
            continue
        if (line.strip() and before.strip() and not _BLOCK_START.match(line)
                and not _BLOCK_START.match(before) and not ends_in_break(before)
                and not set(line.strip()) <= set("=-")
                and not (set(before.strip()) <= set("=-") and index >= 2
                         and lines[index - 2].strip())):
            # (No paragraph goes on under a heading's underline: one with
            # words above it. A pipe may be a table without its outer pipes.)
            if (line[:1] in (" ", "\t") or before[:1] in (" ", "\t")
                    or "|" in line or "|" in before):
                return True, []
            found.append(index - 1)
        if (line.startswith(("  ", "\t")) and line.strip() and not before.strip()
                and index >= 2 and LIST_LINE.match(lines[index - 2])
                and not LIST_LINE.match(line) and not EMPTY_ITEM_LINE.match(line)):
            # (Not an item of a list inside it, which a blank line sets
            # apart, with nothing in it either: the writer puts that
            # blank line before one right under an item's words, which it
            # would join.)
            return True, []
    if found and _free_mark(markdown, _SOFT_MARKS) is None:
        return True, []
    return False, found


def _soft_wrapped(markdown: str) -> bool:
    """Whether the Markdown has soft line breaks the editor cannot hold
    (see _soft_breaks)."""
    return _soft_breaks(markdown)[0]


def _marked_soft_breaks(markdown: str) -> Tuple[str, Optional[str], int]:
    """The Markdown with each soft line break the editor holds made a hard
    break with a mark (_SOFT_MARKS) before it, the mark, and how many
    there are."""
    lossy, found = _soft_breaks(markdown)
    if lossy or not found:
        return markdown, None, 0
    mark = _free_mark(markdown, _SOFT_MARKS)
    lines = markdown.split("\n")
    for index in found:
        lines[index] = lines[index] + mark + "  "
    return "\n".join(lines), mark, len(found)


def _join_soft_breaks(doc: QTextDocument, mark: str) -> int:
    """Each marked hard break Qt read (a block ending in ``mark``) made
    one line of the paragraph again: the mark and the block's end become
    a line separator carrying SOFT_BREAK_PROPERTY. How many were joined."""
    joined = 0
    with _one_step(doc) as cursor:
        # (Last first: a place before a change stays where it is.)
        for start in reversed(_mark_places(doc, mark)):
            block = doc.findBlock(start)
            cursor.setPosition(start + 1)
            if start + 1 == block.position() + block.length() - 1 and block.next().isValid():
                # The mark and the paragraph's end, in the next block's
                # place, in the mark's own format: the words before it may
                # be a link (an address that ends the line), code or
                # emphasis, which the break would join to the next line's;
                # after a picture (a caption on the line under it), in
                # none: the format of the picture would make the break a
                # second copy of it.
                fmt = QTextCharFormat(cursor.charFormat())
                if fmt.isImageFormat():
                    fmt = QTextCharFormat()
                cursor.setPosition(start)
                cursor.setPosition(start + 2, QTextCursor.MoveMode.KeepAnchor)
                fmt.setProperty(SOFT_BREAK_PROPERTY, True)
                cursor.insertText(LINE_SEPARATOR, fmt)
                joined += 1
            else:
                cursor.setPosition(start, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
    return joined


# What marks a hard line break (two spaces or more at a line's end, or a
# backslash) while Qt reads the Markdown. Its reader makes the line after one a paragraph
# of its own, and "first  \nsecond", which the writer writes for a line
# break typed here, came back as two paragraphs. With a mark after its
# spaces the line ends in a soft break, which Qt's reader reads as a
# space where the paragraph goes on and not at all where it ends; once
# read, the spaces (or the backslash), the mark and that space become the
# line break (_keep_hard_breaks). A space (Unicode's Zs), as _SOFT_MARKS are; a file
# is read with the first one it holds nowhere.
_HARD_MARKS = ("\u2003", "\u2001", "\u2000", "\u205f")


def _marked_hard_breaks(markdown: str, breaks: Sequence[int]) -> Tuple[str, Optional[str]]:
    """The Markdown with a mark (_HARD_MARKS) at the end of each of the
    lines ``breaks`` (markdown_scan.hard_breaks), and the mark; as it is
    and None when there is none, or no mark can be used."""
    mark = _free_mark(markdown, _HARD_MARKS) if breaks else None
    if mark is None:
        return markdown, None
    lines = markdown.split("\n")
    for number in breaks:
        lines[number] += mark
    return "\n".join(lines), mark


def _keep_hard_breaks(doc: QTextDocument, mark: str, placed: int) -> bool:
    """Each ``mark`` _marked_hard_breaks set taken out again, with the
    spaces before it: where the paragraph goes on after it (Qt's reader
    put a space there), the line break the file has, in the mark's format,
    in place of a backslash too (which is the paragraph's last word
    where it ends); in code, the spaces stay the code's. False when the marks in the text
    are not as many as ``placed``, or a format holds one."""
    taken = _take_marks(doc, mark)
    if len(taken) != placed or _in_formats(doc, mark):
        return False
    with _one_step(doc) as cursor:
        for number in reversed(range(len(taken))):
            position, fmt = taken[number]
            block = doc.findBlock(position)
            if _is_code(fmt) or block.blockFormat().hasProperty(
                    QTextFormat.Property.BlockCodeLanguage):
                continue
            # (Up to the paragraph's start, or the mark before this one.)
            first = max(block.position(), taken[number - 1][0] if number else 0)
            start = position
            while start > first and doc.characterAt(start - 1) == " ":
                start -= 1
            goes_on = start > block.position() and doc.characterAt(position) == " "
            if goes_on and start == position and doc.characterAt(start - 1) == "\\":
                start -= 1
                while start > first and doc.characterAt(start - 1) == " ":
                    start -= 1
            cursor.setPosition(start)
            cursor.setPosition(position + goes_on, QTextCursor.MoveMode.KeepAnchor)
            if goes_on:
                cursor.insertText(LINE_SEPARATOR,
                                  QTextCharFormat() if fmt.isImageFormat() else fmt)
            else:
                cursor.removeSelectedText()
    return True


def _fingerprint(doc) -> bytes:
    """What a document shows: each block's kind and text, and each piece's
    formatting, so two renderings can be compared; as a digest, so a long
    document is not held twice while it is compared."""
    prints = hashlib.blake2b(digest_size=32)
    for block in iter_blocks(doc):
        fmt = block.blockFormat()
        text_list = block.textList()
        prints.update(repr((
            fmt.headingLevel(),
            (text_list.format().style() in _ORDERED_STYLES) if text_list else None,
            text_list.format().indent() if text_list else 0,
            fmt.property(QTextFormat.Property.BlockQuoteLevel),
            bool(fmt.property(QTextFormat.Property.BlockCodeFence)
                 or fmt.hasProperty(QTextFormat.Property.BlockCodeLanguage)),
            tuple((text, _is_bold(f), f.fontItalic(), f.fontStrikeOut(), _is_code(f),
                   f.anchorHref() if f.isAnchor() else "",
                   (f.toImageFormat().name(),
                    str(f.property(QTextFormat.Property.ImageAltText) or ""))
                   if f.isImageFormat() else "")
                  for text, f in iter_block_runs(block)),
        )).encode("utf-8", "surrogatepass"))
    return prints.digest()


# A footnote definition as typed ("[^1]: Ibid.") at a line's start, maybe
# inside a quote (outside code, found by markdown_scan.FENCE_LINE).
_FOOTNOTE_DEFINITION = re.compile(r"^((?: {0,3}>)* {0,3})\[(?=\^[^\]\s]+\]:)")


def literal_footnotes(markdown: str) -> str:
    """``markdown`` with every footnote definition outside code written
    ``\\[^1]:``, so a reader takes it for the text it is.

    Qt's reader takes a definition whose text is one word or a web address
    for a link reference definition: the line disappears and the mark
    that refers to it becomes a link. Footnotes are kept as typed text in
    the editor (D5), so they must read back as text."""
    out = []
    fence = ""
    for line in markdown.split("\n"):
        found = FENCE_LINE.match(line)
        if fence:
            if (found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence)
                    and not found.group(2).strip()):
                fence = ""
        elif found:
            fence = found.group(1)
        else:
            line = _FOOTNOTE_DEFINITION.sub(r"\1\\[", line)
        out.append(line)
    return "\n".join(out)


def read_markdown(doc: QTextDocument, markdown: str, features=READ_FEATURES) -> None:
    """Read Markdown into ``doc`` the way the editor holds it: the one way
    the app reads Markdown it wrote or accepted (an opened file or draft,
    the publish preview, a paste), so what was typed comes back as typed.
    Replaces what ``doc`` held."""
    _read_counting(doc, markdown, features)


def _read_counting(doc: QTextDocument, markdown: str, features=READ_FEATURES, *,
                   groups: bool = True) -> bool:
    """read_markdown, saying whether the editor holds all the reading
    marked: every soft line break the Markdown has that the editor holds
    read as one, all Qt's reader would leave out kept (_marked_keeps), and
    each entity a character in its line (_keep_entities).
    The editor's reading (READ_FEATURES) keeps soft line breaks; a preview
    or a paste reads them as Markdown readers do, as spaces. ``groups``:
    pictures that follow each other are folded into their group, as the
    editor shows them; without, they stay the paragraphs they are in the
    Markdown."""
    markdown = _breaks_between_pictures(markdown)
    markdown, alts = _whole_descriptions(markdown, features)
    breaks = hard_breaks(markdown)
    marked, soft_mark = 0, None
    if features == READ_FEATURES:
        markdown, soft_mark, marked = _marked_soft_breaks(markdown)

    def read(text: str) -> Tuple[int, bool]:
        escaped, mark = _marked_escapes(text)
        result = _read_once(doc, escaped, features, alts, (soft_mark, marked))
        if mark and not _keep_escaped(doc, mark, escaped.count(mark)):
            # A mark Qt's reader put outside the text (a link's address, a
            # picture's file, a title), where nothing takes it out: read
            # without marks, the escapes then are saved as the writer writes
            # text, and holds_faithfully opens the file as its text when
            # that would change it.
            result = _read_once(doc, text, features, alts, (soft_mark, marked))
        return result

    def read_entities(text: str) -> Tuple[int, bool]:
        held, entity_mark, spelled = _marked_entities(text)
        joined, kept = read(held)
        if entity_mark is not None:
            placed, faithful = _keep_entities(doc, entity_mark, spelled)
            if placed:
                kept = kept and faithful
            else:
                # The same: read with the entities, the words after one as
                # Qt's reader formats them.
                joined, kept = read(text)
        # (Entities, and no mark free for them: read so too, and not held.)
        return joined, kept and (entity_mark is not None or not spelled)

    broken, hard_mark = _marked_hard_breaks(markdown, breaks)
    joined, kept = read_entities(broken)
    if breaks and (hard_mark is None or not _keep_hard_breaks(doc, hard_mark, len(breaks))):
        # The same, and where no mark is free: read with the hard breaks as
        # Qt's reader takes them, the line after each a paragraph of its
        # own.
        if hard_mark is not None:
            joined, _kept = read_entities(markdown)
        kept = False
    normalize_after_markdown_load(doc)
    if groups:
        # Pictures that follow each other show as one group (photo_group).
        photo_group_find.fold(doc)
    return joined == marked and kept


def _read_once(doc: QTextDocument, markdown: str, features, alts: dict,
               soft: Tuple[Optional[str], int]) -> Tuple[int, bool]:
    """Qt's reading of the marked ``markdown`` into ``doc``, what it would
    leave out kept (_marked_keeps), the pictures given their descriptions
    (``alts``) and the ``soft`` breaks (their mark, how many) joined; how
    many were joined, and whether all was kept."""
    markdown = literal_footnotes(markdown)
    keeping, keep_mark, kinds, lost = _marked_keeps(markdown, features)
    keeping, apart_mark_, ended, runs_on = _marked_apart(keeping)
    keeping, info_mark, cut = _whole_infos(keeping)
    doc.setMarkdown(keeping, features)
    if info_mark is not None:
        _split_infos(doc, info_mark)
    if not ((apart_mark_ is None or _set_apart(doc, apart_mark_, ended))
            and (keep_mark is None or _kept_by_marks(doc, keep_mark, kinds))):
        # Read otherwise than the marks expected: read without them, and
        # what they kept is left out.
        doc.setMarkdown(markdown, features)
        lost = True
    lost = lost or runs_on or cut
    if alts:
        _restore_descriptions(doc, alts)
    soft_mark, marked = soft
    return (_join_soft_breaks(doc, soft_mark) if marked else 0), not lost


# --------------------------------------------------------------------------- #
# What a file escaped, saved escaped                                           #
# --------------------------------------------------------------------------- #
#
# Qt's reader reads "\*Stern\*" as the text "*Stern*", and the writer
# writes text as the Markdown typed into it: saved, the words would turn
# into emphasis, "1\. Platz" into a list, "\![a](u)" into a picture.
# Each character a file escaped is marked (ESCAPED_TEXT_PROPERTY), and the
# writer escapes it again where the file did: what is bare in the file
# stays bare, so the file saves as it was.
# For the time Qt reads the file, a mark stands before each escape: a
# punctuation mark as the backslash is, so the emphasis beside it reads
# as before (whether a star opens or closes emphasis depends on what
# stands beside it; a mark from the private use area is no punctuation,
# and "*(a)*\*b" lost its emphasis). Each is a Po character no HTML
# entity names; a file is read with the first one it holds nowhere
# (_free_mark: Qt's reader makes "&#x2E2C;" the character), and without
# marks when it holds all of them.
_ESCAPE_MARKS = ("\u2e2c", "\u2e2d", "\u2e2b", "\u2e2e", "\u2e30")
_NUMERIC_REFERENCE = re.compile(r"&#(?:[xX]([0-9a-fA-F]{1,6})|([0-9]{1,7}));")


def _free_mark(markdown: str, marks: Sequence[str]) -> Optional[str]:
    """The first of ``marks`` ``markdown`` holds nowhere, as a character or
    as a numeric reference, or None: a mark of this reading that Qt's
    reader could also make of the file would be taken for one."""
    held = ({int(hexadecimal, 16) if hexadecimal else int(decimal)
             for hexadecimal, decimal in _NUMERIC_REFERENCE.findall(markdown)}
            if "&#" in markdown else set())
    return next((mark for mark in marks if mark not in markdown and ord(mark) not in held), None)


def _marked_escapes(markdown: str) -> Tuple[str, Optional[str]]:
    """The Markdown with a mark (_ESCAPE_MARKS) before each escape a reader
    takes for its character (markdown_scan.escapes), and the mark; as it
    was and None when it has no escape or no mark can be used."""
    if "\\" not in markdown:
        return markdown, None
    found = escapes(markdown)
    mark = _free_mark(markdown, _ESCAPE_MARKS) if found else None
    if mark is None:
        return markdown, None
    return _with_marks(markdown, [(at, "", "") for at in found], mark), mark


def _with_marks(text: str, places: Sequence[Tuple[int, str, str]], mark: str) -> str:
    """``text`` with ``mark`` at each of ``places``, as (where, what goes
    before the mark, what goes after it), in rising order."""
    pieces, last = [], 0
    for at, before, after in places:
        pieces += [text[last:at], before, mark, after]
        last = at
    pieces.append(text[last:])
    return "".join(pieces)


@contextmanager
def _one_step(doc: QTextDocument):
    """What is changed in ``doc`` meanwhile, as one change (the cursor to
    make it with). A change by itself costs as much as its paragraph is
    long, and a mark for each of a long paragraph's thousand line breaks
    took the square of it: a file of 130 KB opened in 40 seconds."""
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    try:
        yield cursor
    finally:
        cursor.endEditBlock()


def _mark_places(doc: QTextDocument, mark: str) -> List[int]:
    """Where each ``mark`` stands in ``doc``'s text, first to last; read
    off each paragraph's text once."""
    places: List[int] = []
    for block in iter_blocks(doc):
        text = block.text()
        at, last, units = text.find(mark), 0, block.position()
        while at >= 0:
            units += doc_length(text[last:at])
            places.append(units)
            at, last = text.find(mark, at + 1), at
    return places


def _take_marks(doc: QTextDocument, mark: str) -> List[Tuple[int, QTextCharFormat]]:
    """Each ``mark`` in ``doc``'s text taken out, first to last, as where
    it stood (once all are out) and the format it had."""
    places = _mark_places(doc, mark)
    formats = []
    with _one_step(doc) as cursor:
        for position in places:
            cursor.setPosition(position + 1)
            formats.append(QTextCharFormat(cursor.charFormat()))
        for position in reversed(places):
            cursor.setPosition(position)
            cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
    return [(position - before, fmt)
            for before, (position, fmt) in enumerate(zip(places, formats))]


def _keep_escaped(doc: QTextDocument, mark: str, placed: int) -> bool:
    """Each ``mark`` _marked_escapes set gone from the text, and the
    character after it marked escaped. False when fewer than the
    ``placed`` marks were in the text, or a format holds one (a link's
    address, a picture's file or description, a title): it is then in
    what is saved, copied, exported and published."""
    text = QTextCharFormat()
    text.setProperty(ESCAPED_TEXT_PROPERTY, True)
    taken = _take_marks(doc, mark)
    with _one_step(doc) as cursor:
        for position, _fmt in taken:
            if doc.characterAt(position) in ESCAPABLE:
                cursor.setPosition(position)
                cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(text)
    return len(taken) == placed and not _in_formats(doc, mark)


# --------------------------------------------------------------------------- #
# An entity, read in the format of the words around it                         #
# --------------------------------------------------------------------------- #
#
# Qt's reader reads an entity ("&amp;") as its character, but inserts it as
# HTML, which sets the format back: the words after it, to where their
# emphasis or link ends, lose it ("[x &amp; y](u)" was the link "x " and
# the words "& y", saved so), and a space it makes is dropped ("a&#32;b"
# was "ab"). For the time Qt reads the Markdown, a mark stands in each
# entity's place (markdown_scan.entities): a Po character, punctuation as
# the "&" is, so the emphasis beside it reads as before; once read, the
# mark becomes what CommonMark reads the entity as (_entity_text), in the
# format the mark had, and a punctuation mark so made is marked escaped,
# as the file did not write it bare ("&lt;" stays words), characters
# nobody sees with the entity (doc_walk.ENTITY_TEXT_PROPERTY). A file is
# read with the first mark it holds nowhere (_free_mark).
_ENTITY_MARKS = ("\u2e35", "\u2e36", "\u2e37", "\u2e38")
# What an entity can make that the editor holds as a new paragraph or a
# line break, not as a character in the line ("&#10;").
_LINE_ENDS = ("\n", "\r", "\u2028", "\u2029")


def _marked_entities(markdown: str) -> Tuple[str, Optional[str], List[str]]:
    """The Markdown with a mark (_ENTITY_MARKS) in place of each entity
    (in a reference definition's label too, which must match its links'),
    the mark, and the entities shown, in their order; as it was and None
    when it has no entity, or no mark can be used (the entities are told
    then)."""
    if "&" not in markdown:
        return markdown, None, []
    found = entities(markdown)
    mark = _free_mark(markdown, _ENTITY_MARKS) if found else None
    if mark is None:
        return markdown, None, [markdown[start:end] for start, end, _shown in found]
    pieces, last = [], 0
    for start, end, _shown in found:
        pieces += [markdown[last:start], mark]
        last = end
    pieces.append(markdown[last:])
    return ("".join(pieces), mark,
            [markdown[start:end] for start, end, shown in found if shown])


def _entity_text(entity: str) -> str:
    """What CommonMark reads ``entity`` (as markdown_scan.entities finds
    one) as: a name HTML5 has its character(s), one it has not the words
    as written; a number its character, and U+FFFD for 0 or a number that
    is no character."""
    if entity[1] != "#":
        return html.entities.html5.get(entity[1:], entity)
    code = int(entity[3:-1], 16) if entity[2] in "xX" else int(entity[2:-1])
    if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
        return "\ufffd"
    return chr(code)


def _unseen(text: str) -> bool:
    """Whether ``text`` (what an entity makes) is only characters nobody
    sees: spaces of any width, a tab, a zero-width space, a soft hyphen,
    a direction mark. A reader may drop them where a line starts, and in
    the file they would not be seen."""
    return all(char.isspace() or unicodedata.category(char) in ("Cc", "Cf") for char in text)


def _keep_entities(doc: QTextDocument, mark: str, spelled: Sequence[str]) -> Tuple[bool, bool]:
    """Each ``mark`` _marked_entities set made what its entity says
    (``spelled``, in their order), in the mark's format: whether the marks
    stood where expected (False when they are not as many in the text, or
    a format holds one, as for _keep_escaped), and whether each is a
    character in its line (_LINE_ENDS: one that is not is a space)."""
    taken = _take_marks(doc, mark)
    if len(taken) != len(spelled) or _in_formats(doc, mark):
        return False, False
    in_line = True
    with _one_step(doc) as cursor:
        for (position, fmt), entity in zip(reversed(taken), reversed(spelled)):
            text = _entity_text(entity)
            character = QTextCharFormat(fmt)
            if any(end in text for end in _LINE_ENDS):
                in_line, text = False, " "
            elif _unseen(text):
                character.setProperty(ENTITY_TEXT_PROPERTY, entity)
            if text in ESCAPABLE:
                character.setProperty(ESCAPED_TEXT_PROPERTY, True)
            cursor.setPosition(position)
            cursor.insertText(text, character)
    return True, in_line


# --------------------------------------------------------------------------- #
# A fence's words, kept whole                                                  #
# --------------------------------------------------------------------------- #
#
# Qt's reader keeps the first word of what a fence says as the code
# block's language, and drops the rest: "```{r setup, include=FALSE}" was
# saved as "```{r", "```js title=x" as "```js". For the time it reads the
# Markdown, a mark stands in each space of such a fence's words, so they
# are one word; once read, the first of them is the block's language
# again and the rest is kept beside it (doc_walk.CODE_INFO_PROPERTY). The
# mark is one of ASCII's separators: Qt's reader takes a fence's words
# for Latin-1, and gives any other character back as two or three.
_INFO_MARKS = ("\x1f", "\x1e", "\x1d", "\x1c")
_SPACED_INFO = re.compile(r"^[ >]*(?:`{3,}|~{3,})[ \t]*\S+[ \t]+\S", re.MULTILINE)


def _whole_infos(markdown: str) -> Tuple[str, Optional[str], bool]:
    """The Markdown with a mark (_INFO_MARKS) in each space of a fence's
    words, where it says more than its language, and the mark; and
    whether such words are cut all the same: no mark is free, or a tab
    parts them."""
    if _SPACED_INFO.search(markdown) is None:
        return markdown, None, False
    lines = markdown.split("\n")
    spaced = [(number, words) for number, words in fence_words(lines) if len(words.split()) > 1]
    mark = _free_mark(markdown, _INFO_MARKS) if spaced else None
    if mark is None:
        return markdown, None, bool(spaced)
    for number, words in spaced:
        lines[number] = lines[number][:len(lines[number].rstrip()) - len(words)] \
            + words.replace(" ", mark)
    return "\n".join(lines), mark, any("\t" in words for _number, words in spaced)


def _split_infos(doc: QTextDocument, mark: str) -> None:
    """Each code block's language that holds ``mark`` (_whole_infos) made
    its language and the words after it again."""
    language = QTextFormat.Property.BlockCodeLanguage
    with _one_step(doc) as cursor:
        for block in iter_blocks(doc):
            fmt = block.blockFormat()
            words = fmt.stringProperty(language)
            if mark in words:
                first, _mark, rest = words.partition(mark)
                fmt.setProperty(language, first)
                fmt.setProperty(CODE_INFO_PROPERTY, rest.replace(mark, " "))
                cursor.setPosition(block.position())
                cursor.setBlockFormat(fmt)


# --------------------------------------------------------------------------- #
# Blocks the file set apart, kept apart                                        #
# --------------------------------------------------------------------------- #
#
# Qt's reader runs a block into the one before it where the document has
# nothing to tell the two by: two code blocks become one, a quote after
# another's end goes on in it, and a list whose items stand apart by
# blank lines (a loose one) reads as one whose items do not. For the time
# Qt reads the Markdown, a mark stands where the words or the code of each
# such block start (markdown_scan.set_apart); once read, its block says
# that it stands apart (doc_walk.APART_PROPERTY), and the writer writes
# the blank line between the two again. The mark is a space (Unicode's
# Zs), so what follows it reads as at a line's start (an address is a
# link still); a file is read with the first one it holds nowhere.
_APART_MARKS = ("\u2009", "\u200a", "\u2007", "\u2002")


def _marked_apart(markdown: str) -> Tuple[str, Optional[str], List[int], bool]:
    """The Markdown with a mark (_APART_MARKS) at the start of each block
    Qt's reader would run into the one before it, the mark (None for
    none), for each how many quotes end between it and that one, and
    whether a block runs on all the same: one with no words to mark, or
    no free mark."""
    places, runs_on = set_apart(markdown)
    mark = _free_mark(markdown, _APART_MARKS) if places else None
    if mark is None:
        return markdown, None, [], runs_on or bool(places)
    marked = _with_marks(markdown, [(at, "", "") for at, _ended in places], mark)
    return marked, mark, [ended for _at, ended in places], runs_on


def _set_apart(doc: QTextDocument, mark: str, ended: Sequence[int]) -> bool:
    """Each ``mark`` _marked_apart set taken out again, and its block set
    apart from the one before it (``ended``: how many quotes end between
    the two, in the marks' order). False when the marks in the text are not
    as many, a format holds one, or one stood anywhere but at a block's
    start (the Markdown was not read as the marks expected)."""
    taken = _take_marks(doc, mark)
    if len(taken) != len(ended) or _in_formats(doc, mark):
        return False
    with _one_step(doc) as cursor:
        for number, ((position, _fmt), quotes) in enumerate(zip(taken, ended), 1):
            block = doc.findBlock(position)
            if position != block.position():
                return False
            fmt = block.blockFormat()
            fmt.setProperty(APART_PROPERTY, apart_mark(number, quotes))
            cursor.setPosition(position)
            cursor.setBlockFormat(fmt)
    return True


# --------------------------------------------------------------------------- #
# What Qt's reader leaves out, kept by a mark                                  #
# --------------------------------------------------------------------------- #
#
# Qt's reader leaves out:
#
# - A list item with nothing in it ("- One\n-\n" was one item, and saved
#   so). A mark stands in each such item (markdown_scan.empty_items), and
#   once read the item is emptied again.
# - The link around a picture ("[![a](p.png)](u)": the picture, without
#   the link). A mark stands before each picture in brackets
#   (markdown_scan.bracketed): it is text, so it gets the link the words
#   around it get, and once read the picture gets it.
# - A tag in a link's words ("[x <y>](u)": "x", the tag taken for HTML
#   and dropped). Its "<" is escaped behind a mark, so the tag is read as
#   the words it is, and once read the "<" is marked as written bare
#   (BARE_TEXT_PROPERTY), so it is written bare again. Only where Qt's
#   reader reads HTML (not for a preview or a paste), and only a tag that
#   is no HTML element's and then reads as itself (_tag_is_its_text):
#   real HTML ("[x<sup>2</sup>](u)") opens the file as its text.
#
# - The angle brackets of an address ("<https://a.example>"), which
#   reads as a link that shows its address, as a bare one does. A mark
#   stands before each (markdown_scan.autolinks), and once read the link
#   after it is marked as written so (ANGLED_TEXT_PROPERTY): the writer
#   writes such a link bare where readers find it, and this one in its
#   brackets again. In a link's words the address would be a link in a
#   link, which the editor cannot hold: the file opens as its text. So
#   does one with a bare address there that Qt's reader links by itself
#   ("[see https://a.example](u)": a mark at each end of such words
#   tells), and one with a link that has no words or no address
#   ("[](u)", "[x]()"), which Qt's reader drops.
#
# For the time Qt reads the Markdown the mark is in it: a Po character no
# HTML entity names, none of _ESCAPE_MARKS, punctuation as the "!" of the
# picture is, so emphasis beside it reads as before. A file is read with
# the first one it holds nowhere (_free_mark).
_KEEP_MARKS = ("\u2e31", "\u2e33", "\u2e34", "\u2e32")


# What a mark of _marked_keeps keeps: an empty item, the link around a
# picture, a tag in a link's words, an address's angle brackets; and what
# two of them tell: where a link's words start and end.
_ITEM, _PICTURE, _TAG, _ADDRESS, _WORDS, _WORDS_END = (
    "item", "picture", "tag", "address", "words", "words end")


def _marked_keeps(markdown: str, features) -> Tuple[str, Optional[str], List[str], bool]:
    """The Markdown with a mark (_KEEP_MARKS) where Qt's reader, reading
    with ``features``, would leave out what the editor holds, the mark
    (None for none), what each mark keeps or tells (_ITEM, _PICTURE,
    _TAG, _ADDRESS, _WORDS, _WORDS_END, in their order), and whether
    something is left out all the same: an empty list item where a word
    would change the lists, a link with no words or no address, raw HTML
    but the tags kept, or no free mark."""
    items, lost = empty_items(markdown)
    # (After the line's spaces and tabs: Qt's reader keeps a tab after
    # the words of an item.)
    places = [(at, "" if markdown[at - 1] in " \t" else " ", "", _ITEM) for at in items]
    pictured, tags, addressed = bracketed(markdown)
    places += [(at, "", "", _PICTURE) for at in pictured]
    for start, end in addressed:
        places += [(start, "", "", _WORDS), (end, "", "", _WORDS_END)]
    html = _reads_html(features)
    if html:
        places += [(start, "", "\\", _TAG) for start, end in tags
                   if _tag_is_its_text(markdown[start:end])]
    addresses, wordless = autolinks(markdown) if "<" in markdown or "[" in markdown else ([], False)
    places += [(at, "", "", _ADDRESS) for at in addresses]
    places.sort()
    mark = _free_mark(markdown, _KEEP_MARKS) if places else None
    if mark is None:
        marked, kinds = markdown, []
    else:
        marked = _with_marks(markdown, [place[:3] for place in places], mark)
        kinds = [place[3] for place in places]
    # (The tags kept are escaped now: raw HTML left is dropped.)
    return marked, mark, kinds, (lost or wordless or (bool(places) and mark is None)
                                 or (html and _RAW_HTML.search(marked) is not None))


def _reads_html(features) -> bool:
    """Whether Qt's reader, reading with ``features``, takes a tag for
    HTML (the editor's reading), not for text (a preview's, a paste's)."""
    no_html = QTextDocument.MarkdownFeature.MarkdownNoHTML.value
    return (features.value & no_html) != no_html


def _label_tags(markdown: str) -> List[Tuple[int, int]]:
    """Where each tag in a link's words starts and ends that the editor
    keeps as the words it is (_marked_keeps)."""
    return [(start, end) for start, end in bracketed(markdown)[1]
            if _tag_is_its_text(markdown[start:end])]


# The names of HTML's elements: the WHATWG HTML standard's index of the
# elements (MathML's and SVG's roots among them) and the elements it
# lists as obsolete, and "image", which its parser reads as "img".
_HTML_ELEMENTS = frozenset("""
    a abbr address area article aside audio b base bdi bdo blockquote body br button canvas
    caption cite code col colgroup data datalist dd del details dfn dialog div dl dt em embed
    fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html i
    iframe img input ins kbd label legend li link main map mark math menu meta meter nav
    noscript object ol optgroup option output p picture pre progress q rp rt ruby s samp script
    search section select selectedcontent slot small source span strong style sub summary sup
    svg table tbody td template textarea tfoot th thead time title tr track u ul var video wbr
    applet acronym bgsound dir frame frameset noframes isindex keygen listing menuitem nextid
    noembed param plaintext rb rtc strike xmp basefont big blink center font marquee multicol
    nobr spacer tt image
""".split())
_TAG_NAME = re.compile(r"</?([A-Za-z][\w-]*)")


@lru_cache(maxsize=1024)
def _tag_is_its_text(tag: str) -> bool:
    """Whether ``tag`` in a link's words is words in angle brackets, not
    HTML a writer meant ("<y>", not "<sup>", "<br>", "<my-card>" or a
    comment): its name no element's (_HTML_ELEMENTS) and no custom
    element's (one with a hyphen); and whether, its "<" escaped, it reads
    as the words it is, in the link's format alone: nothing in it a reader
    takes for emphasis, code, an entity or an escape."""
    name = _TAG_NAME.match(tag)
    if name is None or "-" in name.group(1) or name.group(1).lower() in _HTML_ELEMENTS:
        return False
    doc = QTextDocument()
    doc.setMarkdown(f"[\\{tag}](u)", READ_FEATURES)
    runs = list(iter_block_runs(doc.begin()))
    return (doc.blockCount() == 1 and len(runs) == 1 and runs[0][0] == tag
            and runs[0][1].anchorHref() == "u" and not _is_bold(runs[0][1])
            and not runs[0][1].fontItalic() and not runs[0][1].fontStrikeOut()
            and not _is_code(runs[0][1]))


def _kept_by_marks(doc: QTextDocument, mark: str, kinds: Sequence[str]) -> bool:
    """Each ``mark`` _marked_keeps set taken out again, and what it kept
    (``kinds``, in the marks' order) given to its place: to the picture
    after it, the link the mark was in; to the "<" after it, that it was
    written bare in a link's words; to the link after it, that its
    address was written in angle brackets; else an empty list item. False
    when the marks in the text are not as many, a format holds one, one
    stood where its kind does not (the Markdown was not read as the marks
    expected), or an address, in angle brackets or bare, is a link of its
    own in a link's words."""
    taken = _take_marks(doc, mark)
    if len(taken) != len(kinds) or _in_formats(doc, mark):
        return False
    bare = QTextCharFormat()
    bare.setProperty(BARE_TEXT_PROPERTY, True)
    angled = QTextCharFormat()
    angled.setProperty(ANGLED_TEXT_PROPERTY, True)
    opened: List[Tuple[int, str]] = []      # where a link's words start, and its address
    with _one_step(doc) as after:
        for (position, fmt), kind in zip(taken, kinds):
            if kind == _ITEM:
                block = doc.findBlock(position)
                if block.textList() is None or block.length() != 1:
                    return False
                continue
            linked = fmt.isAnchor() and bool(fmt.anchorHref())
            if kind == _WORDS:
                opened.append((position, fmt.anchorHref() if linked else ""))
                continue
            if kind == _WORDS_END:
                start, href = opened.pop() if opened else (position, "")
                if href and not _one_link(doc, start, position, href):
                    return False            # a link in a link's words
                continue
            after.setPosition(position)
            after.setPosition(min(position + 1, doc.characterCount() - 1),
                              QTextCursor.MoveMode.KeepAnchor)
            if kind == _TAG:
                if doc.characterAt(position) != "<" or not linked:
                    return False            # brackets that were no link
                after.mergeCharFormat(bare)
            elif kind == _PICTURE:
                if doc.characterAt(position) != OBJECT_REPLACEMENT \
                        or not after.charFormat().isImageFormat():
                    return False
                if linked:
                    link = QTextCharFormat()
                    link.setAnchor(True)
                    link.setAnchorHref(fmt.anchorHref())
                    after.mergeCharFormat(link)
            else:
                if linked:
                    return False            # a link in a link's words
                address = after.charFormat()
                if address.isAnchor() and address.anchorHref():
                    after.setPosition(_link_end(doc, position, address.anchorHref()),
                                      QTextCursor.MoveMode.KeepAnchor)
                    after.mergeCharFormat(angled)

    return True


def _one_link(doc: QTextDocument, start: int, end: int, href: str) -> bool:
    """Whether every link from ``start`` to ``end`` is the one to ``href``."""
    cursor = QTextCursor(doc)
    for at in range(start + 1, end + 1):
        cursor.setPosition(at)
        fmt = cursor.charFormat()
        if fmt.isAnchor() and fmt.anchorHref() not in ("", href):
            return False
    return True


def _link_end(doc: QTextDocument, position: int, href: str) -> int:
    """Where the link to ``href`` that starts at ``position`` ends, in its
    block."""
    block = doc.findBlock(position)
    last = block.position() + block.length() - 1
    cursor = QTextCursor(doc)

    def linked(at: int) -> bool:
        """Whether the character before ``at`` is the link's."""
        if not position < at <= last:
            return False
        cursor.setPosition(at)
        fmt = cursor.charFormat()
        return fmt.isAnchor() and fmt.anchorHref() == href

    # An address shows itself, an email address without its "mailto:":
    # looked for there first, not character by character.
    for shown in (href, href.partition("mailto:")[2]):
        end = position + doc_length(shown)
        if shown and linked(end) and not linked(end + 1):
            return end
    end = position
    while linked(end + 1):
        end += 1
    return end


# Where Qt's reader puts words of the Markdown that are not its text.
_WORDS_OUTSIDE_TEXT = (QTextFormat.Property.AnchorHref, QTextFormat.Property.TextToolTip,
                       QTextFormat.Property.ImageName, QTextFormat.Property.ImageAltText,
                       QTextFormat.Property.ImageTitle)


def _in_formats(doc: QTextDocument, mark: str) -> bool:
    """Whether a format of ``doc`` holds ``mark``: a link's address or
    anchor, a title, a picture's file or description, a code language."""
    for block in iter_blocks(doc):
        if mark in block.blockFormat().stringProperty(QTextFormat.Property.BlockCodeLanguage):
            return True
        for _text, fmt in iter_block_runs(block):
            if (any(mark in fmt.stringProperty(words) for words in _WORDS_OUTSIDE_TEXT)
                    or any(mark in name for name in fmt.anchorNames())):
                return True
    return False


# --------------------------------------------------------------------------- #
# A picture's description, read whole                                          #
# --------------------------------------------------------------------------- #
#
# Qt's reader inserts the picture once for every piece of text md4c hands
# it inside ``![...]``: an escape (``\<``), an entity, emphasis, code, a
# line break or an autolink each start a piece, so ``![a \<b\>](x)`` came
# out as five pictures. Before Qt reads the Markdown, each description that
# holds such a piece becomes one plain word (a mark of this reading), and
# after it the picture gets its description back as CommonMark has it: its
# inlines read as plain text (escapes and entities resolved, emphasis and
# code without their marks, a line break a space).

# What can start a piece of text of its own in a description.
_PIECE = re.compile(r"[\\&*_`<~\n]|://|www\.|@")


_BREAK_TAG = re.compile(r"[ \t]*<br[ \t]*/?>[ \t]*\n?[ \t]*", re.IGNORECASE)


def _breaks_between_pictures(markdown: str) -> str:
    """``markdown`` with an HTML line break that stands between two
    pictures (``![a](a.png)<br>![b](b.png)``, as pages pasted from the web
    hold them) written as Markdown's own break. Qt's reader drops such a
    tag and the description of the picture after it; read without HTML
    (a paste) the tag is text between the pictures."""
    if "<" not in markdown or "![" not in markdown:
        return markdown
    found = pictures(markdown)
    pieces, last = [], 0
    for (_s, _e, after), (start, _e2, _a2) in zip(found, found[1:]):
        if _BREAK_TAG.fullmatch(markdown, after, start - 2):
            pieces.append(markdown[last:after])
            pieces.append("  \n")
            last = start - 2
    pieces.append(markdown[last:])
    return "".join(pieces)


def _plain_description(words: str, features) -> str:
    """A description as CommonMark gives it: its inlines as plain text."""
    doc = QTextDocument()
    # A letter first, so the words start no block of their own.
    doc.setMarkdown("x" + words, features)
    text = doc.toPlainText()[1:]
    text = text.replace(OBJECT_REPLACEMENT, "")
    return re.sub(r"[\n\u2028\u2029]+", " ", text)


def _whole_descriptions(markdown: str, features) -> Tuple[str, dict]:
    """``markdown`` with each picture description Qt would read in pieces
    made one plain word, and the descriptions by their word. An empty one
    too: Qt's reader leaves a picture with no description out."""
    if "![" not in markdown:
        return markdown, {}
    spans = [(start, end) for start, end in descriptions(markdown)
             if start == end or _PIECE.search(markdown, start, end)]
    if not spans:
        return markdown, {}
    stamp = "".join(chr(ord("a") + (ord(c) % 26)) for c in uuid.uuid4().hex)
    alts: dict = {}
    pieces, last = [], 0
    for number, (start, end) in enumerate(spans):
        word = f"pgalt{stamp}{number:x}"
        alts[word] = (_plain_description(markdown[start:end], features), markdown[start:end])
        pieces.append(markdown[last:start])
        pieces.append(word)
        last = end
        if markdown.startswith("][]", end):
            # Its description is its reference too ("![a *b*][]"): written
            # out, so the word in its place still finds the definition.
            pieces.append(f"][{markdown[start:end]}]")
            last = end + 3
    pieces.append(markdown[last:])
    return "".join(pieces), alts


def _restore_descriptions(doc: QTextDocument, alts: dict) -> None:
    """Give each picture read with a plain word its description, and how
    the file spelled it where the writer would spell it otherwise. A word
    Qt's reader made no picture of (a reference nothing defines) is the
    words it stood for again."""
    found = []
    pictured = set()
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isImageFormat():
                word = str(fmt.property(QTextFormat.Property.ImageAltText) or "")
                if word in alts:
                    found.append((fragment.position(), fragment.length(), *alts[word]))
                    pictured.add(word)
            it += 1
    unread = sorted(((position, word) for word in alts if word not in pictured
                     for position in _mark_places(doc, word)), reverse=True)
    with _one_step(doc) as cursor:
        for position, length, alt, spelling in found:
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextImageFormat(cursor.charFormat().toImageFormat()) if length == 1 else None
            if fmt is None:
                fmt = cursor.charFormat()
            fmt.setProperty(QTextFormat.Property.ImageAltText, alt)
            if spelling != _description_markdown(alt):
                fmt.setProperty(ALT_SPELLING_PROPERTY, spelling)
            cursor.setCharFormat(fmt)
        for position, word in unread:
            cursor.setPosition(position + len(word))
            fmt = cursor.charFormat()
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(alts[word][1], fmt)


def _tab_in_fence(markdown: str) -> bool:
    """Whether a fenced code block holds a tab (Qt's reader makes it spaces,
    and a Makefile stops working)."""
    fence = ""
    for line in markdown.split("\n"):
        found = FENCE_LINE.match(line)
        if fence:
            if found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence) \
                    and not found.group(2).strip():
                fence = ""
            elif "\t" in line:
                return True
        elif found:
            fence = found.group(1)
    return False


_HEADER_EMPHASIS = re.compile(r"\*|~~|(?<![\w\\])_|_(?!\w)")


def _table_loses(markdown: str) -> bool:
    """Whether a table says what the editor's tables cannot keep: a
    column's alignment, or emphasis in its header row (the header is bold
    by its own format there)."""
    lines = markdown.split("\n")
    for index in range(1, len(lines)):
        if DELIMITER_ROW.match(lines[index]) and "|" in lines[index - 1]:
            if ":" in lines[index] or _HEADER_EMPHASIS.search(lines[index - 1]):
                return True
    return False


def holds_faithfully(markdown: str) -> bool:
    """Whether the editor can hold this Markdown and write it back without
    losing anything. When it cannot, open it as its Markdown text; when
    finding out fails too, so that no file is one that cannot be opened
    (as its text, every file can)."""
    try:
        return _holds(markdown)
    except Exception:
        log.exception("Reading Markdown to see whether the editor holds it failed")
        return False


def _holds(markdown: str) -> bool:
    if (any(pattern.search(markdown) for pattern in _LOSSY) or _raw_html(markdown)
            or _soft_wrapped(markdown) or _tab_in_fence(markdown) or _table_loses(markdown)
            or titled_link(markdown) or lossy_definition(markdown) or emphasized_code(markdown)
            or misread_blocks(markdown)):
        return False
    # Read as the editor reads it (footnotes as their text, code in cells
    # without the escape of its pipes), but with its pictures as the
    # paragraphs they are: the editor writes a photo group back as exactly
    # the paragraphs it was read from (photo_group, whose tests hold it to
    # the byte), so the paragraphs are what is checked. The fold is the
    # editor's own, done once when the text is opened; here it would cost
    # as much again (10,000 groups: seconds of processor time and hundreds
    # of megabytes).
    first = QTextDocument()
    if not _read_counting(first, markdown, groups=False):
        return False            # a soft break or an empty item not read as marked
    laid_out = _laid_out(document_blocks(first, _image_as_read))
    shown = _fingerprint(first), _shape(laid_out)
    written = _written(laid_out)
    del first, laid_out         # freed before the next read
    second = QTextDocument()
    _read_counting(second, written, groups=False)
    # What the writer goes by is compared too: where two blocks would be
    # written as one, or a list's items closer than they stood, the file
    # would be saved otherwise than it is.
    return shown == (_fingerprint(second),
                     _shape(_laid_out(document_blocks(second, _image_as_read))))


def _image_as_read(fmt) -> str:
    """An image written back where it was read from: what a save does with
    a picture this app did not make (a web or relative address)."""
    return image_markdown(str(fmt.property(QTextImageFormat.ImageAltText) or ""), fmt.name())


def image_markdown(alt: str, url: str) -> str:
    """``![alt](url)``, the alt text written so it reads back as it is
    (none as none: ``![](url)``), and an address that holds a space or a
    parenthesis written as ``<url>``."""
    return f"![{_description_markdown(alt)}]({_destination(url)})"


@lru_cache(maxsize=1024)
def _description_markdown(alt: str) -> str:
    """A picture's description as Markdown that reads back as ``alt``.
    Plain words stay as they are, so a description nobody changed is
    written as it was read; else a "<" (which may open a tag) is escaped,
    then the brackets, and when the words still read otherwise
    (emphasis, code, an entity, an escape) every mark that can start one."""
    for words in (alt, alt.replace("<", "\\<"), re.sub(r"([\[\]<])", r"\\\1", alt)):
        if _reads_back(words, alt):
            return words
    return re.sub(r"([\\`*_\[\]<>&~!])", r"\\\1", alt)


def _reads_back(words: str, alt: str) -> bool:
    """Whether ``![words](x)`` is a picture described ``alt``."""
    if descriptions(f"![{words}](x)") != [(2, 2 + len(words))]:
        return False
    if not _PIECE.search(words):
        return words == alt
    return _plain_description(words, READ_FEATURES) == alt


def link_markdown(words: str, url: str) -> str:
    """``[words](url)``, with a bracket, a "<" or a backslash in the words
    escaped (the address itself stands in for missing words) and an
    address that holds a space or a parenthesis written as ``<url>``."""
    words = re.sub(r"([\\\[\]<])", r"\\\1", words or url)
    return f"[{words}]({_destination(url)})"


def document_to(doc, flavor: str, image_target: Optional[ImageTarget] = None) -> str:
    """:func:`document_to_markdown` or :func:`document_to_note_text`."""
    if flavor == NOTE:
        return document_to_note_text(doc, image_target)
    return document_to_markdown(doc, image_target)


__all__: Tuple[str, ...] = (
    "MARKDOWN", "NOTE", "document_blocks", "document_to", "document_to_markdown",
    "document_to_note_text", "has_local_only_formatting", "holds_faithfully",
    "image_markdown", "link_markdown", "literal_footnotes", "literal_text", "read_markdown", "READ_FEATURES",
    "PASTE_FEATURES", "remove_local_only_formatting",
)
