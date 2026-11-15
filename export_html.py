#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Handrolled HTML5 exporter for the editor's QTextDocuments.

Qt's own toHtml() emits HTML 4.0 with proprietary -qt-* attributes and an
inline style on every paragraph; bullets stay literal "• " text and images
point at machine-local cache paths. This module replaces it with clean,
semantic, fully self-contained HTML:

- bullets typed as text become real nested <ul><li> lists, and real
  lists (bulleted, numbered, checklists) stay what they are,
- headings become <h1> to <h6>,
- bold/italic/underline/strikethrough become <strong>/<em>/<u>/<s>,
  inline code becomes <code>, a link an <a href>,
- colors stay inline styles (Qt ignores <style> blocks on setHtml, so
  inline is the only representation that survives reopening the file),
- images are embedded as base64 data URIs so a shared file carries its
  media with it; the original upload URL rides along in data-source-url,
- a photo group (photo_group) and a picture alone are written as
  STANDUP writes them (photo_page): the vendored engine's markup, the
  pictures as data: URIs (web pictures too, when the caller fetched
  them), and STANDUP's stylesheet and scripts (web/) inside the page; on
  a phone a group's runs swipe, and a picture opens in STANDUP's full
  view,
- audio and video become the browser's own players (<audio>, <video>),
  their bytes embedded the same way up to MEDIA_EMBED_LIMIT and played
  from the media server beyond it; inside each, a link to the file, which
  is also what the editor reads back when the page is opened.

A document can name any path on the machine, so image bytes are read
only through an ImageRootPolicy built from the caller's image_roots.
The default is empty, which reads nothing: a caller that forgets the
argument exports a placeholder instead of leaking a file.

normalize_after_set_html() tidies what Qt's HTML reader leaves behind
when a file is opened: the empty paragraphs this exporter writes as
&nbsp;, and stray whitespace from pretty-printed HTML. Lists stay real
lists: the editor edits them as such.
"""

import base64
import html
import itertools
import re
from typing import List, Tuple

from PySide6.QtGui import (
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextImageFormat,
    QTextListFormat,
)

import export_pictures
import media_files
import photo_group
import photo_group_find
import photo_layout
import photo_page
import rich_text
from constants import DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, MONO_FONT
from fonts import names_monospace
from i18n import _
from doc_walk import (
    bullet_depth,
    doc_length,
    doc_offset,
    iter_block_runs,
    iter_blocks,
    parse_bullet_line,
    skip_prefix,
)
# Re-exported: the sniffers moved to image_safety because the decode
# boundary needs them too, and main_window plus the tests import them
# from here.
from image_safety import (  # noqa: F401
    DECODABLE_MIMES,
    ImageRootPolicy,
    declared_size,
    is_portable_image_source,
    sniff_image_ext,
    sniff_image_mime,
)

GENERATOR = "minimal texteditor"

# Object-replacement character marking an inline image fragment.
_OBJ = "\ufffc"

# Qt uses U+2028 for Shift+Enter line breaks inside a block.
_LINE_SEP = "\u2028"

# Audio and video up to this size travel inside the page, as its pictures
# do; a larger file plays from its media server (a page of hundreds of
# megabytes opens slowly in every browser and fills every mailbox).
MEDIA_EMBED_LIMIT = 64 * 1024 * 1024


def image_data_uri(data: bytes) -> str | None:
    """Base64 data URI for image bytes, or None when unrecognized."""
    mime = sniff_image_mime(data)
    if mime is None:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _font_stack() -> str:
    """Monospace stack led by the app's font, without duplicate entries."""
    fonts = ["ui-monospace", f"'{MONO_FONT}'", "Menlo", "Consolas",
             "'Noto Sans Mono'", "monospace"]
    seen = set()
    unique = []
    for f in fonts:
        key = f.strip("'").lower()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return ", ".join(unique)


