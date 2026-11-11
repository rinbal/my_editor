# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A note or an article, shown the way Nostr apps will show it.

The preview renders exactly what is published (the text the publish
dialogs and drafts send, from markdown_writer.py), never the editor's own
document, so what it shows is what readers get. It follows what the
widely used apps do, as surveyed in October 2026:

Articles (kind 30023) are Markdown, rendered like the long-form readers
(Habla, Highlighter, YakiHonne, Primal, noStrudel): a cover image, the
title, the summary in muted text, a byline ("Name · date · N min read",
225 words a minute), then the body in a calm serif column about 680
pixels wide, headings in bold sans, quotes indented and muted, code on a
light background without colors, tags as "#tag" after the body. A single
line break inside a paragraph is a space, as everywhere; raw HTML is
shown as text.

Notes (kind 1) are plain text in every app that matters: line breaks are
kept, ``**`` stays literal, links, hashtags and mentions are colored, and
images (by file extension) show below the text, their address taken out
of it, as Damus and Primal do.

In both, a ``nostr:`` reference to a person shows as "@Name" (their
name when it is known, a shortened key otherwise); one to a note or an
article shows as a labelled link, since cards differ between apps.
The address of audio or video (by file extension) shows where readers
put their player, as a card: a video's first frame with a play button,
or a strip for audio, its name and how long it plays. Clicking it plays
the file in the app.

Footnotes in an article show as most long-form readers draw them: raised
numbers by first use, and the notes numbered at the end (the preview
also says that some apps show them as typed).

:func:`article_document` and :func:`note_document` build a
QTextDocument; :class:`NostrPreview` shows one in a QTextBrowser, with
images from an injected loader (no loader, no network).
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import shiboken6
from PySide6.QtCore import QDate, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
    QTextListFormat,
)
from PySide6.QtWidgets import QTextBrowser

import i18n
import media_files
import rich_text
import text_look
import word_count
from fonts import monospace_family
from text_size import text_size
from doc_walk import doc_length, doc_offset, iter_blocks
from i18n import _, ngettext
import photo_group
import photo_group_drawing
import photo_group_view
from markdown_writer import READ_FEATURES, read_markdown
from nostr import refs
from nostr.refs import outside_code

# person's pubkey (hex) -> the name to show, or None
NameLookup = Callable[[str], Optional[str]]
# image address -> the image, when it is at hand (no network here)
ImageLookup = Callable[[str], Optional[QImage]]
# The name an audio or video file had, by its address (None when unknown).
MediaNames = Callable[[str], Optional[str]]

COLUMN_WIDTH = 680
WORDS_PER_MINUTE = word_count.WORDS_PER_MINUTE
SERIF = ["Charter", "Iowan Old Style", "Source Serif Pro", "Georgia", "serif"]
MEDIA_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_REFERENCE = re.compile(r"nostr:(npub1|nprofile1|note1|nevent1|naddr1)[02-9ac-hj-np-z]+")
_URL = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]]")
_HASHTAG = re.compile(r"(?<![\w#])#(\w[\w-]*)")


@dataclass(frozen=True)
class Palette:
    text: str
    muted: str
    link: str
    code_background: str
    hairline: str
    background: str


LIGHT = Palette("#222222", "#6B6B6B", "#0A66C2", "#F6F8FA", "#E6E6E6", "#FFFFFF")
DARK = Palette("#E8E8E8", "#9A9A9A", "#4AA3FF", "#26282B", "#2C2C2E", "#161618")


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT


@dataclass(frozen=True)
class Article:
    """What an article carries besides its body."""

    title: str = ""
    summary: str = ""
    image: str = ""
    tags: Sequence[str] = field(default_factory=tuple)
    author: str = ""
    published_at: int = 0          # unix seconds; 0 = now


# --------------------------------------------------------------------------- #
# References                                                                   #
# --------------------------------------------------------------------------- #

short_key = refs.short_key


def reference_label(reference: str, names: Optional[NameLookup]) -> str:
    """How a ``nostr:`` reference reads in the preview (refs.reference_label)."""
    return refs.reference_label(reference, names)


