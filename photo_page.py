# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The photo groups and pictures of a page as STANDUP writes them: their
markup (as md.py writes it), their stylesheet and their scripts
(STANDUP's, in web/) and the words they say.

export_html writes a document's with it, rmarkdown a knitted page's: one
writer of the markup, so every page shows a picture the same way. A page
MyEditor wrote is read back by the editor as the paragraphs it was written
from (page_as_paragraphs).
"""

from __future__ import annotations

import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

import photo_group_geometry
import photo_layout
from constants import DARK_FG, LIGHT_FG
from i18n import _, ngettext

# The page parts of a photo group, vendored from STANDUP.
_WEB = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"


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
      --e21-ui-font: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    }}
{_GROUP_DARK if dark else ""}    .article-content img[data-pg-open] {{ cursor: zoom-in; }}
    .article-content img[data-pg-open]:focus-visible {{ outline: 2px solid var(--article-link); outline-offset: 3px; }}
""" + (_WEB / "photo_groups.css").read_text(encoding="utf-8"))


def group_scripts() -> str:
    """The words and the scripts of photo groups, inline."""
    words = json.dumps(_group_words(), ensure_ascii=False).replace("</", "<\\/")
    return (f'<script type="application/json" id="e21-photo-words">{words}</script>\n'
            f'<script>\n{(_WEB / "photo_groups.js").read_text(encoding="utf-8")}</script>\n'
            f'<script>\n{(_WEB / "photo_viewer.js").read_text(encoding="utf-8")}</script>\n')


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


def figure_html(f: Figure, properties) -> str:
    """One picture as ``<figure class="e21-figure">``; with ``properties``
    it is a cell of its group (photo_layout.group_markup)."""
    alt = html.escape(f.alt, quote=True)
    cls = ("e21-figure" + (" e21-figure--wide" if f.wide else "")
           + (" e21-pg-cell" if properties is not None else ""))
    style = f"--ar:{f.size[0]}/{f.size[1]}" if f.size else "--ar:3/2"
    if properties:
        style += ";" + photo_layout.style_text(properties)
    provisional = "" if f.size else " data-provisional"
    if f.src is None:
        img = f"<em>{html.escape(_('[image unavailable]'))}</em>"
    else:
        img = f'<img src="{html.escape(f.src, quote=True)}" alt="{alt}"'
        if f.size:
            img += f' width="{f.size[0]}" height="{f.size[1]}"'
        if f.source:
            img += f' data-source-url="{html.escape(f.source, quote=True)}"'
        # Its bytes are in the page already: nothing to wait for (and
        # a page printed or saved as PDF shows every picture).
        img += ' decoding="async">' if f.src.startswith("data:") else \
            ' loading="lazy" decoding="async">'
    caption = f"<figcaption>{f.caption_html}</figcaption>" if f.caption_html else ""
    return f'<figure class="{cls}" style="{style}"{provisional}>{img}{caption}</figure>'


def figure_alone_html(f: Figure) -> str:
    """A picture alone, as md.py writes one: a figure of its own, as wide
    as the column (as wide as the page for #wide), its caption under it."""
    return f'<div class="article-content">{figure_html(f, None)}</div>'


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
    label = html.escape(ngettext("{n} photo", "{n} photos", n).format(n=n), quote=True)
    return (f'<div class="article-content"><div class="e21-imagegroup '
            f'e21-imagegroup--{n if n <= 4 else "many"}" role="group" aria-label="{label}" '
            f'data-layout="{photo_layout.notation(result)}">'
            f'<div class="e21-imagegroup-track">{track}</div></div></div>')


# A figure this module wrote, as Qt's HTML reader would take it (it knows
# no figure, and keeps every word and size it finds): the picture
# paragraph the figure was written from, its caption on the line under
# the picture in italic, the picture at its own size.
_FIGURE = re.compile(r'<figure class="e21-figure[^"]*"[^>]*>(.*?)</figure>', re.S)
_PICTURE_SIZE = re.compile(r'(<img [^>]*?) width="\d+" height="\d+"')


def _figure_paragraph(match) -> str:
    inside = _PICTURE_SIZE.sub(r"\1", match.group(1))
    inside = inside.replace("<figcaption>", "<br><em>").replace("</figcaption>", "</em>")
    return f"<p>{inside}</p>"


def page_as_paragraphs(page: str) -> str:
    """``page`` (an HTML file being opened) with each figure MyEditor
    wrote as the paragraph it was written from, so the editor reads a
    group's pictures and a picture alone back as they were."""
    return _FIGURE.sub(_figure_paragraph, page)
