#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Handrolled HTML5 exporter for the editor's QTextDocuments.

Qt's own toHtml() emits HTML 4.0 with proprietary -qt-* attributes and an
inline style on every paragraph; bullets stay literal "• " text and images
point at machine-local cache paths. This module replaces it with clean,
semantic, fully self-contained HTML:

- bullets become real nested <ul><li> lists,
- bold/italic/underline become <strong>/<em>/<u>,
- colors stay inline styles (Qt ignores <style> blocks on setHtml, so
  inline is the only representation that survives reopening the file),
- images are embedded as base64 data URIs so a shared file carries its
  media with it; the original upload URL rides along in data-source-url.

A document can name any path on the machine, so image bytes are read
only through an ImageRootPolicy built from the caller's image_roots.
The default is empty, which reads nothing: a caller that forgets the
argument exports a placeholder instead of leaking a file.

normalize_lists_after_set_html() is the inverse half of the round-trip:
after loading any HTML file, real QTextList items are converted back to
the editor's literal "• " bullet convention so the bullet key handlers
keep working.
"""

import base64
import html

from PySide6.QtGui import (
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextImageFormat,
)

from constants import DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, MONO_FONT
from doc_walk import (
    INDENT_STEP,
    bullet_depth,
    iter_block_runs,
    iter_blocks,
    parse_bullet_line,
    skip_prefix,
)
# Re-exported: the sniffers moved to image_safety because the decode
# boundary needs them too, and main_window plus the tests import them
# from here.
from image_safety import (  # noqa: F401
    ImageRootPolicy,
    is_portable_image_source,
    sniff_image_ext,
    sniff_image_mime,
)

GENERATOR = "minimal texteditor"

# Object-replacement character marking an inline image fragment.
_OBJ = "\ufffc"

# Qt uses U+2028 for Shift+Enter line breaks inside a block.
_LINE_SEP = "\u2028"


def _data_uri(data: bytes) -> str | None:
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
    # The page CSS is for browsers only; Qt ignores <style> blocks when the
    # file is reopened, which is fine because everything that must round-trip
    # (colors, pre-wrap paragraphs) is also emitted inline.
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
    img {{ max-width: 100%; height: auto; }}"""


def _render_image(img_fmt, source_url_for, policy, asset_resolver) -> str:
    """One <img>, resolving bytes through the resolver then the policy.

    Order matters: an app-created asset is named by a key only the
    resolver understands, a legacy or foreign image is named by a path
    only the policy may read, and an image whose name is itself an
    address (a data: URI or a third-party URL) is its own answer.
    """
    name = img_fmt.name()
    alt = str(img_fmt.property(QTextImageFormat.ImageAltText) or "")
    attrs = f' alt="{html.escape(alt, quote=True)}"'
    for prop, value in (("width", img_fmt.width()), ("height", img_fmt.height())):
        if value and value > 0:
            attrs += f' {prop}="{int(value)}"'

    source_url = source_url_for(name) if source_url_for else None
    data = None
    resolved = asset_resolver(name) if asset_resolver else None
    if resolved is not None and getattr(resolved, "data", b""):
        data = resolved.data
        remote_url = str(getattr(resolved, "remote_url", "") or "")
        if remote_url:
            source_url = remote_url
    if source_url:
        attrs += f' data-source-url="{html.escape(source_url, quote=True)}"'
    if data is None:
        data = policy.read(name)

    uri = _data_uri(data) if data else None
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
    return "<em>[image unavailable]</em>"


def _render_text_run(text: str, fmt: QTextCharFormat) -> str:
    out = html.escape(text).replace(_LINE_SEP, "<br>")
    if fmt.fontUnderline():
        out = f"<u>{out}</u>"
    if fmt.fontItalic():
        out = f"<em>{out}</em>"
    if fmt.fontWeight() > 400:
        out = f"<strong>{out}</strong>"
    if fmt.hasProperty(QTextCharFormat.ForegroundBrush):
        color = fmt.foreground().color().name()
        out = f'<span style="color:{color}">{out}</span>'
    return out


def _render_runs(runs, render_image) -> str:
    parts = []
    for text, fmt in runs:
        if fmt.isImageFormat():
            parts.append(render_image(fmt.toImageFormat()))
            continue
        # Defensive: strip stray object-replacement chars from plain runs.
        text = text.replace(_OBJ, "")
        if text:
            parts.append(_render_text_run(text, fmt))
    return "".join(parts)


