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
import re
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtGui import (
    QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat,
    QTextImageFormat, QTextListFormat, QTextTable,
)

from doc_walk import (
    BULLET_MARKER,
    ESCAPED_TEXT_PROPERTY,
    LINE_SEPARATOR,
    OBJECT_REPLACEMENT,
    SOFT_BREAK_PROPERTY,
    TYPED_TEXT_PROPERTY,
    bullet_depth,
    iter_block_runs,
    iter_blocks,
    parse_bullet_line,
    skip_prefix,
)
import photo_group
import photo_group_find
from markdown_scan import (ESCAPABLE, FENCE_LINE, LIST_LINE, descriptions, emphasized_code,
                           escapes, lossy_definition, pictures, titled_link)
from rich_text import normalize_after_markdown_load

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
    fence: str = ""
    language: str = ""
    code_text: str = ""
    rows: List[List[List[_Span]]] = field(default_factory=list)
    bullet_text: str = ""      # a typed bullet line, exactly as typed
    group: int = -1            # which list an item belongs to
    quote: int = 0             # how deep in block quotes it sits
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
                spans.append(_Span(text=_as_spelled(fmt, target), raw=True))
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
                           escaped=fmt.boolProperty(ESCAPED_TEXT_PROPERTY)))
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
                     out[-1].literal, out[-1].escaped) == (span.bold, span.italic, span.strike,
                                                           span.code, span.href, span.literal,
                                                           span.escaped)):
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
            paragraphs[-1].extend(photo_group.join_runs(unit.join))
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
    for r in range(table.rows()):
        row = []
        for c in range(table.columns()):
            cell = table.cellAt(r, c)
            spans: List[_Span] = []
            it = cell.begin()
            first = True
            while not it.atEnd():
                block = it.currentBlock()
                if block.isValid():
                    if not first:
                        spans.append(_Span(text=" "))
                    spans.extend(_spans_of(_block_runs(block), image_target))
                    first = False
                it += 1
            if r == 0:
                # The header row is bold by its own format.
                spans = [_plain_weight(span) for span in spans]
            row.append(_merged(spans))
        rows.append(row)
    return _Block("table", rows=rows, spaced=True)


def _blocks_of_frame(frame, image_target) -> List[_Block]:
    blocks: List[_Block] = []
    it = frame.begin()
    while not it.atEnd():
        child = it.currentFrame()
        block = it.currentBlock()
        if child is not None:
            if isinstance(child, QTextTable):
                blocks.append(_table_block(child, image_target))
            else:
                blocks.extend(_blocks_of_frame(child, image_target))
        elif block.isValid():
            # A photo group is written as the paragraphs it was read from:
            # its pictures, each with its caption (photo_group).
            segments = photo_group.block_segments(block)
            if segments is not None:
                blocks.extend(_group_blocks(block, segments, image_target))
            else:
                blocks.append(_one_block(block, image_target))
        it += 1
    return _joined_code(blocks)


def _one_block(block, image_target) -> _Block:
    written = _block_kind(block, image_target)
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
        language = fmt.property(QTextFormat.Property.BlockCodeLanguage) or ""
        text = block.text().replace(LINE_SEPARATOR, "\n").replace(OBJECT_REPLACEMENT, "")
        return _Block("code", fence=str(fence or "`"), language=str(language),
                      code_text=text, spaced=True)
    text_list = block.textList()
    if text_list is not None:
        return _list_block(block, text_list, image_target)
    return _paragraph_block(block, image_target)


def _joined_code(blocks: List[_Block]) -> List[_Block]:
    """Qt keeps each line of a fenced block as a block of its own."""
    out: List[_Block] = []
    for block in blocks:
        previous = out[-1] if out else None
        if (block.kind == "code" and previous is not None and previous.kind == "code"
                and previous.fence == block.fence and previous.language == block.language
                and previous.quote == block.quote):
            previous.code_text += "\n" + block.code_text
        else:
            out.append(block)
    return out


def document_blocks(doc, image_target: Optional[ImageTarget] = None) -> List[_Block]:
    """The document as the blocks both writers share. Exposed for tests."""
    return _blocks_of_frame(doc.rootFrame(), image_target)


