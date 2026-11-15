# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a paste brings into a document that holds Markdown structure.

Copied from a web page, Google Docs, Word, Pages or Notes, text arrives as
HTML with its fonts, sizes and colors; copied from a Markdown editor, as
plain text full of Markdown. Either way only what Markdown can say comes
in: headings, bold, italic, strikethrough, code, links, lists and
checklists, quotes, code blocks, tables, dividers and pictures on the
web. Nothing else can come in, by construction: the HTML is read by Qt,
tidied (below), written by the one Markdown writer (markdown_writer.py)
and read back the way a .md file is opened. Colors, fonts and sizes have
no Markdown, so they never arrive.

Qt reads the HTML rather than an HTML-to-Markdown converter because Qt
applies the inline styles that Google Docs and Word use for bold and
italic (``<span style="font-weight:700">``, inside a ``<b>`` that turns
bold off), which a converter that looks only at tags gets backwards.

Tidying what Qt read:

- Word marks list paragraphs with a style of its own instead of HTML
  lists; they become lists.
- A ``<pre>`` block, and lines that keep their spacing (VS Code), are a
  code block, with the language the page names in its classes.
- A checkbox at the start of a list item makes it a checklist item.
- Words the page sets in a monospace font are code.
- A link that would not work in a published article (a path on the site
  it came from, a script) keeps its words without the link.
- A picture inside the clipboard or on a disk cannot come along; it is
  counted, so the person can be told.

What a page shows as text stays text: the Markdown written in between
escapes it (``literal``), so "[x](y)", "**stars**" or "<div>" on a page
arrive as those characters. Text copied in this editor carries its
Markdown twice: as it would be saved (``text/markdown``, for Markdown
opened as its text and for other apps) and written literally
(``LITERAL_MIME``), which a paste here reads, so copying and pasting
keeps every word and every structure exactly. VS Code says which
language it copied: Markdown is read as Markdown, code becomes code.

Markdown is read without HTML (``PASTE_FEATURES``): a tag in it is text.

Plain text is read as Markdown only when it clearly is Markdown (two
signs of it, such as a "# " heading and a "- " list); otherwise the
editor inserts it as it is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import List, NamedTuple, Optional, Tuple

from PySide6.QtCore import QMimeData
from PySide6.QtGui import (
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextDocumentFragment,
    QTextImageFormat,
)

from doc_walk import doc_length, iter_blocks
from fonts import names_monospace
from link_url import normalize_link_input
from markdown_writer import PASTE_FEATURES, document_to_markdown, image_markdown, read_markdown
import rich_text

MARKDOWN_MIME = "text/markdown"
# The editor's own copy, written literally: only its structure is Markdown.
# Like the HTML one beside it, it is private: it names the app's own
# objects (pictures, audio and video) by their internal names, so a paste
# here brings them back; what other apps read names none of them (Outside).
LITERAL_MIME = "application/x-myeditor-markdown"
LITERAL_HTML_MIME = "application/x-myeditor-html"
VSCODE_MIME = "vscode-editor-data"


class Outside(NamedTuple):
    """How one of the app's own objects goes to another app on the
    clipboard: never by its internal name."""

    media: bool         # audio or video, a paragraph of its own
    text: str           # in text and Markdown: its address, else its name
    href: str = ""      # where it is, once on a media server
    src: str = ""       # a picture's source in HTML: its address, else its bytes

# --------------------------------------------------------------------------- #
# Plain text that is Markdown                                                  #
# --------------------------------------------------------------------------- #

# Signs that plain text is Markdown: block marks at a line start, and
# inline marks anywhere.
_LINE_SIGNS = re.compile(
    r"^(?:#{1,6} \S|[-*+] \S|\d{1,9}[.)] \S|> |```|~~~|\|.*\|[ \t]*$)", re.MULTILINE)
_INLINE_SIGNS = (re.compile(r"\*\*[^*\n]+\*\*"), re.compile(r"\[[^\]\n]+\]\([^)\s]+\)"),
                 re.compile(r"`[^`\n]+`"), re.compile(r"~~[^~\n]+~~"))


def looks_like_markdown(text: str) -> bool:
    """Whether plain text carries at least two signs of Markdown."""
    text = text or ""
    signs = len(_LINE_SIGNS.findall(text))
    if signs >= 2:
        return True
    signs += sum(1 for pattern in _INLINE_SIGNS if pattern.search(text))
    return signs >= 2


# --------------------------------------------------------------------------- #
# Tidying the HTML before Qt reads it                                          #
# --------------------------------------------------------------------------- #

# Marks put into the HTML for what Qt would forget, found again in what it
# read (characters of Unicode's private use area, which no page contains).
_CODE_START = ""         # a code block starts here; its language follows
_CODE_END = ""           # ... up to here
_CHECKED = ""            # a ticked checkbox
_UNCHECKED = ""          # an empty checkbox
_MARKS = re.compile("[-]")