def _head_css() -> str:
    # The page CSS is for browsers. Everything that must round-trip (colors,
    # pre-wrap paragraphs) is also emitted inline. Qt does apply some of it
    # when the file is reopened: the body margin moves every paragraph in,
    # and normalize_after_set_html takes it off again.
    return f"""    :root {{ color-scheme: light dark; }}
    body {{
      margin: 2rem auto;
      max-width: 48rem;
      padding: 0 1rem;
      font-family: {_font_stack()};
      font-size: 14px;
      line-height: 1.5;
      background: {LIGHT_BG};
      color: {LIGHT_FG};
    }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: {DARK_BG}; color: {DARK_FG}; }}
    }}
    p {{ margin: 0; }}
    ul {{ margin: 0; padding-left: 1.5em; }}
    ul ul {{ list-style-type: circle; }}
    ul ul ul {{ list-style-type: square; }}
    img, video {{ max-width: 100%; height: auto; }}
    audio, video {{ display: block; margin: 0.5rem 0; }}
    audio {{ width: 100%; }}
    li.checked, li.unchecked {{ list-style: none; }}
    li.checked::before {{ content: "\\2611\\00a0"; }}
    li.unchecked::before {{ content: "\\2610\\00a0"; }}
    code {{ font-family: {_font_stack()}; }}"""


def _render_image(img_fmt, source_url_for, policy, asset_resolver,
                  remote_pictures=None) -> str:
    """One <img>, its bytes read as every export reads a picture
    (export_pictures.picture_bytes): the resolver, a web picture the
    caller gathered, then the policy.

    Order matters: an app-created asset is named by a key only the
    resolver understands, a legacy or foreign image is named by a path
    only the policy may read, and an image whose name is itself an
    address (a data: URI or a third-party URL) is its own answer when
    its bytes are not at hand.
    """
    name = img_fmt.name()
    # A picture's #wide (photo_group) is how it is laid out, not where
    # it is: it is read without it.
    plain = photo_group.without_wide(name)
    alt = str(img_fmt.property(QTextImageFormat.ImageAltText) or "")
    attrs = f' alt="{html.escape(alt, quote=True)}"'
    for prop, value in (("width", img_fmt.width()), ("height", img_fmt.height())):
        if value and value > 0:
            attrs += f' {prop}="{int(value)}"'

    resolved = asset_resolver(plain) if asset_resolver else None
    if _is_media_asset(resolved):
        return _render_media(alt, resolved)

    src, _size, source_url = _picture_source(plain, policy, resolved, remote_pictures,
                                             source_url_for)
    if source_url:
        attrs += f' data-source-url="{html.escape(source_url, quote=True)}"'
    if src is None:
        return f"<em>{html.escape(_('[image unavailable]'))}</em>"
    return f'<img src="{html.escape(src, quote=True)}"{attrs}>'


def _is_media_asset(resolved) -> bool:
    """Whether an asset the resolver gave is audio or video (its card is
    a picture in the document, its page a player)."""
    return media_files.kind_of(str(getattr(resolved, "mime", "") or "")) is not None


def _picture_source(plain: str, policy, resolved, remote_pictures, source_url_for):
    """What a page shows for the picture ``plain`` (its name without
    #wide): ``(src, (w, h) or None, source address)``. The one way every
    picture of an export is read, alone or in a photo group.

    - Its bytes inside the page, as a data: URI, read as every export
      reads a picture (export_pictures.picture_bytes: the app's own
      pictures through ``resolved``, a web picture the caller gathered, a
      file ``policy`` may read; never larger than MAX_PICTURE_BYTES), with
      the pixel size their header declares. Never bytes the decode policy
      refuses for their size (a decompression bomb).
    - Else the name itself when it is an address (a data: URI the app
      does not read, a foreign URL): media this app did not create comes
      out as the document holds it.
    - Else the address the picture was uploaded to: the bytes are gone
      from the cache, and the original is better than nothing even though
      it breaks strict self-containment.
    - Else nothing: the page says the picture is unavailable.
    """
    data, came_from = export_pictures.picture_bytes(
        plain, policy, asset_resolver=lambda _name: resolved, remote_pictures=remote_pictures)
    source = came_from or (source_url_for(plain) if source_url_for else None)
    mime = sniff_image_mime(data) if data else None
    if mime in DECODABLE_MIMES:
        size = declared_size(data)
        if size is None:
            return None, None, source
        return image_data_uri(data), photo_layout.picture_size(size.width(), size.height()), source
    if mime is not None:
        return image_data_uri(data), None, source          # a kind only a browser shows (SVG)
    if is_portable_image_source(plain):
        return plain, None, source
    return source, None, source