def player_label(url: str, media_names: Optional[MediaNames] = None) -> Optional[str]:
    """"▶ Audio: name" or "▶ Video: name" for the address of audio or
    video, where readers show a player, named as the file was (else by its
    address); None for any other address."""
    path = QUrl(url).path()
    mime = media_files.type_of(path) or ""
    if media_files.kind_of(mime) is None:
        return None
    name = (media_names(url) if media_names else None) or path.rsplit("/", 1)[-1]
    return media_files.link_words(mime, name)


# The picture of a player card names the address it plays, in a name of
# its own (the address in it, base64url), so the card is asked for through
# the preview's image lookup like any picture, and never fetched.
PLAYER_SCHEME = "myeditor-player"


def player_card_name(url: str) -> str:
    """The picture name of the player card for the audio or video at ``url``."""
    return f"{PLAYER_SCHEME}:" + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")


def player_card_address(name: str) -> str:
    """The address a player card's picture name plays, "" for any other name."""
    prefix = f"{PLAYER_SCHEME}:"
    if not name.startswith(prefix):
        return ""
    try:
        return base64.urlsafe_b64decode(name[len(prefix):].encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        return ""


def _player_markdown(url: str, label: str) -> str:
    """The player card, a picture linked to the file, in Markdown."""
    alt = label.replace("\\", "\\\\").replace("]", "\\]").replace("[", "\\[")
    return f"[![{alt}]({player_card_name(url)})]({url})"


_OWN_LINE_URL = re.compile(r"^(https?://\S+)$", re.MULTILINE)


def markdown_for_preview(markdown: str, names: Optional[NameLookup] = None,
                         media_names: Optional[MediaNames] = None) -> str:
    """The article's Markdown with every ``nostr:`` reference turned into
    a link that reads like the apps show it, and audio or video standing
    on a line of its own shown as the player readers put there (a card
    that plays it when clicked)."""
    def player(match) -> str:
        label = player_label(match.group(1), media_names)
        return _player_markdown(match.group(1), label) if label else match.group(0)

    def replace(text: str) -> str:
        text = _OWN_LINE_URL.sub(player, text)
        return _REFERENCE.sub(
            lambda m: f"[{reference_label(m.group(0), names)}]({m.group(0)})", text)
    return outside_code(markdown, replace)


# --------------------------------------------------------------------------- #
# Articles                                                                     #
# --------------------------------------------------------------------------- #

def reading_minutes(markdown: str) -> int:
    """Minutes to read the article, by the one rule (word_count.py); at
    least one, since a byline never says "0 min read"."""
    return max(1, word_count.reading_minutes(word_count.count_markdown_words(markdown)))


def _date_text(published_at: int) -> str:
    from PySide6.QtCore import QDateTime
    when = (QDateTime.fromSecsSinceEpoch(published_at).date() if published_at
            else QDate.currentDate())
    return i18n.format_day(when, short_month=True)


def _char(*, size: float = 0, bold: bool = False, color: str = "",
          family: Optional[Sequence[str]] = None) -> QTextCharFormat:
    fmt = QTextCharFormat()
    if size:
        fmt.setFontPointSize(size)
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if color:
        fmt.setForeground(QColor(color))
    if family:
        fmt.setFontFamilies(list(family))
    return fmt


def _CODE_FAMILIES() -> List[str]:
    return [monospace_family(), "monospace"]


def _style_body(first_block, colors: Palette) -> None:
    """Give the rendered Markdown the readers' look, block by block, from
    ``first_block`` to the end."""
    heading_sizes = {1: 22.0, 2: 18.0, 3: 15.0}
    block = first_block
    while block.isValid():
        fmt = block.blockFormat()
        cursor = QTextCursor(block)
        heading = fmt.headingLevel()
        quote = fmt.property(QTextFormat.Property.BlockQuoteLevel)
        in_table = cursor.currentTable() is not None
        if heading:
            fmt.setTopMargin(22)
            fmt.setBottomMargin(8)
            char = _char(size=heading_sizes.get(heading, 13.5), bold=True, color=colors.text)
        elif rich_text.is_code_block(block):
            # One box behind the whole block is painted (text_look), with
            # room around its lines; a background per line would leave a
            # gap between them.
            first, last = rich_text.code_run(block)
            fmt.clearBackground()
            fmt.setLeftMargin(text_look.CODE_PAD_X + 2)
            fmt.setRightMargin(text_look.CODE_PAD_X + 2)
            fmt.setTopMargin(10 if block == first else 0)
            fmt.setBottomMargin(14 if block == last else 0)
            fmt.setLineHeight(135, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            char = _char(size=12, color=colors.text, family=_CODE_FAMILIES())
        elif in_table:
            fmt.setBottomMargin(0)
            char = _char(size=12.5, color=colors.text, family=SERIF)
        else:
            # Text gets room between its lines; a picture or a player alone
            # in its paragraph does not (160 % of a picture's height would
            # leave a gap below it larger than half the picture), nor does a
            # photo group's paragraph, whose rows are its lines.
            pictures = block.text() == "\ufffc" or photo_group.is_group_block(block)
            height = 100 if pictures else 160
            fmt.setLineHeight(height, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            # List items sit closer together than paragraphs.
            if block.textList() is not None:
                fmt.setTopMargin(0)
            fmt.setBottomMargin(4 if block.textList() is not None else 12)
            color = colors.muted if isinstance(quote, int) and quote > 0 else colors.text
            char = _char(size=13.5, color=color, family=SERIF)
            if isinstance(quote, int) and quote > 0:
                # Room for a bar beside each level (text_look).
                fmt.setLeftMargin(20 * quote)
        cursor.setBlockFormat(fmt)
        _merge_fragments(block, char, colors)
        block = block.next()


def _merge_fragments(block, char: QTextCharFormat, colors: Palette) -> None:
    """Apply ``char`` to every piece of the block, keeping inline code in
    its fixed-width font and links in the link color."""
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid():
            cursor = QTextCursor(block.document())
            cursor.setPosition(fragment.position())
            cursor.setPosition(fragment.position() + fragment.length(),
                               QTextCursor.MoveMode.KeepAnchor)
            own = fragment.charFormat()
            if not own.isImageFormat():
                fmt = QTextCharFormat(own)
                fmt.merge(char)
                # Qt sizes headings relative to the text; the size given wins.
                fmt.clearProperty(QTextFormat.Property.FontSizeAdjustment)
                if own.fontFixedPitch() or rich_text.is_code_block(block):
                    # "monospace", which Markdown gives code, is no font on
                    # macOS or Windows: the system's own monospace font.
                    fmt.setFontFamilies(_CODE_FAMILIES())
                    if not rich_text.is_code_block(block):
                        fmt.setBackground(QColor(colors.code_background))
                if own.isAnchor():
                    fmt.setForeground(QColor(colors.link))
                cursor.setCharFormat(fmt)
        it += 1


# Read the way the editor reads it (``_word_`` is italic, as in every
# reader), with raw HTML shown as text: NIP-23 rules HTML out, and the
# apps that clean it up show it as text or drop it.
MARKDOWN_FEATURES = QTextDocument.MarkdownFeature(
    READ_FEATURES.value | QTextDocument.MarkdownFeature.MarkdownNoHTML.value)


def body_document(markdown: str, *, names: Optional[NameLookup] = None,
                  media_names: Optional[MediaNames] = None,
                  dark: bool = False, parent=None) -> QTextDocument:
    """Markdown as the readers show an article's body: the document's
    preview when nothing is published."""
    doc = QTextDocument(parent)
    doc.setDocumentMargin(0)
    # List markers are drawn in the document's font: the body's, so they
    # sit on the line at the size of its words.
    body = QFont()
    body.setFamilies(SERIF)
    body.setPointSizeF(13.5)
    doc.setDefaultFont(body)
    read_markdown(doc, markdown_for_preview(markdown, names, media_names), MARKDOWN_FEATURES)
    notes = _render_footnotes(doc)
    _style_body(doc.begin(), palette(dark))
    if notes:
        _anchor_notes(doc, len(notes))
    return doc


# --------------------------------------------------------------------------- #
# Footnotes                                                                    #
# --------------------------------------------------------------------------- #

def _render_footnotes(doc: QTextDocument) -> List[int]:
    """Footnotes the way the long-form readers draw them (Primal,
    YakiHonne, noStrudel, njump, Highlighter, as GitHub does): each mark a
    raised number, by first use, that leads to its note; the notes
    numbered under a rule at the end, each with a way back. A mark with no
    note stays as typed, and a note no mark refers to is not shown, as in
    those readers. Returns the numbers given, in order."""
    notes = {}
    defining = []
    for block in iter_blocks(doc):
        if rich_text.is_code_block(block):
            continue
        found = rich_text.FOOTNOTE_DEFINITION.match(block.text())
        if found is None:
            continue
        defining.append(block)
        if found.group(1) not in notes:
            text = block.text()
            rest = text[found.end():]
            words = QTextCursor(block)
            words.setPosition(block.position() + doc_offset(
                text, found.end() + len(rest) - len(rest.lstrip())))
            words.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                               QTextCursor.MoveMode.KeepAnchor)
            notes[found.group(1)] = words.selection()
    if not notes:
        return []
    numbers = {}
    marks = []
    for block in iter_blocks(doc):
        if rich_text.is_code_block(block) or block in defining:
            continue
        text = block.text()
        for found in rich_text.FOOTNOTE_MARK.finditer(text):
            label = found.group(1)
            # (Places in the text, as places in the document.)
            start = block.position() + doc_offset(text, found.start())
            probe = QTextCursor(block)
            probe.setPosition(start + 1)
            if label in notes and not probe.charFormat().fontFixedPitch():
                first = label not in numbers
                numbers.setdefault(label, len(numbers) + 1)
                marks.append((start, doc_length(found.group(0)), numbers[label], first))
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for position, length, number, first in reversed(marks):
        mark = QTextCursor(doc)
        mark.setPosition(position)
        mark.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat(mark.charFormat())
        fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSuperScript)
        fmt.setAnchor(True)
        fmt.setAnchorHref(f"#fn-{number}")
        if first:
            fmt.setAnchorNames([f"fnref-{number}"])
        mark.insertText(str(number), fmt)
    for block in reversed(defining):
        gone = QTextCursor(block)
        if block.previous().isValid():
            gone.setPosition(block.position() - 1)
            gone.setPosition(block.position() + block.length() - 1,
                             QTextCursor.MoveMode.KeepAnchor)
        else:
            gone.movePosition(QTextCursor.MoveOperation.NextBlock,
                              QTextCursor.MoveMode.KeepAnchor)
        gone.removeSelectedText()
    if numbers:
        end = QTextCursor(doc)
        end.movePosition(QTextCursor.MoveOperation.End)
        rule = QTextBlockFormat()
        rule.setProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth, 1)
        end.insertBlock(rule, QTextCharFormat())
        listed = None
        for label, number in sorted(numbers.items(), key=lambda item: item[1]):
            end.insertBlock(QTextBlockFormat(), QTextCharFormat())
            if listed is None:
                listed = end.createList(QTextListFormat.Style.ListDecimal)
            else:
                listed.add(end.block())
            end.insertFragment(notes[label])
            back = QTextCharFormat()
            back.setAnchor(True)
            back.setAnchorHref(f"#fnref-{number}")
            end.insertText(" ", QTextCharFormat())
            end.insertText("\u21a9", back)
    cursor.endEditBlock()
    return sorted(numbers.values())


