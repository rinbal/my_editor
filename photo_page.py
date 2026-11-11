# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The photo groups and pictures of a page as STANDUP writes them: their
markup (as md.py writes it), their stylesheet and their scripts
(STANDUP's own files, in web/) and the words they say.

export_html writes a document's with it, rmarkdown a knitted page's: one
writer of the markup, so every page shows a picture the same way. A page
MyEditor wrote is read back by the editor as the paragraphs it was written
from (page_as_paragraphs).

What differs from STANDUP's page, because the page holds its pictures
(data: addresses): a picture carries its pixel size and the web address
it came from (data-source-url, the viewer's "Open Original"); it is not
loaded lazily (it is in the page, and a printed page shows it); its
opener leads to that web address, or, for a picture that has none, to
the picture itself (a page without script cannot open a data: address).
The words are those of the app's language.
"""

from __future__ import annotations

import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple
from urllib.parse import urlsplit

import i18n
import photo_group_geometry
import photo_layout
from constants import DARK_FG, LIGHT_FG
from i18n import _

# The page parts of photo groups: STANDUP's files.
_WEB = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"
# The words of the photo parts in both languages, as STANDUP's pages carry
# them (shared/photo-group-messages.json).
MESSAGES = json.loads((_WEB / "photo-group-messages.json").read_text(encoding="utf-8"))
# The scripts in the order STANDUP's pages inline them (photo_scripts.py):
# the engine, the viewer, the group behaviour, which uses both.
_SCRIPTS = ("photo_layout.js", "photo_viewer.js", "photo_groups.js")
# JSON inside a script element (STANDUP's script_json): nothing that could
# end the element or change how the page's parser reads it.
_SCRIPT_UNSAFE = {ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026",
                  0x2028: "\\u2028", 0x2029: "\\u2029"}

# A picture a reader can open full size (md.py, _OPENABLE_RE), and one
# written into the page that the viewer opens (photo_groups.js, INLINE).
_OPENABLE = re.compile(r"(?:https?://|/(?!/))", re.I)
_INLINE = re.compile(r"data:image/(?:png|jpeg|gif|webp|avif);base64,", re.I)
_WEB_ADDRESS = re.compile(r"https?://", re.I)

# md.py's icons: "Enlarge" and the arrow of a linked picture's flag.
_ZOOM_SVG = ('<svg class="e21-pg-zoom-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
             'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" '
             'aria-hidden="true"><path d="M15 3h6v6"></path><path d="M9 21H3v-6"></path>'
             '<path d="m21 3-7 7"></path><path d="m3 21 7-7"></path></svg>')
_ARROW_SVG = ('<svg class="e21-imgflag-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" '
              'stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" '
              'aria-hidden="true"><path d="M7 7h10v10"></path><path d="M7 17 17 7"></path></svg>')


def page_language() -> str:
    """The language of a page's photo words and of its pictures' text:
    the app's, ``de`` or ``en`` (as STANDUP's page_language reads one)."""
    return "de" if i18n.language().lower().startswith("de") else "en"


def _words() -> dict:
    return MESSAGES[page_language()]


_GROUP_DARK = f"""    @media (prefers-color-scheme: dark) {{
      .article-content {{
        --theme-bg-subtle: #2a2a2c;
        --theme-text-muted: #a8a49c;
        --theme-text-heading: {DARK_FG};
        --theme-accent: #8c9eff;
        --e21-on-accent: #111;
        --article-link: #8ab4ff;
      }}
    }}
"""


def group_head(dark: bool = True) -> str:
    """The stylesheet of photo groups, with the colours and type of this
    page in the variables it reads; ``dark``: also the colours of Dark
    Mode (a page that does not change with it, as a knitted one, keeps
    the light ones)."""
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
      --e21-ui-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif;
    }}
{_GROUP_DARK if dark else ""}""" + (_WEB / "photo_groups.css").read_text(encoding="utf-8"))