def _render_media(alt: str, resolved) -> str:
    """Audio or video as the browser's player: its bytes inside the page
    when they are here and not too large, else its media server's address;
    nothing when neither is known (never a picture of it, never a
    placeholder). The link inside is for a reader without a player, and
    for the editor reopening the page."""
    mime = str(getattr(resolved, "mime", "") or "")
    data = bytes(getattr(resolved, "data", b"") or b"")
    remote = str(getattr(resolved, "remote_url", "") or "")
    if remote:
        remote = media_files.address_with_extension(remote, mime)
    if data and len(data) <= MEDIA_EMBED_LIMIT:
        src = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    else:
        src = remote
    if not src:
        return ""
    tag = "video" if media_files.kind_of(mime) == media_files.VIDEO else "audio"
    name = alt or (remote.rsplit("/", 1)[-1] if remote else "")
    words = html.escape(media_files.link_words(mime, name))
    fallback = f'<a href="{html.escape(remote, quote=True)}">{words}</a>' if remote else words
    source = f' data-source-url="{html.escape(remote, quote=True)}"' if remote else ""
    return (f'<{tag} controls preload="metadata" src="{html.escape(src, quote=True)}"'
            f' title="{html.escape(name, quote=True)}"{source}>{fallback}</{tag}>')


def _render_text_run(text: str, fmt: QTextCharFormat, heading: bool = False) -> str:
    out = html.escape(text).replace(_LINE_SEP, "<br>")
    if fmt.fontFixedPitch():
        out = f"<code>{out}</code>"
    if fmt.fontStrikeOut():
        out = f"<s>{out}</s>"
    if fmt.fontUnderline() and not fmt.isAnchor():
        out = f"<u>{out}</u>"
    if fmt.fontItalic():
        out = f"<em>{out}</em>"
    if fmt.fontWeight() > 400 and not heading:
        # A heading is bold by its own format; that is not emphasis.
        out = f"<strong>{out}</strong>"
    if fmt.hasProperty(QTextCharFormat.ForegroundBrush) and not fmt.isAnchor():
        # A link's color is how links look, not a color of the text.
        color = fmt.foreground().color().name()
        out = f'<span style="color:{color}">{out}</span>'
    return out


# A link's scheme, as a browser reads it (WHATWG URL): letters, digits,
# "+", "-" and "." after a letter, before the first ":".
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_LINK_SCHEMES = ("http:", "https:", "mailto:")


def safe_href(href: str) -> str:
    """``href`` when a page may link to it: a web address, an email
    address (mailto:) or one relative to the page; else "" (javascript:,
    data:, file: and every other scheme), and the words stay as text. Read
    as a browser reads it: without the spaces and control characters
    around it, and without the tabs and line breaks inside it."""
    plain = re.sub(r"[\t\n\r]", "", href).strip("".join(map(chr, range(0x21))))
    scheme = _SCHEME.match(plain)
    if scheme is None or scheme.group(0).lower() in _LINK_SCHEMES:
        return href
    return ""


def _render_runs(runs, render_image, heading: bool = False) -> str:
    parts = []
    link = ""            # the address of the <a> open in parts, if any
    for text, fmt in runs:
        href = safe_href(fmt.anchorHref()) if fmt.isAnchor() else ""
        if href != link:
            if link:
                parts.append("</a>")
            if href:
                parts.append(f'<a href="{html.escape(href, quote=True)}">')
            link = href
        if fmt.isImageFormat():
            parts.append(render_image(fmt.toImageFormat()))
            continue
        # Defensive: strip stray object-replacement chars from plain runs.
        text = text.replace(_OBJ, "")
        if text:
            parts.append(_render_text_run(text, fmt, heading))
    if link:
        parts.append("</a>")
    return "".join(parts)


_ORDERED_STYLES = {QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha,
                   QTextListFormat.Style.ListUpperAlpha, QTextListFormat.Style.ListLowerRoman,
                   QTextListFormat.Style.ListUpperRoman}


def _list_item_class(block) -> str:
    marker = block.blockFormat().marker()
    if marker == QTextBlockFormat.MarkerType.Checked:
        return ' class="checked"'
    if marker == QTextBlockFormat.MarkerType.Unchecked:
        return ' class="unchecked"'
    return ""


def _needs_pre_wrap(text: str) -> bool:
    """Leading or consecutive spaces would collapse in normal HTML flow."""
    return text.startswith(" ") or "  " in text


def _caption_html(unit, render_image) -> str:
    """A unit's caption as the readers write it: the words of its
    emphasis, not in italic (the figure shows it as a caption)."""
    runs = []
    for text, fmt in unit.caption_runs():
        fmt.setFontItalic(False)
        runs.append((text, fmt))
    return _render_runs(runs, render_image)