_PRE = re.compile(r"(<pre\b[^>]*>)(\s*<code\b[^>]*>)?", re.IGNORECASE)
_LANGUAGE = re.compile(
    r"(?:\b(?:language|lang)-|\bbrush:\s*|\bdata-lang(?:uage)?=[\"']?)([A-Za-z0-9_#+.-]+)",
    re.IGNORECASE)
# GitHub names the language on the box around the <pre>.
_GITHUB_LANGUAGE = re.compile(r"highlight-source-([A-Za-z0-9_#+-]+)[^<>]*>\s*$",
                              re.IGNORECASE)
_CHECKBOX = re.compile(r"<input\b[^>]*\btype=[\"']?checkbox\b[^>]*>", re.IGNORECASE)
_CHECKED_ATTRIBUTE = re.compile(r"\bchecked\b", re.IGNORECASE)

# Word: <p style='mso-list:l0 level2 lfo1'><![if !supportLists]>1.<![endif]>Text</p>
_WORD_ITEM = re.compile(
    r"<p\b[^>]*\bmso-list:\s*l\d+\s+level(?P<level>\d+)[^>]*>(?P<body>.*?)</p>",
    re.IGNORECASE | re.DOTALL)
_WORD_MARKER = re.compile(
    r"<!(?:--)?\[if !supportLists\](?:--)?>(?P<marker>.*?)<!(?:--)?\[endif\](?:--)?>",
    re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_ORDERED_MARKER = re.compile(r"^\(?[0-9A-Za-z]{1,5}[.)]$")


def _language_of(text: str) -> str:
    found = _LANGUAGE.search(text)
    return found.group(1).lower()[:30] if found else ""


def _marked_code_blocks(html: str) -> str:
    def mark(match):
        before = html[max(0, match.start() - 400):match.start()]
        github = _GITHUB_LANGUAGE.search(before)
        # The <code> inside names the language most precisely
        # (language-python), the <pre> often more briefly (lang-py).
        language = (_language_of(match.group(2) or "") or _language_of(match.group(1))
                    or (github.group(1).lower()[:30] if github else ""))
        return match.group(0) + _CODE_START + language + _CODE_END
    return _PRE.sub(mark, html)


def _marked_checkboxes(html: str) -> str:
    return _CHECKBOX.sub(
        lambda m: _CHECKED if _CHECKED_ATTRIBUTE.search(m.group(0)) else _UNCHECKED, html)


def _word_lists(html: str) -> str:
    """Word's list paragraphs (a style naming the level, and the bullet or
    number typed in front) as HTML lists."""
    if "mso-list" not in html:
        return html
    out: List[str] = []
    open_lists: List[str] = []
    position = 0
    for item in _WORD_ITEM.finditer(html):
        between = html[position:item.start()]
        if open_lists and between.strip():
            # Something else came in between: the list ended.
            out.extend(f"</{kind}>" for kind in reversed(open_lists))
            open_lists.clear()
        out.append(between)
        body = item.group("body")
        marker = ""
        typed = _WORD_MARKER.search(body)
        if typed:
            marker = unescape(_TAG.sub("", typed.group("marker"))).replace("\xa0", " ").strip()
            body = body[:typed.start()] + body[typed.end():]
        kind = "ol" if _ORDERED_MARKER.match(marker) else "ul"
        level = max(1, min(int(item.group("level")), rich_text.MAX_LIST_DEPTH))
        while len(open_lists) > level:
            out.append(f"</{open_lists.pop()}>")
        if len(open_lists) == level and open_lists[-1] != kind:
            out.append(f"</{open_lists.pop()}>")
        while len(open_lists) < level:
            out.append(f"<{kind}>")
            open_lists.append(kind)
        out.append(f"<li>{body}</li>")
        position = item.end()
    out.extend(f"</{kind}>" for kind in reversed(open_lists))
    out.append(html[position:])
    return "".join(out)


class _InertDocument(QTextDocument):
    """A document that never reads a file or anything else an HTML page
    names (style sheets, pictures): what it holds is only what was pasted."""

    def loadResource(self, _type, _name):   # noqa: N802 (Qt's name)
        return None


# --------------------------------------------------------------------------- #
# Tidying what Qt read                                                         #
# --------------------------------------------------------------------------- #

def _strip_mark(block, length: int) -> None:
    cursor = QTextCursor(block)
    cursor.setPosition(block.position() + length, QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()


def _code_runs(doc) -> List[Tuple[str, list]]:
    """The code blocks Qt read: runs of lines that keep their spacing, each
    with the language its mark names."""
    runs: List[Tuple[str, list]] = []
    current: Optional[Tuple[str, list]] = None
    for block in iter_blocks(doc):
        text = block.text()
        if text.startswith(_CODE_START):
            end = text.find(_CODE_END)
            language = text[1:end] if end > 0 else ""
            _strip_mark(block, end + 1 if end > 0 else 1)
            current = (language, [block])
            runs.append(current)
        elif block.blockFormat().nonBreakableLines():
            if current is None:
                current = ("", [])
                runs.append(current)
            current[1].append(block)
        else:
            current = None
    return runs


def _make_code_blocks(doc, language: str) -> None:
    for run_language, blocks in _code_runs(doc):
        while blocks and not blocks[0].text().strip():
            blocks.pop(0)
        while blocks and not blocks[-1].text().strip():
            blocks.pop()
        for block in blocks:
            QTextCursor(block).setBlockFormat(
                rich_text.code_line_format(block.blockFormat(), run_language or language))


def _make_checklists(doc) -> None:
    for block in iter_blocks(doc):
        text = block.text()
        stripped = text.lstrip()
        if block.textList() is None or not stripped or stripped[0] not in (_CHECKED,
                                                                           _UNCHECKED):
            continue
        skip = len(text) - len(stripped) + 1
        if text[skip:skip + 1] == " ":
            skip += 1
        fmt = block.blockFormat()
        fmt.setMarker(QTextBlockFormat.MarkerType.Checked if stripped[0] == _CHECKED
                      else QTextBlockFormat.MarkerType.Unchecked)
        QTextCursor(block).setBlockFormat(fmt)
        _strip_mark(block, skip)


def _is_monospace(fmt: QTextCharFormat) -> bool:
    """Whether the page sets these words in a monospace font."""
    return names_monospace(fmt.fontFamilies())


def _kept_href(href: str) -> Optional[str]:
    """Where a pasted link may go: the web, an email, Nostr. A path on the
    site it came from, a place on that page or a script would not work
    (or would be harmful) where the article is read."""
    if not href.lower().startswith(("http://", "https://", "mailto:", "nostr:")):
        return None
    address, _reason = normalize_link_input(href, nostr=True)
    return address


# Raised and lowered digits and signs, as the characters every reader
# shows ("10<sup>6</sup>" is 10⁶, not 106; "H<sub>2</sub>O" is H₂O).
_RAISED = str.maketrans("0123456789+-=()ni", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076"
                        "\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e\u207f\u2071")
_LOWERED = str.maketrans("0123456789+-=()", "\u2080\u2081\u2082\u2083\u2084\u2085\u2086"
                         "\u2087\u2088\u2089\u208a\u208b\u208c\u208d\u208e")


def _script_text(fmt: QTextCharFormat, words: str) -> Optional[str]:
    """``words`` written raised or lowered with their own characters, when
    the page raised or lowered them and every one has such a form."""
    alignment = fmt.verticalAlignment()
    table = (_RAISED if alignment == QTextCharFormat.VerticalAlignment.AlignSuperScript
             else _LOWERED if alignment == QTextCharFormat.VerticalAlignment.AlignSubScript
             else None)
    if table is None or not words:
        return None
    shown = words.translate(table)
    if any(ch == plain for ch, plain in zip(shown, words) if not ch.isspace()):
        return None
    return shown


def _tidy_text(doc) -> None:
    """Inline code where the page used a monospace font (the words, not the
    spaces around them); links that cannot work where the article is read
    lose their link, never their words; raised and lowered digits keep
    their meaning."""
    edits = []
    code = []
    scripts = []
    for block in iter_blocks(doc):
        if block.blockFormat().nonBreakableLines():
            continue
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            it += 1
            if not fragment.isValid():
                continue
            fmt = fragment.charFormat()
            if fmt.isImageFormat():
                continue
            changed = QTextCharFormat(fmt)
            words = fragment.text()
            shown = _script_text(fmt, words)
            if shown is not None:
                scripts.append((fragment.position(), fragment.length(), shown))
            if _is_monospace(fmt) and words.strip():
                lead = len(words) - len(words.lstrip())
                # (Its length in the document: an emoji takes two places.)
                code.append((fragment.position() + lead, doc_length(words.strip())))
            if fmt.isAnchor():
                href = _kept_href(fmt.anchorHref())
                if href is None:
                    changed = rich_text.without_link(changed)
                elif href != fmt.anchorHref():
                    changed.setAnchorHref(href)
            if changed != fmt:
                edits.append((fragment.position(), fragment.length(), changed))
    for position, length, fmt in edits:
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(fmt)
    for position, length in code:
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(rich_text.style_format(rich_text.CODE, True))
    for position, length, shown in reversed(scripts):    # same lengths: places hold
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        fmt = cursor.charFormat()
        fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignNormal)
        cursor.insertText(shown, fmt)


def markdown_from_html(html: str, *, language: str = "") -> Tuple[str, int]:
    """HTML as the Markdown it can be, and how many pictures had to be left
    out. ``language`` is the code language when the source named one for
    all of it (VS Code)."""
    prepared = _marked_checkboxes(_marked_code_blocks(_word_lists(html or "")))
    doc = _InertDocument()
    doc.setHtml(prepared)
    _make_code_blocks(doc, language)
    _make_checklists(doc)
    _tidy_text(doc)
    left_out = 0

    def web_pictures_only(fmt: QTextImageFormat) -> Optional[str]:
        nonlocal left_out
        name = fmt.name()
        if not name.lower().startswith(("https://", "http://")):
            left_out += 1
            return None
        return image_markdown(str(fmt.property(QTextImageFormat.Property.ImageAltText) or ""),
                              name)

    # Literally: what the page showed as text stays text.
    markdown = document_to_markdown(doc, web_pictures_only, literal=True)
    return _MARKS.sub("", markdown), left_out


# --------------------------------------------------------------------------- #
# What a paste holds                                                           #
# --------------------------------------------------------------------------- #

@dataclass
class Pasted:
    """The document a paste brings in, and how many pictures it had to
    leave out."""
    document: QTextDocument
    left_out: int = 0


def document_from_markdown(markdown: str) -> QTextDocument:
    """Markdown read the way a .md file is opened, a tag in it as text."""
    doc = QTextDocument()
    read_markdown(doc, markdown, PASTE_FEATURES)
    return doc


def _inline_code(text: str) -> QTextDocument:
    doc = QTextDocument()
    QTextCursor(doc).insertText(text, rich_text.style_format(rich_text.CODE, True))
    return doc


def _fenced(text: str, language: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text}\n{fence}\n"


def _vscode_language(source: QMimeData) -> Optional[str]:
    """The language VS Code says it copied, or None when it did not."""
    if not source.hasFormat(VSCODE_MIME):
        return None
    try:
        data = json.loads(bytes(source.data(VSCODE_MIME)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    mode = data.get("mode") if isinstance(data, dict) else None
    return mode.lower()[:30] if isinstance(mode, str) else None


def plain_text(source: QMimeData) -> str:
    """The paste as plain text: its text, or the words of its HTML when
    it came without (a paste into a code block, Paste and Match Style)."""
    if source.hasText() and source.text():
        return source.text()
    if source.hasHtml():
        doc = _InertDocument()
        doc.setHtml(source.html())
        return doc.toPlainText()
    return ""


def from_mime(source: QMimeData) -> Optional[Pasted]:
    """What a paste brings in, or None when it is plain text to insert as
    it is."""
    for kind in (LITERAL_MIME, MARKDOWN_MIME):
        if source.hasFormat(kind):
            markdown = bytes(source.data(kind)).decode("utf-8", "replace")
            return Pasted(document_from_markdown(markdown))
    text = source.text() if source.hasText() else ""
    language = _vscode_language(source)
    if language is not None and text.strip():
        if language in ("markdown", "mdx"):
            return Pasted(document_from_markdown(text))
        if language == "plaintext":
            return None
        code = text.strip("\n")
        if "\n" not in code:
            return Pasted(_inline_code(code))
        return Pasted(document_from_markdown(_fenced(code, language)))
    if source.hasHtml():
        markdown, left_out = markdown_from_html(source.html())
        return Pasted(document_from_markdown(markdown), left_out)
    if looks_like_markdown(text):
        return Pasted(document_from_markdown(text))
    return None


def selection_markdown(cursor: QTextCursor, selected: Optional[QTextDocumentFragment] = None,
                       *, literal: bool = False, outside=None) -> str:
    """The selection as Markdown, for the clipboard: pictures keep their
    names, so they are found again when pasted into this document.
    ``selected``: what of it goes there (photo_group_cut.clipped), by
    default all of it. ``literal``: its text written to read back as the
    same text. ``outside(name)``: for Markdown other apps read, the
    Outside of one of the app's own objects (None for any other picture,
    kept as it is)."""
    doc = QTextDocument()
    QTextCursor(doc).insertFragment(selected if selected is not None else cursor.selection())
    started_in = cursor.document().findBlock(cursor.selectionStart())
    if started_in.blockFormat().headingLevel() and not doc.begin().blockFormat().headingLevel():
        # Words from inside a heading: its size and weight are the
        # heading's, not theirs.
        rich_text.set_heading(QTextCursor(doc.begin()), rich_text.BODY)

    def keep_pictures(fmt: QTextImageFormat) -> str:
        alt = str(fmt.property(QTextImageFormat.Property.ImageAltText) or "")
        found = outside(fmt.name()) if outside is not None else None
        if found is None:
            return image_markdown(alt, fmt.name())
        if found.media or not found.href:
            return found.text
        return image_markdown(alt, found.href)

    return document_to_markdown(doc, keep_pictures, literal=literal)