def group_scripts() -> str:
    """The photo scripts of a page in one script, as STANDUP's pages
    inline them (photo_scripts.py), with the words of both languages."""
    source = "\n".join((_WEB / name).read_text(encoding="utf-8") for name in _SCRIPTS)
    messages = json.dumps(MESSAGES, ensure_ascii=False).translate(_SCRIPT_UNSAFE)
    return f"<script>{source}\nE21PhotoGroups.attach(document, {{messages: {messages}}});</script>\n"


@dataclass
class Figure:
    """One picture as a page shows it, in a photo group or alone: its src
    (a data: URI or an address; None when it cannot be had), its pixel
    size (None: not known, laid out as 3:2 until it is), where it came
    from, its description, its caption as HTML and as text, and whether
    it is wide."""

    src: Optional[str]
    size: Optional[Tuple[int, int]]
    source: Optional[str]
    alt: str
    caption_html: str = ""
    caption_text: str = ""
    wide: bool = False
    # The address the picture links to ("" for none), one a page may link to.
    href: str = ""
    # Its number among the page's pictures, from 1: its name on the page.
    place: int = 0


def link_host(href: str) -> str:
    """``example.org`` for a web address (``www.`` dropped), else "": the
    words of a linked picture's flag (md.py, link_host)."""
    try:
        parts = urlsplit(href)
        host = parts.hostname if parts.scheme in ("http", "https") else None
    except ValueError:
        return ""
    if not host:
        return ""
    return host[4:] if host.startswith("www.") else host


def _linked(picture: str, href: str) -> str:
    """A linked picture as md.py writes one: the whole picture is the
    link, a flag on it names where it leads."""
    host = link_host(href)
    flag = ('<span class="e21-imgflag">'
            + (f'<span class="e21-imgflag-host">{html.escape(host)}</span>' if host else "")
            + _ARROW_SVG + "</span>")
    attrs = f' href="{html.escape(href, quote=True)}"'
    if not href.startswith("#"):
        attrs += ' target="_blank" rel="noopener noreferrer"'
    return f'<a class="e21-imglink"{attrs}>{picture}{flag}</a>'


def _opener(f: Figure) -> Tuple[str, str]:
    """Where the link around a picture that opens full size leads, and the
    picture's own name on the page when it leads to the picture itself: an
    address leads to it (md.py), a picture in the page to the web address
    it came from, or, without one, to itself."""
    if not _INLINE.match(f.src):
        return f.src, ""
    if f.source and _WEB_ADDRESS.match(f.source):
        return f.source, ""
    name = f"e21-photo-{f.place}"
    return f"#{name}", name


def _zoom(alt: str) -> str:
    """md.py's "Enlarge" under a #wide picture that opens full size."""
    words = _words()
    label = words["enlargeNamed"].replace("{alt}", alt) if alt else words["enlarge"]
    return (f'<button type="button" class="e21-pg-zoom" aria-label="{html.escape(label, quote=True)}">'
            f'{_ZOOM_SVG}{html.escape(words["enlarge"])}</button>')


def _picture(f: Figure, alt: str) -> str:
    img = f'<img src="{html.escape(f.src, quote=True)}" alt="{html.escape(alt, quote=True)}"'
    if f.size:
        img += f' width="{f.size[0]}" height="{f.size[1]}"'
    if f.source:
        img += f' data-source-url="{html.escape(f.source, quote=True)}"'
    # Its bytes are in the page already: nothing to wait for (and a page
    # printed or saved as PDF shows every picture).
    return img + (' decoding="async">' if f.src.startswith("data:")
                  else ' loading="lazy" decoding="async">')