def _figure_of(unit, picture_of, render_image, place: int) -> photo_page.Figure:
    """A picture of the document (a unit of photo_group) as a page shows
    it, the ``place``-th of the page's."""
    src, size, source = picture_of(unit.name)
    caption = _caption_html(unit, render_image) if unit.caption_text.strip() else ""
    href = safe_href(unit.image.anchorHref()) if unit.image.isAnchor() else ""
    return photo_page.Figure(src, size, source, unit.alt, caption, unit.caption_text, unit.wide,
                             href, place)


def _photo_units(doc, is_media) -> List[Tuple[int, int, list, bool]]:
    """The photo groups of ``doc`` and its pictures alone, in order, each
    as ``(first position, last position, units, whether a group)``. A
    picture alone is a picture paragraph at the top of the document with
    its caption (photo_group_find, as md.py reads one), shown at no size
    of its own (a picture an HTML file gave a size keeps it, as before)
    and not audio or video (``is_media``, by its name)."""
    groups = [(group.start, group.end, group.units, True)
              for group in photo_group.groups_in(doc)]
    alone = [(run.first, run.end, run.units, False)
             for run in photo_group_find.find_runs(doc)
             if len(run.units) == 1 and not run.groups and _at_own_size(run.units[0])
             and not is_media(run.units[0].name)]
    return sorted(groups + alone, key=lambda unit: unit[0])


def _at_own_size(unit) -> bool:
    image = unit.image.toImageFormat()
    return image.width() <= 0 and image.height() <= 0