def _anchor_notes(doc: QTextDocument, count: int) -> None:
    """Each note's place, for its mark's link to go to: on the note's first
    letter, which keeps its color (after _style_body)."""
    found = 0
    for block in iter_blocks(doc):
        text_list = block.textList()
        if text_list is None or text_list.format().style() != QTextListFormat.Style.ListDecimal:
            continue
        if not block.text().endswith("\u21a9"):
            continue
        found += 1
        if found > count:
            break
        first = QTextCursor(block)
        first.movePosition(QTextCursor.MoveOperation.NextCharacter,
                           QTextCursor.MoveMode.KeepAnchor)
        place = QTextCharFormat()
        place.setAnchor(True)
        place.setAnchorNames([f"fn-{found}"])
        first.mergeCharFormat(place)


def article_document(markdown: str, article: Article, *, names: Optional[NameLookup] = None,
                     media_names: Optional[MediaNames] = None,
                     dark: bool = False, parent=None) -> QTextDocument:
    """The article as readers will see it."""
    colors = palette(dark)
    # The body, rendered from exactly the published Markdown.
    doc = body_document(markdown, names=names, media_names=media_names, dark=dark,
                        parent=parent)

    # The header goes in front: cover, title, summary, byline, a rule. A
    # new block is opened before the body's first one, which keeps its own
    # format (a heading stays a heading).
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    cursor.insertBlock(cursor.blockFormat(), cursor.charFormat())
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    head = QTextBlockFormat()
    head.setBottomMargin(10)
    cursor.setBlockFormat(head)
    if article.image:
        image = QTextImageFormat()
        image.setName(article.image)
        image.setWidth(COLUMN_WIDTH)
        cursor.insertImage(image)
        cursor.insertBlock(head)
    cursor.insertText(article.title or _("Untitled"),
                      _char(size=24, bold=True, color=colors.text))
    if article.summary:
        cursor.insertBlock(head)
        cursor.insertText(article.summary, _char(size=14, color=colors.muted))
    minutes = reading_minutes(markdown)
    byline = [part for part in (
        article.author, _date_text(article.published_at),
        ngettext("{n} min read", "{n} min read", minutes).format(n=minutes)) if part]
    cursor.insertBlock(head)
    cursor.insertText(" · ".join(byline), _char(size=11, color=colors.muted))
    rule = QTextBlockFormat()
    rule.setProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth, 1)
    rule.setBottomMargin(16)
    cursor.insertBlock(rule, QTextCharFormat())

    if article.tags:
        end = QTextCursor(doc)
        end.movePosition(QTextCursor.MoveOperation.End)
        tags = QTextBlockFormat()
        tags.setTopMargin(16)
        end.insertBlock(tags)
        end.insertText("   ".join(f"#{t}" for t in article.tags),
                       _char(size=11, color=colors.muted))
    return doc