def _needs_pre_wrap(text: str) -> bool:
    """Leading or consecutive spaces would collapse in normal HTML flow."""
    return text.startswith(" ") or "  " in text


def document_to_html(doc, title: str = "", source_url_for=None, *,
                     image_roots=(), asset_resolver=None) -> str:
    """Serialize a QTextDocument to a self-contained semantic HTML5 page.

    source_url_for: optional callback mapping a local image path to its
    original https URL, used only for the data-source-url provenance
    attribute and as a last-resort src when the cached bytes are gone.

    image_roots: directories a local image name may be read from. Empty
    (the default) reads nothing.

    asset_resolver: optional callback mapping an image name to an object
    exposing ``data`` / ``remote_url``, used for images this app created
    and holds in its own cache.
    """
    policy = ImageRootPolicy(image_roots)

    def render_image(img_fmt) -> str:
        return _render_image(img_fmt, source_url_for, policy, asset_resolver)

    body: list[str] = []
    # List markup is emitted compactly (no newlines): whitespace text nodes
    # inside <li> would be re-parsed by Qt as trailing spaces on the item.
    list_buf: list[str] = []
    depth = 0             # how many <ul> levels are open
    li_open: list[bool] = [False]  # index = level, [0] unused

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

    for block in iter_blocks(doc):
        text = block.text()
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
            # blank) paragraph; normalize_lists_after_set_html turns it back
            # into a truly empty block on load.
            body.append("<p>&nbsp;</p>")
            continue
        content = _render_runs(list(iter_block_runs(block)), render_image)
        if _needs_pre_wrap(text):
            body.append(f'<p style="white-space:pre-wrap">{content}</p>')
        else:
            body.append(f"<p>{content}</p>")

    close_to(0)

    page_title = html.escape(title) if title else "Untitled"
    body_html = "\n".join(body)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="generator" content="{GENERATOR}">
  <title>{page_title}</title>
  <style>
{_head_css()}
  </style>
</head>
<body>
{body_html}</body>
</html>
"""


def normalize_lists_after_set_html(doc) -> None:
    """Convert QTextList items back to the editor's literal bullet lines.

    Qt parses <ul><li> into QTextList objects, but the editor's bullet
    behavior (Tab, Enter, Backspace handlers) operates on literal "• "
    lines. Called after every setHtml so both our own exports and foreign
    HTML files edit consistently. Depth N becomes N*4 leading spaces.
    """
    NBSP = "\u00a0"
    list_targets = []
    nbsp_targets = []
    for block in iter_blocks(doc):
        lst = block.textList()
        if lst is not None:
            list_targets.append((block.position(), max(1, lst.format().indent())))
        elif block.text() == NBSP:
            # Our own exports encode empty lines as <p>&nbsp;</p> because Qt
            # drops <p></p> and doubles <p><br></p>; restore true emptiness.
            nbsp_targets.append(block.position())

    # Process bottom-up so earlier positions stay valid while we mutate.
    for pos in reversed(nbsp_targets):
        block = doc.findBlock(pos)
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    for pos, depth in reversed(list_targets):
        block = doc.findBlock(pos)
        lst = block.textList()
        if lst is not None:
            lst.remove(block)

        # Pretty-printed foreign HTML often leaves "item\n" whitespace that
        # Qt parses into trailing spaces; trim them from the item.
        stripped_len = len(block.text().rstrip())
        cursor = QTextCursor(block)
        cursor.setBlockFormat(QTextBlockFormat())
        if stripped_len < len(block.text()):
            cursor.setPosition(block.position() + stripped_len)
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()

        marker_fmt = QTextCharFormat()
        marker_fmt.setFontWeight(400)
        marker_fmt.setFontItalic(False)
        marker_fmt.setFontUnderline(False)
        marker_fmt.clearForeground()
        cursor.setPosition(block.position())
        cursor.insertText(" " * (depth * INDENT_STEP) + "• ", marker_fmt)

    # Qt folds whitespace after </html> (e.g. the file's final newline) into
    # the last paragraph as a trailing space; trim it.
    last = doc.lastBlock()
    text = last.text()
    trimmed = text.rstrip(" " + NBSP)
    if len(trimmed) < len(text):
        cursor = QTextCursor(last)
        cursor.setPosition(last.position() + len(trimmed))
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