def document_to_html(doc, title: str = "", source_url_for=None, *,
                     image_roots=(), asset_resolver=None, remote_pictures=None) -> str:
    """Serialize a QTextDocument to a self-contained semantic HTML5 page.

    source_url_for: optional callback mapping a local image path to its
    original https URL, used only for the data-source-url provenance
    attribute and as a last-resort src when the cached bytes are gone.

    image_roots: directories a local image name may be read from. Empty
    (the default) reads nothing.

    asset_resolver: optional callback mapping an image name to an object
    exposing ``data`` / ``remote_url``, used for images this app created
    and holds in its own cache.

    remote_pictures: the bytes of the document's web pictures, by address
    (export_pictures.gather), carried in the page as the rest.
    """
    policy = ImageRootPolicy(image_roots)

    def render_image(img_fmt) -> str:
        return _render_image(img_fmt, source_url_for, policy, asset_resolver, remote_pictures)

    def resolved(name: str):
        return asset_resolver(photo_group.without_wide(name)) if asset_resolver else None

    def picture_of(name: str):
        return _picture_source(photo_group.without_wide(name), policy, resolved(name),
                               remote_pictures, source_url_for)

    def is_media(name: str) -> bool:
        return _is_media_asset(resolved(name))

    # Photo groups and pictures alone: written whole where their first
    # paragraph is; every paragraph they take up is theirs (a group's
    # tables, a picture's caption).
    photos = _photo_units(doc, is_media)
    pending = list(photos)
    photos_end = -1
    places = itertools.count(1)
    groups = sum(1 for _first, _end, _units, grouped in photos if grouped)

    body: list[str] = []
    # List markup is emitted compactly (no newlines): whitespace text nodes
    # inside <li> would be re-parsed by Qt as trailing spaces on the item.
    list_buf: list[str] = []
    depth = 0             # how many <ul> levels are open
    li_open: list[bool] = [False]  # index = level, [0] unused
    # Real lists: the tags open, outermost first, and whether each level
    # has an <li> open.
    real_tags: list[str] = []
    real_li: list[bool] = []
    real_buf: list[str] = []

    def close_real(target: int):
        while len(real_tags) > target:
            if real_li.pop():
                real_buf.append("</li>")
            real_buf.append(f"</{real_tags.pop()}>")
        if target == 0 and real_buf:
            body.append("".join(real_buf))
            real_buf.clear()

    def real_item(block, text_list):
        level = max(1, text_list.format().indent())
        ordered = text_list.format().style() in _ORDERED_STYLES
        tag = "ol" if ordered else "ul"
        close_real(min(len(real_tags), level))       # leave deeper levels
        if len(real_tags) == level and real_tags[-1] != tag:
            # Another kind at the same depth is a list of its own.
            close_real(level - 1)
        while len(real_tags) < level:
            if real_tags and not real_li[-1]:
                # A depth jump: an item that only holds the nested list.
                real_buf.append("<li>")
                real_li[-1] = True
            opening = tag if len(real_tags) == level - 1 else "ul"
            start = text_list.format().start() if hasattr(text_list.format(), "start") else 1
            first = text_list.itemNumber(block) == 0
            extra = (f' start="{start}"' if opening == "ol" and first and start not in (0, 1)
                     else "")
            real_buf.append(f"<{opening}{extra}>")
            real_tags.append(opening)
            real_li.append(False)
        if real_li[-1]:
            real_buf.append("</li>")
        real_buf.append(f"<li{_list_item_class(block)}>")
        real_li[-1] = True
        real_buf.append(_render_runs(list(iter_block_runs(block)), render_image))

    def close_to(target: int):
        nonlocal depth
        while depth > target:
            if li_open[depth]:
                list_buf.append("</li>")
                li_open[depth] = False
            list_buf.append("</ul>")
            depth -= 1
        if target == 0 and list_buf:
            body.append("".join(list_buf))
            list_buf.clear()

    quotes = 0            # how many <blockquote> levels are open

    def quote_to(level: int):
        nonlocal quotes
        if level == quotes:
            return
        close_to(0)
        close_real(0)
        while quotes > level:
            body.append("</blockquote>")
            quotes -= 1
        while quotes < level:
            body.append("<blockquote>")
            quotes += 1

    for block in iter_blocks(doc):
        if pending and block.position() >= pending[0][0]:
            _first, photos_end, units, grouped = pending.pop(0)
            quote_to(0)
            close_to(0)
            close_real(0)
            figures = [_figure_of(unit, picture_of, render_image, next(places)) for unit in units]
            body.append(photo_page.group_figures_html(figures, groups) if grouped
                        else photo_page.figure_alone_html(figures[0]))
            continue
        if block.position() <= photos_end:
            continue
        text = block.text()
        text_list = block.textList()
        quote_to(rich_text.quote_depth(block))
        if text_list is not None:
            close_to(0)
            real_item(block, text_list)
            continue
        close_real(0)
        if rich_text.is_divider(block):
            close_to(0)
            body.append("<hr>")
            continue
        spaces, has_bullet = parse_bullet_line(text)

        if has_bullet:
            d = bullet_depth(spaces)
            close_to(d)
            while depth < d:
                list_buf.append("<ul>")
                depth += 1
                if len(li_open) <= depth:
                    li_open.append(False)
                if depth < d:
                    # Intermediate level of a depth jump: its item exists
                    # only to hold the nested list.
                    list_buf.append("<li>")
                    li_open[depth] = True
            if li_open[d]:
                list_buf.append("</li>")
            list_buf.append("<li>")
            li_open[d] = True

            runs = skip_prefix(list(iter_block_runs(block)), spaces + len("• "))
            list_buf.append(_render_runs(runs, render_image))
            continue

        close_to(0)
        if not text and block.begin().atEnd():
            # &nbsp; is the only form Qt re-parses as exactly one (visually
            # blank) paragraph; normalize_after_set_html turns it back
            # into a truly empty block on load.
            body.append("<p>&nbsp;</p>")
            continue
        heading = min(6, block.blockFormat().headingLevel())
        content = _render_runs(list(iter_block_runs(block)), render_image, heading > 0)
        if heading:
            body.append(f"<h{heading}>{content}</h{heading}>")
        elif _needs_pre_wrap(text):
            body.append(f'<p style="white-space:pre-wrap">{content}</p>')
        else:
            body.append(f"<p>{content}</p>")

    close_to(0)
    close_real(0)
    quote_to(0)

    page_title = html.escape(title or _("Untitled"))
    body_html = "\n".join(body)
    head_css = _head_css() + (photo_page.group_head() if photos else "")
    scripts = photo_page.group_scripts() if photos else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="generator" content="{GENERATOR}">
  <title>{page_title}</title>
  <style>
{head_css}
  </style>
</head>
<body>
{body_html}
{scripts}</body>
</html>
"""


def _quote_depths(doc) -> None:
    """Qt's reader marks every <blockquote> as the first quote level and
    only moves an inner one further in; a page's style sheet can move
    every paragraph in as well (the body margin of the pages this module
    writes). The depth is read back from how far each quoted paragraph
    sits in from the others, and the page's own margin is taken off."""
    blocks = list(iter_blocks(doc))
    quoted = [b for b in blocks if rich_text.quote_depth(b)]
    plain = [b.blockFormat().leftMargin() for b in blocks
             if not rich_text.quote_depth(b) and b.textList() is None]
    if plain:
        base = min(plain)
    elif quoted:
        base = min(b.blockFormat().leftMargin() for b in quoted) - rich_text.QUOTE_INDENT
    else:
        return
    base = max(0.0, base)
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    if base:
        for block in blocks:
            fmt = block.blockFormat()
            if fmt.leftMargin() >= base:
                fmt.setLeftMargin(fmt.leftMargin() - base)
                QTextCursor(block).setBlockFormat(fmt)
    for block in quoted:
        depth = max(1, round(block.blockFormat().leftMargin() / rich_text.QUOTE_INDENT))
        rich_text.set_quote_depth(block, depth)
    cursor.endEditBlock()