# --------------------------------------------------------------------------- #
# Notes                                                                        #
# --------------------------------------------------------------------------- #

def is_media(url: str) -> bool:
    path = QUrl(url).path().lower()
    return path.endswith(MEDIA_EXTENSIONS)


def split_media(text: str) -> tuple:
    """``(text, media)``: the note's text with the image addresses that end
    it taken out, and every image address in it, in order."""
    media = [m.group(0) for m in _URL.finditer(text) if is_media(m.group(0))]
    trimmed = text.rstrip()
    while True:
        last = trimmed.split()[-1] if trimmed.split() else ""
        if last and last in media:
            trimmed = trimmed[: len(trimmed) - len(last)].rstrip()
        else:
            break
    return trimmed, media


def note_html(text: str, *, author: str = "", names: Optional[NameLookup] = None,
              media_names: Optional[MediaNames] = None, dark: bool = False) -> str:
    """The note as HTML for a QTextBrowser: plain text, links colored."""
    colors = palette(dark)
    body, media = split_media(text)
    # Three or more line breaks show as two in every app.
    body = re.sub(r"\n{3,}", "\n\n", body)
    pieces = []
    last = 0
    pattern = re.compile(f"{_REFERENCE.pattern}|{_URL.pattern}|{_HASHTAG.pattern}")
    for match in pattern.finditer(body):
        pieces.append(html.escape(body[last:match.start()]))
        token = match.group(0)
        if token.startswith("nostr:"):
            label = reference_label(token, names)
            pieces.append(f'<span style="color:{colors.link};">{html.escape(label)}</span>')
        elif token.startswith("#"):
            pieces.append(f'<span style="color:{colors.link};">{html.escape(token)}</span>')
        else:
            href = html.escape(token, quote=True)
            words = player_label(token, media_names)
            if words:
                # Where the apps show its player: the card, on a line of its own.
                pieces.append(f'</p><p><a href="{href}"><img src="'
                              f'{html.escape(player_card_name(token), quote=True)}" '
                              f'alt="{html.escape(words, quote=True)}" '
                              f'width="{COLUMN_WIDTH}"></a></p>'
                              f'<p style="white-space:pre-wrap; line-height:140%;">')
            else:
                pieces.append(f'<a href="{href}" '
                              f'style="color:{colors.link};">{html.escape(token)}</a>')
        last = match.end()
    pieces.append(html.escape(body[last:]))
    images = "".join(
        f'<p><img src="{html.escape(url, quote=True)}" width="{COLUMN_WIDTH}"></p>'
        for url in media)
    name = (f'<p style="margin-bottom:6px;"><b>{html.escape(author)}</b></p>'
            if author else "")
    return (f'<div style="color:{colors.text}; font-size:13.5pt;">{name}'
            f'<p style="white-space:pre-wrap; line-height:140%;">{"".join(pieces)}</p>'
            f'{images}</div>')