# --------------------------------------------------------------------------- #
# Markdown                                                                     #
# --------------------------------------------------------------------------- #

def _protect_addresses(text: str) -> str:
    """Typed text as typed, except a web address holding a character a
    Markdown reader would take for emphasis or code: that one is written
    as ``<address>``, which keeps it intact everywhere."""
    def protect(match):
        address = match.group(0)
        if any(ch in _MARKUP_IN_ADDRESS for ch in address):
            return f"<{address}>"
        return address
    return _WEB_ADDRESS.sub(protect, text)


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


def _code_span(text: str) -> str:
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


def _autolink(address: str, href: str, before: str, following: Optional[_Span]) -> str:
    """A link that shows its own address, written so every reader links it.

    Bare, the way it was typed, where a reader finds it by itself: a web
    address or an email between spaces, with no character a reader would
    take for emphasis, and only one that Qt's reader takes back whole.
    Readers that look for a bare address (a picture or a video alone on
    its line becomes a player) find it there. Anywhere else it is written
    ``<address>``, which every reader keeps whole.
    """
    is_email = "@" in address and "://" not in address
    findable = (address.lower().startswith(("https://", "http://", "www.")) or is_email)
    if (findable and not any(ch in _MARKUP_IN_ADDRESS for ch in address)
            and (not before or before[-1].isspace() or before[-1] == "(")
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


def _inline_markdown(spans: List[_Span], hard_break: str = "  \n") -> str:
    caption = _caption_start(spans)
    if caption is not None:
        words = _inline_markdown([replace(span, italic=False) for span in spans[caption:]],
                                 hard_break)
        spans = spans[:caption] + [_Span(text=_wrap(words, "*"), raw=True)]
    out = []
    i = 0
    while i < len(spans):
        span = spans[i]
        if span.raw:
            out.append(span.text)
            i += 1
            continue
        if span.href:
            # One link around every neighbour that points at the same place.
            group = []
            while i < len(spans) and spans[i].href == span.href and not spans[i].raw:
                group.append(spans[i])
                i += 1
            following = spans[i] if i < len(spans) else None
            if _is_nostr_reference(span.href) and not span.literal:
                # Bare, for readers, in the styles all its words have; a
                # copy keeps the words it shows.
                bare = _styled([_Span(text=span.href, bold=all(s.bold for s in group),
                                      italic=all(s.italic for s in group),
                                      strike=all(s.strike for s in group))])
                out.append(_apart(bare, "".join(out), following))
                continue
            words = "".join(s.text for s in group).replace(LINE_SEPARATOR, " ").replace(
                _SOFT, " ")
            address = _own_address(words.strip(), span.href)
            if address and not any(s.bold or s.italic or s.strike for s in group):
                out.append(_autolink(address, span.href, "".join(out), following))
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
    # A line break at the very end of a block breaks nothing, and Markdown
    # drops it; trailing spaces there mean nothing either.
    text = "".join(out).rstrip(LINE_SEPARATOR + _SOFT + " ")
    # A soft break is the line end it was; where a hard one is a space (a
    # heading), so is a soft one.
    soft = "\n" if hard_break.endswith("\n") else hard_break
    return text.replace(LINE_SEPARATOR, hard_break).replace(_SOFT, soft)


def _style(span: _Span) -> Tuple[bool, bool, bool, bool]:
    """What a span's marks are: neighbours in one style are written inside
    them once, though one holds text a file escaped ("**a \\* b**", not
    "**a** **\\*** **b**")."""
    return span.bold, span.italic, span.strike, span.code


def _style_runs(spans: List[_Span]) -> List[List[_Span]]:
    """``spans`` cut where the style changes (_style)."""
    runs: List[List[_Span]] = []
    for span in spans:
        if runs and _style(runs[-1][0]) == _style(span):
            runs[-1].append(span)
        else:
            runs.append([span])
    return runs


def _words(span: _Span, label: bool) -> str:
    """A span's text as Markdown, without its style: what the file escaped
    escaped as it was, literal text escaped, else web addresses kept text
    and, in a link's label, a bracket or a "<" (read as HTML) escaped,
    which would end or change the link when the file is read again."""
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
    if first.code:
        parts = "".join(span.text for span in spans).split(LINE_SEPARATOR)
        return LINE_SEPARATOR.join(_code_span(p) for p in parts if p)
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
    if not block.rows:
        return ""
    width = max(len(row) for row in block.rows)

    def cell(spans):
        text = _inline_markdown(spans, hard_break=" ")
        # Every pipe escaped that is not yet (one a file escaped is).
        return _BARE_PIPE.sub(r"\1\\|", text).replace("\n", " ").strip() or " "

    lines = []
    for index, row in enumerate(block.rows):
        cells = [cell(spans) for spans in row] + [" "] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def _markdown_lines(block: _Block) -> str:
    text = _block_markdown(block)
    if not block.quote:
        return text
    prefix = "> " * block.quote
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.split("\n"))