# What a page sets about its text that this editor has its own choice for:
# the font and the size are the person's (Settings, View > Zoom In).
_PAGE_FONT = (QTextFormat.Property.FontFamilies, QTextFormat.Property.FontFamily,
              QTextFormat.Property.FontPointSize, QTextFormat.Property.FontPixelSize)


def _drop_page_fonts(doc) -> None:
    """Take off the fonts and sizes the page set, as a paste does (paste.py):
    the text is in the font and at the size the person chose, and grows
    with the text size, headings staying larger by their own measure (Apple:
    "maintain the relative hierarchy ... when people adjust text sizes").
    Nothing is lost by it: the writer keeps no fonts or sizes. This file's
    own CSS for browsers (_head_css) is one such page font.

    Words in a monospace font of their own, apart from the page's, are code
    (a code element, or code this editor wrote); text all in one monospace
    font is the page's font."""
    runs = []
    page_counts = {}
    for block in iter_blocks(doc):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fragment.isValid() and not fmt.isImageFormat():
                families = tuple(fmt.fontFamilies() or ())
                runs.append((fragment.position(), fragment.length(), fmt, families))
                page_counts[families] = page_counts.get(families, 0) + fragment.length()
            it += 1
    if not runs:
        return
    page = max(page_counts, key=page_counts.get)
    cursor = QTextCursor(doc)
    for position, length, fmt, families in runs:
        if not any(fmt.hasProperty(prop) for prop in _PAGE_FONT):
            continue
        for prop in _PAGE_FONT:
            fmt.clearProperty(prop)
        if families != page and names_monospace(families):
            fmt.setFontFixedPitch(True)
            fmt.setFontFamilies(rich_text.CODE_FAMILIES)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(fmt)


def open_page(editor, page: str) -> None:
    """Show the HTML file ``page`` in ``editor`` (a QTextEdit) as the text
    it holds: the figures of a page this app wrote are the paragraphs they
    were written from (photo_page), its groups are groups again, and what
    Qt's reader leaves behind is tidied (normalize_after_set_html)."""
    editor.setHtml(photo_page.page_as_paragraphs(page))
    normalize_after_set_html(editor.document())
    photo_group_find.fold(editor.document())


def normalize_after_set_html(doc) -> None:
    """Tidy a document Qt's HTML reader just read (a file being opened, a
    crash backup restored).

    Empty paragraphs come back empty: this exporter writes them as
    <p>&nbsp;</p>, because Qt drops <p></p> and doubles <p><br></p>.
    Whitespace that pretty-printed HTML leaves at the end of a list item,
    or after </html>, is trimmed. Lists stay real lists. Quotes get their
    depth back (see _quote_depths). The page's fonts and sizes go, code
    staying code (see _drop_page_fonts).
    """
    _quote_depths(doc)
    _drop_page_fonts(doc)
    NBSP = "\u00a0"
    nbsp_targets = []
    items = []
    for block in iter_blocks(doc):
        if block.textList() is not None:
            items.append(block.position())
        elif block.text() == NBSP:
            nbsp_targets.append(block.position())

    # Bottom-up, so earlier positions stay valid while we change text.
    for pos in reversed(sorted(nbsp_targets + items)):
        block = doc.findBlock(pos)
        text = block.text()
        keep = 0 if text == NBSP and block.textList() is None else len(text.rstrip())
        if keep < len(text):
            cursor = QTextCursor(block)
            # (A place in the text, as a place in the document.)
            cursor.setPosition(block.position() + doc_offset(text, keep))
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()

    # Qt folds whitespace after </html> (e.g. the file's final newline) into
    # the last paragraph as a trailing space; trim it.
    last = doc.lastBlock()
    text = last.text()
    trimmed = text.rstrip(" " + NBSP)
    if len(trimmed) < len(text):
        cursor = QTextCursor(last)
        cursor.setPosition(last.position() + doc_length(trimmed))
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