def note_document(text: str, *, author: str = "", names: Optional[NameLookup] = None,
                  media_names: Optional[MediaNames] = None,
                  dark: bool = False, parent=None) -> QTextDocument:
    doc = QTextDocument(parent)
    doc.setDocumentMargin(0)
    doc.setHtml(note_html(text, author=author, names=names, media_names=media_names,
                          dark=dark))
    return doc


# --------------------------------------------------------------------------- #
# The widget                                                                   #
# --------------------------------------------------------------------------- #

def scale_text(doc: QTextDocument, factor: float) -> None:
    """Every size of text in ``doc`` times ``factor``: a preview at the app's
    text size (text_size.py). The sizes are the documents' own (a heading
    stays larger than the body), so they are scaled where they are set."""
    if factor == 1:
        return
    default = doc.defaultFont()
    if default.pointSizeF() > 0:
        default.setPointSizeF(default.pointSizeF() * factor)
        doc.setDefaultFont(default)
    cursor = QTextCursor(doc)
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            size = fragment.charFormat().fontPointSize()
            if fragment.isValid() and size > 0:
                cursor.setPosition(fragment.position())
                cursor.setPosition(fragment.position() + fragment.length(),
                                   QTextCursor.MoveMode.KeepAnchor)
                scaled = QTextCharFormat()
                scaled.setFontPointSize(size * factor)
                cursor.mergeCharFormat(scaled)
            it += 1