def figure_html(f: Figure, properties) -> str:
    """One picture as ``<figure class="e21-figure">`` (md.py, _figure_html);
    with ``properties`` it is a cell of its group (photo_layout.group_markup).
    A description of only spaces is none."""
    alt = f.alt if f.alt.strip() else ""
    cls = ("e21-figure" + (" e21-figure--wide" if f.wide else "")
           + (" e21-pg-cell" if properties is not None else ""))
    style = f"--ar:{f.size[0]}/{f.size[1]}" if f.size else "--ar:3/2"
    if properties:
        style += ";" + photo_layout.style_text(properties)
    provisional = "" if f.size else " data-provisional"
    name = zoom = ""
    if f.src is None:
        img = f"<em>{html.escape(_('[image unavailable]'))}</em>"
    else:
        img = _picture(f, alt)
        opens = bool(_OPENABLE.match(f.src) or _INLINE.match(f.src))
        if f.href:
            img = _linked(img, f.href)
        elif opens:
            target, name = _opener(f)
            img = f'<a class="e21-pg-open" href="{html.escape(target, quote=True)}">{img}</a>'
        if f.wide and opens:
            zoom = _zoom(alt)
    caption = f"<figcaption>{f.caption_html}</figcaption>" if f.caption_html else ""
    named = f' id="{name}"' if name else ""
    return f'<figure class="{cls}" style="{style}"{provisional}{named}>{img}{zoom}{caption}</figure>'


def _column(inside: str) -> str:
    """A part of the page in STANDUP's article column, in the language of
    its words."""
    return f'<div class="article-content" lang="{page_language()}">{inside}</div>'


def figure_alone_html(f: Figure) -> str:
    """A picture alone, as md.py writes one: a figure of its own, as wide
    as the column (as wide as the page for #wide), its caption under it."""
    return _column(figure_html(f, None))


def group_figures_html(figures: Sequence[Figure]) -> str:
    """One photo group as STANDUP's renderer writes it (md.py,
    _image_group): the engine's cells in Markdown order, each picture a
    figure with its shape and its place, its caption under it. The one
    writer of a group's markup, for the export and for a knitted page."""
    items = [photo_group_geometry.item(f.size, f.caption_text, f.wide) for f in figures]
    result = photo_layout.layout(items)
    track = photo_layout.group_html(
        photo_layout.group_markup(result, items),
        lambda index, properties: figure_html(figures[index], properties))
    n = len(figures)
    label = html.escape(_words()["group"].replace("{n}", str(n)), quote=True)
    return _column(f'<div class="e21-imagegroup e21-imagegroup--{n if n <= 4 else "many"}" '
                   f'role="group" aria-label="{label}" data-layout="{photo_layout.notation(result)}">'
                   f'<div class="e21-imagegroup-track">{track}</div></div>')


# A figure this module wrote, as Qt's HTML reader would take it (it knows
# no figure, and keeps every word, link and size it finds): the picture
# paragraph the figure was written from, its caption on the line under
# the picture in italic, the picture at its own size. What only a browser
# shows of it goes: the opener's link, "Enlarge", a linked picture's flag.
_FIGURE = re.compile(r'<figure class="e21-figure[^"]*"[^>]*>(.*?)</figure>', re.S)
_PICTURE_SIZE = re.compile(r'(<img [^>]*?) width="\d+" height="\d+"')
_OPENER = re.compile(r'<a class="e21-pg-open" href="[^"]*">(<img [^>]*>)</a>')
_ZOOM = re.compile(r'<button type="button" class="e21-pg-zoom"[^>]*>.*?</button>', re.S)
_FLAG = re.compile(r'<span class="e21-imgflag">(?:<span class="e21-imgflag-host">[^<]*</span>)?'
                   r'<svg class="e21-imgflag-icon".*?</svg></span>', re.S)


def _figure_paragraph(match) -> str:
    inside = _PICTURE_SIZE.sub(r"\1", match.group(1))
    inside = _FLAG.sub("", _ZOOM.sub("", _OPENER.sub(r"\1", inside)))
    inside = inside.replace("<figcaption>", "<br><em>").replace("</figcaption>", "</em>")
    return f"<p>{inside}</p>"


def page_as_paragraphs(page: str) -> str:
    """``page`` (an HTML file being opened) with each figure MyEditor
    wrote as the paragraph it was written from, so the editor reads a
    group's pictures and a picture alone back as they were."""
    return _FIGURE.sub(_figure_paragraph, page)