def _block_markdown(block: _Block) -> str:
    if block.kind == "heading":
        return "#" * min(6, block.level) + " " + _inline_markdown(block.spans, " ").strip()
    if block.kind == "rule":
        return "---"
    if block.kind == "code":
        fence = "```"
        while fence in block.code_text:
            fence += "`"
        return f"{fence}{block.language}\n{block.code_text}\n{fence}"
    if block.kind == "table":
        return _table_markdown(block)
    if block.kind == "quote":
        prefix = "> " * block.level
        body = _inline_markdown(block.spans)
        return "\n".join(prefix + line if line else prefix.rstrip()
                         for line in body.split("\n"))
    if block.kind == "item":
        marker = f"{block.number}." if block.ordered else "-"
        if block.task is not None:
            marker += " [x]" if block.task else " [ ]"
        pad = " " * (LIST_INDENT * (block.level - 1))
        body = _inline_markdown(block.spans)
        cont = " " * (len(pad) + len(marker) + 1)
        lines = body.split("\n")
        return "\n".join([f"{pad}{marker} {lines[0]}".rstrip()]
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
    out: List[str] = []
    previous: Optional[_Block] = None
    for block in blocks:
        if block.kind == "empty":
            previous = block
            continue
        text = _markdown_lines(block)
        if previous is not None and out:
            out.append(_separator(previous, block))
        out.append(text)
        previous = block
    return "".join(out).rstrip("\n") + ("\n" if out else "")


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


def _quote_depth(block: _Block) -> int:
    return block.level if block.kind == "quote" else block.quote


def _separator(previous: _Block, block: _Block) -> str:
    """What goes between two written blocks: a line break, or a blank line."""
    if previous.kind == "empty":
        return "\n\n"
    if previous.kind == "item" and block.kind == "item":
        # A different list at the same depth is a list of its own.
        return "\n\n" if _new_list(previous, block) else "\n"
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
        if span.href:
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
    for block in iter_blocks(doc):
        for _text, fmt in iter_block_runs(block):
            if fmt.isImageFormat() or fmt.isAnchor():
                continue
            if (fmt.fontUnderline()
                    or fmt.hasProperty(QTextCharFormat.Property.ForegroundBrush)
                    or fmt.hasProperty(QTextCharFormat.Property.BackgroundBrush)):
                return True
    return False


# Markdown the editor's document cannot hold, so reading it in would lose
# something: a footnote whose note goes on in a second paragraph (the
# editor keeps footnotes as their text, one paragraph each), a tab where a
# line starts (lists and code are indented with spaces), code inside a
# list item (it moves out of the item), an escaped footnote mark, raw
# HTML, a link around an image, a heading inside a quote, a paragraph
# continued inside a list item, and lines that wrap inside a paragraph (a
# draft saved as plain text by an earlier version). Also (below): a tab
# inside fenced code, a column's alignment, emphasis in a table's header
# row, a link title (markdown_scan.titled_link), a link reference
# definition in a quote or a list item or with a title
# (markdown_scan.lossy_definition), emphasis around nothing but code
# (markdown_scan.emphasized_code); these three outside code.
_LOSSY = [
    re.compile(r"^ {0,3}\[\^[^\]]+\]:.*\n(?:[ \t]*\n)+(?: {4}|\t)\S",
               re.MULTILINE),                              # a note of two paragraphs
    re.compile(r"^\t", re.MULTILINE),                     # a tab where a line starts
    re.compile(r"^ {2,}(?:```|~~~)", re.MULTILINE),        # code inside a list item
    re.compile(r"\\\[\^"),                                 # an escaped footnote mark
    # raw HTML: not an escaped "\<", nor a link's whole address in <…>
    # (one with a space); a "<" that opens no such address is HTML
    re.compile(r"(?<!\\)(?<!\]\()<(?:[A-Za-z][\w-]*[\s/>]|/[A-Za-z]|!--)"),
    re.compile(r"\]\(<(?![^<>\n]*>\))"),
    re.compile(r"\[!\["),                                  # image inside a link
    re.compile(r"^\s*>\s*#", re.MULTILINE),               # heading in a quote
]
_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|>|#|\||```|~~~|    |\t)")


def _soft_breaks(markdown: str) -> Tuple[bool, List[int]]:
    """Where lines of one paragraph follow each other (soft line breaks):
    ``(lossy, lines)``, ``lines`` the numbers of the lines a soft break
    ends. ``lossy`` when a break is one the editor cannot hold: inside an
    indented paragraph or a list item, after a backslash break, or a list
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
                and not _BLOCK_START.match(before) and not before.endswith("  ")
                and not set(line.strip()) <= set("=-")):
            # (A pipe may be a table without its outer pipes.)
            if (line[:1] in (" ", "\t") or before[:1] in (" ", "\t")
                    or before.endswith("\\")
                    or "|" in line or "|" in before):
                return True, []
            found.append(index - 1)
        if (line.startswith(("  ", "\t")) and line.strip() and not before.strip()
                and index >= 2 and LIST_LINE.match(lines[index - 2])):
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
    position = 0
    while True:
        cursor = doc.find(mark, position)
        if cursor.isNull():
            return joined
        start = cursor.selectionStart()
        block = doc.findBlock(start)
        if start + 1 == block.position() + block.length() - 1 and block.next().isValid():
            # The mark and the paragraph's end, in the next block's place,
            # in the mark's own format: the words before it may be a link
            # (an address that ends the line), code or emphasis, which the
            # break would join to the next line's; after a picture (a
            # caption on the line under it), in none: the format of the
            # picture would make the break a second copy of it.
            fmt = QTextCharFormat(cursor.charFormat())
            if fmt.isImageFormat():
                fmt = QTextCharFormat()
            cursor.setPosition(start)
            cursor.setPosition(start + 2, QTextCursor.MoveMode.KeepAnchor)
            fmt.setProperty(SOFT_BREAK_PROPERTY, True)
            cursor.insertText(LINE_SEPARATOR, fmt)
            joined += 1
        else:
            cursor.removeSelectedText()
        position = start + 1


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
                   groups: bool = True) -> Tuple[int, int]:
    """read_markdown, saying how many soft line breaks the Markdown has
    that the editor holds, and how many it holds. The editor's reading
    (READ_FEATURES) keeps them; a preview or a paste reads them as
    Markdown readers do, as spaces. ``groups``: pictures that follow each
    other are folded into their group, as the editor shows them; without,
    they stay the paragraphs they are in the Markdown."""
    markdown = _breaks_between_pictures(markdown)
    markdown, alts = _whole_descriptions(markdown, features)
    marked, soft_mark = 0, None
    if features == READ_FEATURES:
        markdown, soft_mark, marked = _marked_soft_breaks(markdown)
    escaped, mark = _marked_escapes(markdown)
    joined = _read_once(doc, escaped, features, alts, (soft_mark, marked))
    if mark and not _keep_escaped(doc, mark, escaped.count(mark)):
        # A mark Qt's reader put outside the text (a link's address, a
        # picture's file, a title), where nothing takes it out: read
        # without marks, the escapes then are saved as the writer writes
        # text, and holds_faithfully opens the file as its text when
        # that would change it.
        joined = _read_once(doc, markdown, features, alts, (soft_mark, marked))
    normalize_after_markdown_load(doc)
    if groups:
        # Pictures that follow each other show as one group (photo_group).
        photo_group_find.fold(doc)
    return marked, joined


def _read_once(doc: QTextDocument, markdown: str, features, alts: dict,
               soft: Tuple[Optional[str], int]) -> int:
    """Qt's reading of the marked ``markdown`` into ``doc``, the pictures
    given their descriptions (``alts``) and the ``soft`` breaks (their
    mark, how many) joined; how many were joined."""
    doc.setMarkdown(literal_footnotes(markdown), features)
    if alts:
        _restore_descriptions(doc, alts)
    soft_mark, marked = soft
    return _join_soft_breaks(doc, soft_mark) if marked else 0


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
    pieces, last = [], 0
    for at in found:
        pieces.append(markdown[last:at])
        pieces.append(mark)
        last = at
    pieces.append(markdown[last:])
    return "".join(pieces), mark


def _keep_escaped(doc: QTextDocument, mark: str, placed: int) -> bool:
    """Each ``mark`` _marked_escapes set gone from the text, and the
    character after it marked escaped. False when fewer than the
    ``placed`` marks were in the text, or a format holds one (a link's
    address, a picture's file or description, a title): it is then in
    what is saved, copied, exported and published."""
    position, removed = 0, 0
    text = QTextCharFormat()
    text.setProperty(ESCAPED_TEXT_PROPERTY, True)
    while True:
        cursor = doc.find(mark, position)
        if cursor.isNull():
            break
        position = cursor.selectionStart()
        cursor.removeSelectedText()
        removed += 1
        if doc.characterAt(position) in ESCAPABLE:
            cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(text)
    return removed == placed and not _in_formats(doc, mark)


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
    pieces.append(markdown[last:])
    return "".join(pieces), alts


def _restore_descriptions(doc: QTextDocument, alts: dict) -> None:
    """Give each picture read with a plain word its description, and how
    the file spelled it where the writer would spell it otherwise."""
    found = []
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isImageFormat():
                word = str(fmt.property(QTextFormat.Property.ImageAltText) or "")
                if word in alts:
                    found.append((fragment.position(), fragment.length(), *alts[word]))
            it += 1
    for position, length, alt, spelling in found:
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextImageFormat(cursor.charFormat().toImageFormat()) if length == 1 else None
        if fmt is None:
            fmt = cursor.charFormat()
        fmt.setProperty(QTextFormat.Property.ImageAltText, alt)
        if spelling != _description_markdown(alt):
            fmt.setProperty(ALT_SPELLING_PROPERTY, spelling)
        cursor.setCharFormat(fmt)


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


_DELIMITER_ROW = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*(?::?-+:?)?[ \t]*$")
_HEADER_EMPHASIS = re.compile(r"\*|~~|(?<![\w\\])_|_(?!\w)")


def _table_loses(markdown: str) -> bool:
    """Whether a table says what the editor's tables cannot keep: a
    column's alignment, or emphasis in its header row (the header is bold
    by its own format there)."""
    lines = markdown.split("\n")
    for index in range(1, len(lines)):
        if _DELIMITER_ROW.match(lines[index]) and "|" in lines[index - 1]:
            if ":" in lines[index] or _HEADER_EMPHASIS.search(lines[index - 1]):
                return True
    return False


def holds_faithfully(markdown: str) -> bool:
    """Whether the editor can hold this Markdown and write it back without
    losing anything. When it cannot, open it as its Markdown text."""
    if (any(pattern.search(markdown) for pattern in _LOSSY) or _soft_wrapped(markdown)
            or _tab_in_fence(markdown) or _table_loses(markdown) or titled_link(markdown)
            or lossy_definition(markdown) or emphasized_code(markdown)):
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
    marked, joined = _read_counting(first, markdown, groups=False)
    if joined != marked:
        return False            # a soft break Qt did not read as marked
    shown = _fingerprint(first)
    written = document_to_markdown(first, _image_as_read)
    del first                   # freed before the next read
    second = QTextDocument()
    _read_counting(second, written, groups=False)
    return shown == _fingerprint(second)


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
    "PASTE_FEATURES",
)