class NostrPreview(QTextBrowser):
    """A read-only view of a note or an article as readers will see it.

    The text sits in a column at most COLUMN_WIDTH wide, centered. Web
    links open in the browser; images come from ``images`` (a lookup that
    answers from what is already at hand, never the network) and show as
    an empty frame otherwise.
    """

    def __init__(self, *, images: Optional[ImageLookup] = None,
                 media_names: Optional[MediaNames] = None, dark: bool = False,
                 play: Optional[Callable[[str], None]] = None, parent=None) -> None:
        super().__init__(parent)
        self._images = images
        self._media_names = media_names
        self._dark = dark
        self._play = play
        # The text size the shown document is at (scale_text).
        self._scale = 1.0
        text_size().changed.connect(self._follow_text_size)
        # Links are followed here: audio or video plays in the app (``play``),
        # any other link opens where the system opens it.
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._follow_link)
        self.setAccessibleName(_("Preview"))
        self._apply_colors()

    def _follow_link(self, url: QUrl) -> None:
        address = url.toString()
        if self._play is not None and player_label(address, None):
            self._play(address)
        else:
            QDesktopServices.openUrl(url)

    def _apply_colors(self) -> None:
        colors = palette(self._dark)
        self.setStyleSheet(f"QTextBrowser {{ background: {colors.background}; "
                           f"color: {colors.text}; border: none; }}")

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self._apply_colors()

    def show_article(self, markdown: str, article: Article, *,
                     names: Optional[NameLookup] = None) -> None:
        self._show(article_document(markdown, article, names=names,
                                    media_names=self._media_names, dark=self._dark,
                                    parent=self))

    def show_markdown(self, markdown: str, *, names: Optional[NameLookup] = None) -> None:
        """Markdown as it reads, without an article's header."""
        self._show(body_document(markdown, names=names, media_names=self._media_names,
                                 dark=self._dark, parent=self))

    def show_note(self, text: str, *, author: str = "",
                  names: Optional[NameLookup] = None) -> None:
        self._show(note_document(text, author=author, names=names,
                                 media_names=self._media_names, dark=self._dark,
                                 parent=self))

    def paintEvent(self, event) -> None:
        # Under the text, as the editor draws them: a box behind each code
        # block and a bar beside each quote (text_look).
        painter = QPainter(self.viewport())
        text_look.paint_code_boxes(self, painter, QColor(palette(self._dark).code_background))
        text_look.paint_quote_bars(self, painter, text_look.quote_bar_color(self._dark))
        painter.end()
        super().paintEvent(event)

    def _show(self, doc: QTextDocument) -> None:
        # The document's parent is this view, so its images are asked for
        # through loadResource below; the one it replaces goes.
        previous = self.document()
        self._scale = 1.0
        # Photo groups sized as readers size them, from the pictures this
        # view loads (photo_group_view).
        photo_group_view.install(doc, photo_group_view.resource_sizes(doc))
        self.setDocument(doc)
        # Qt deletes a document the view made itself; one made here goes now.
        if (previous is not doc and shiboken6.isValid(previous)
                and previous.parent() is self):
            previous.deleteLater()
        self._follow_text_size()

    def _follow_text_size(self, *_args) -> None:
        """The shown document at the app's text size, its column as wide in
        words as at the actual size."""
        target = text_size().scale()
        if target != self._scale:
            scale_text(self.document(), target / self._scale)
            self._scale = target
        self._center_column()

    def loadResource(self, kind: int, url: QUrl):
        # A picture as wide as the page (#wide) is the picture without it.
        url = photo_group.without_wide_url(url)
        if kind == QTextDocument.ResourceType.ImageResource.value and self._images:
            image = self._images(url.toString())
            if image is not None and not image.isNull():
                return image
        if kind == QTextDocument.ResourceType.ImageResource.value:
            placeholder = QImage(COLUMN_WIDTH, COLUMN_WIDTH * 9 // 16, QImage.Format.Format_RGB32)
            placeholder.fill(QColor(palette(self._dark).hairline))
            # Its shape is not the picture's: a group lays it out at 3:2.
            return photo_group_drawing.mark_placeholder(placeholder)
        return super().loadResource(kind, url)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._center_column()

    def _center_column(self) -> None:
        """Keep the text in a readable column, as the reading apps do, and
        no image wider than it."""
        widest = round(COLUMN_WIDTH * self._scale)
        spare = max(0, self.width() - widest - 32)
        side = spare // 2 + 16
        self.setViewportMargins(side, 16, side, 16)
        column = max(120, min(widest, self.width() - 2 * side - 24))
        # Photo groups follow the column by themselves (below 600 pixels
        # as on a phone, strips one picture a line: nothing here swipes);
        # their rows are never taller than their share of the view.
        photo_group_view.set_screen_height(self.document(), self.viewport().height())
        self._fit_images(column)

    def _fit_images(self, width: int) -> None:
        doc = self.document()
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                # A group's pictures are sized by its layout
                # (photo_group_view).
                if fragment.isValid() and fmt.isImageFormat() \
                        and not fmt.hasProperty(photo_group.UNIT):
                    image = fmt.toImageFormat()
                    if int(image.width()) != width:
                        image.setWidth(width)
                        image.clearProperty(QTextFormat.Property.ImageHeight)
                        cursor = QTextCursor(doc)
                        cursor.setPosition(fragment.position())
                        cursor.setPosition(fragment.position() + fragment.length(),
                                           QTextCursor.MoveMode.KeepAnchor)
                        cursor.setCharFormat(image)
                it += 1
            block = block.next()
