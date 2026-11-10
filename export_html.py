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
- a photo group (photo_group) is written as STANDUP writes it (SPEC 3 of
  the photo grid work): the vendored engine's markup, its pictures as
  data: URIs (web pictures too, when the caller fetched them), and its
  stylesheet and scripts (web/) inside the page; on a phone its runs
  swipe, and a picture opens large,
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
import json
from pathlib import Path

from PySide6.QtGui import (
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextImageFormat,
    QTextListFormat,
)

import media_files
import photo_group
import photo_layout
import rich_text
from constants import DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, MONO_FONT
from fonts import names_monospace
from i18n import _, ngettext
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
    MAX_PICTURE_BYTES,
    ImageRootPolicy,
    data_uri_bytes,
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


def _render_image(img_fmt, source_url_for, policy, asset_resolver) -> str:
    """One <img>, resolving bytes through the resolver then the policy.

    Order matters: an app-created asset is named by a key only the
    resolver understands, a legacy or foreign image is named by a path
    only the policy may read, and an image whose name is itself an
    address (a data: URI or a third-party URL) is its own answer.
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

    source_url = source_url_for(plain) if source_url_for else None
    data = None
    resolved = asset_resolver(plain) if asset_resolver else None
    if media_files.kind_of(str(getattr(resolved, "mime", "") or "")) is not None:
        return _render_media(alt, resolved)

    if resolved is not None and getattr(resolved, "data", b""):
        data = resolved.data
        remote_url = str(getattr(resolved, "remote_url", "") or "")
        if remote_url:
            source_url = remote_url
    if source_url:
        attrs += f' data-source-url="{html.escape(source_url, quote=True)}"'
    if data is None:
        data = policy.read(plain)

    uri = image_data_uri(data) if data else None
    if uri is not None:
        return f'<img src="{uri}"{attrs}>'
    if is_portable_image_source(name):
        # The name is the picture's address: a data: URI already carries
        # the bytes, and a foreign URL is media this app did not create.
        # Both must come out exactly as the document holds them, and a
        # document reopened from HTML names every embedded image this
        # way, so anything else would erase the user's own picture.
        return f'<img src="{html.escape(name, quote=True)}"{attrs}>'
    if source_url:
        # Bytes are gone from the cache; the original URL is better than
        # nothing even though it breaks strict self-containment.
        return f'<img src="{html.escape(source_url, quote=True)}"{attrs}>'
    return f"<em>{html.escape(_('[image unavailable]'))}</em>"


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


def _render_runs(runs, render_image, heading: bool = False) -> str:
    parts = []
    link = ""            # the address of the <a> open in parts, if any
    for text, fmt in runs:
        href = fmt.anchorHref() if fmt.isAnchor() else ""
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


# The page parts of a photo group, vendored from STANDUP (web/).
_WEB = Path(__file__).resolve().parent / "web"


def _group_words() -> dict:
    """The words the page scripts of a group say, in the app's language
    ({k} and {n} are numbers, {alt} a picture's description)."""
    return {
        "photoOf": _("Photo {k} of {n}"),
        "of": _("{k} of {n}"),
        "swipe": _("{n} photos to swipe through"),
        "viewer": _("Full View"),
        "close": _("Close"),
        "original": _("Open Original in a New Tab"),
        "previous": _("Previous Photo"),
        "next": _("Next Photo"),
        "enlarge": _("Enlarge"),
        "enlargeNamed": _("Enlarge: {alt}"),
    }


def _group_head() -> str:
    """The stylesheet of photo groups, with the colours and type of this
    page in the variables it reads."""
    return (f"""
    .article-content {{
      /* As STANDUP's article (article.css): a word longer than its
         picture breaks, and never widens the group past the page. */
      overflow-wrap: break-word;
      --theme-bg-subtle: #f1f0ec;
      --theme-text-muted: #6b6760;
      --theme-text-heading: {LIGHT_FG};
      --theme-accent: #3d5afe;
      --e21-on-accent: #fff;
      --article-link: #2f55d4;
      --article-bleed: 0px;
      --article-gutter: 1rem;
      --e21-ui-font: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    }}
    @media (prefers-color-scheme: dark) {{
      .article-content {{
        --theme-bg-subtle: #2a2a2c;
        --theme-text-muted: #a8a49c;
        --theme-text-heading: {DARK_FG};
        --theme-accent: #8c9eff;
        --e21-on-accent: #111;
        --article-link: #8ab4ff;
      }}
    }}
    .article-content img[data-pg-open] {{ cursor: zoom-in; }}
    .article-content img[data-pg-open]:focus-visible {{ outline: 2px solid var(--article-link); outline-offset: 3px; }}
""" + (_WEB / "photo_groups.css").read_text(encoding="utf-8"))


def _group_scripts() -> str:
    words = json.dumps(_group_words(), ensure_ascii=False).replace("</", "<\\/")
    return (f'<script type="application/json" id="e21-photo-words">{words}</script>\n'
            f'<script>\n{(_WEB / "photo_groups.js").read_text(encoding="utf-8")}</script>\n'
            f'<script>\n{(_WEB / "photo_viewer.js").read_text(encoding="utf-8")}</script>\n')


def _caption_html(caption, render_image) -> str:
    """A caption as the readers write it: the words of its emphasis, not
    in italic (the figure shows it as a caption)."""
    if caption is None:
        return ""
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    QTextCursor(doc).insertFragment(caption)
    runs = []
    for block in iter_blocks(doc):
        for text, fmt in iter_block_runs(block):
            upright = QTextCharFormat(fmt)
            upright.setFontItalic(False)
            runs.append((text, upright))
    return _render_runs(runs, render_image)


def _group_html(group, picture_of, render_image) -> str:
    """One photo group as STANDUP's renderer writes it (md.py,
    _image_group): the engine's cells in Markdown order, each picture a
    figure with its shape and its place, its caption under it."""
    units = group.units
    pictures = [picture_of(unit.name) for unit in units]
    items = [photo_group.item(size, unit.caption_text, unit.wide)
             for unit, (_src, size, _source) in zip(units, pictures)]
    result = photo_layout.layout(items)

    def figure(index: int, properties) -> str:
        unit = units[index]
        src, size, source = pictures[index]
        alt = html.escape(unit.alt, quote=True)
        cls = ("e21-figure" + (" e21-figure--wide" if unit.wide else "")
               + (" e21-pg-cell" if properties is not None else ""))
        style = f"--ar:{size[0]}/{size[1]}" if size else "--ar:3/2"
        if properties:
            style += ";" + photo_layout.style_text(properties)
        provisional = "" if size else " data-provisional"
        if src is None:
            img = f"<em>{html.escape(_('[image unavailable]'))}</em>"
        else:
            img = f'<img src="{html.escape(src, quote=True)}" alt="{alt}"'
            if size:
                img += f' width="{size[0]}" height="{size[1]}"'
            if source:
                img += f' data-source-url="{html.escape(source, quote=True)}"'
            # Its bytes are in the page already: nothing to wait for (and
            # a page printed or saved as PDF shows every picture).
            img += ' decoding="async">' if src.startswith("data:") else \
                ' loading="lazy" decoding="async">'
        caption = _caption_html(unit.caption, render_image) if unit.caption_text.strip() else ""
        caption = f"<figcaption>{caption}</figcaption>" if caption else ""
        return f'<figure class="{cls}" style="{style}"{provisional}>{img}{caption}</figure>'

    track = photo_layout.group_html(photo_layout.group_markup(result, items), figure)
    n = len(units)
    label = html.escape(ngettext("{n} photo", "{n} photos", n).format(n=n), quote=True)
    return (f'<div class="article-content"><div class="e21-imagegroup '
            f'e21-imagegroup--{n if n <= 4 else "many"}" role="group" aria-label="{label}" '
            f'data-layout="{photo_layout.notation(result)}">'
            f'<div class="e21-imagegroup-track">{track}</div></div></div>')


def _group_picture(name: str, policy, asset_resolver, remote_pictures, source_url_for):
    """A group picture's (src, (w, h) or None, source address): its bytes as
    a data: URI when the export may read them, its pixel size from their
    header. Read without its #wide; only from the trusted places
    (``policy``), the app's own pictures, a data: name, or what the caller
    fetched from the web; never larger than MAX_PICTURE_BYTES, never a
    picture the decode policy refuses (a decompression bomb)."""
    plain = photo_group.without_wide(name)
    source = source_url_for(plain) if source_url_for else None
    data = None
    resolved = asset_resolver(plain) if asset_resolver else None
    if resolved is not None and getattr(resolved, "data", b""):
        data = resolved.data
        source = str(getattr(resolved, "remote_url", "") or "") or source
    if data is None and remote_pictures and plain in remote_pictures:
        data = remote_pictures[plain]
        source = plain
    if data is None:
        data = policy.read(plain, limit=MAX_PICTURE_BYTES) or data_uri_bytes(plain)
    if data is not None and len(data) <= MAX_PICTURE_BYTES:
        size = declared_size(data)
        uri = image_data_uri(data) if size is not None else None
        if uri is not None:
            return uri, photo_layout.picture_size(size.width(), size.height()), source
    if is_portable_image_source(plain) and not plain.lower().startswith("data:"):
        return plain, None, source
    return None, None, source


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

    remote_pictures: the bytes of web pictures of the photo groups, by
    address (export_pictures.gather), carried in the page as the rest.
    """
    policy = ImageRootPolicy(image_roots)

    def render_image(img_fmt) -> str:
        return _render_image(img_fmt, source_url_for, policy, asset_resolver)

    def picture_of(name: str):
        return _group_picture(name, policy, asset_resolver, remote_pictures, source_url_for)

    # Photo groups: written whole where their first paragraph is; every
    # paragraph of their tables is theirs.
    groups = photo_group.groups_in(doc)
    pending_groups = list(groups)
    group_end = -1

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
        if pending_groups and block.position() >= pending_groups[0].start:
            group = pending_groups.pop(0)
            quote_to(0)
            close_to(0)
            close_real(0)
            body.append(_group_html(group, picture_of, render_image))
            group_end = group.end
            continue
        if block.position() <= group_end:
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
    head_css = _head_css() + (_group_head() if groups else "")
    scripts = _group_scripts() if groups else ""
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
