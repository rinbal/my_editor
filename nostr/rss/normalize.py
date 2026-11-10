# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalise a ``FeedItem`` into kwargs for ``nostr.publisher.build_article``.

Two layers:

- :func:`html_to_markdown` runs ``markdownify`` with TurndownService-aligned
  options so the output matches the TypeScript reference byte-for-byte
  whenever the input HTML round-trips cleanly.
- :func:`item_to_article` produces a small :class:`ArticleTemplate` value
  the importer feeds to ``build_article`` (which then becomes the inner
  event for a NIP-37 draft wrap).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from markdownify import ATX, markdownify as _markdownify

from .clean import APPEARED_FIRST, clean_html, code_language
from .dtag import derive_identifier
from .markdown import escape, unescape, without_code_blocks
from .parser import FeedItem
from .urls import is_web, whole_url


_SOURCE_FOOTER_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class ArticleTemplate:
    """Inputs for ``build_article``. The signing pubkey is supplied by
    the caller; everything else comes from the feed item.

    The article's own Markdown (``body``) and its attribution line
    (``footer``, "" when there is none) are kept apart, so a step that
    replaces or measures the body never has to find the line again."""

    slug: str
    body: str
    footer: str
    title: str
    summary: str
    image: str
    published_at: Optional[int]
    hashtags: Tuple[str, ...]

    @property
    def content(self) -> str:
        """The Markdown published: the body, then the attribution line."""
        return (self.body + self.footer).strip()


def html_to_markdown(html: str, *, base_url: Optional[str] = None) -> str:
    """HTML to Markdown with TS-reference-aligned options.

    The HTML is cleaned first (clean.py): ``base_url`` (the post's own
    page) makes relative links and images whole, since a feed's
    ``<a href="/about">`` or ``<img src="/media/a.jpg">`` points nowhere
    in a draft or an article read elsewhere (CP1 review O3); lazy
    images, embeds, video and audio, and page furniture are settled
    there too. A code block keeps the language it names.
    """
    if not html:
        return ""
    return _markdownify(
        clean_html(html, base_url=base_url),
        heading_style=ATX,
        bullets="-",
        strong_em_symbol="_",
        code_language_callback=code_language,
    )


# WordPress's default category, as its translations name it
# (translate.wordpress.org), written as a hashtag: a post nobody filed
# is not about "uncategorized".
_UNFILED = frozenset({
    "uncategorized", "uncategorised", "allgemein", "sincategoría", "nonclassé",
    "senzacategoria", "geencategorie", "nietgecategoriseerd", "semcategoria",
    "bezkategorii", "okategoriserade", "безрубрики", "未分類", "ikke-kategoriseret",
    "ukategorisert", "nezařazené",
})
# How a teaser ends: WordPress's excerpt "[…]", an ellipsis, the "more"
# link of WordPress ("(more…)") and of its themes ("Continue reading")
# and their translations.
_ELLIPSIS = r"\[(?:…|\.\.\.)\]|…|\.\.\.|\(more…\)"
_CONTINUE = (r"continue reading|read more|weiterlesen|lire la suite|continuer la lecture|"
             r"poursuivre la lecture|leer más|seguir leyendo|sigue leyendo|"
             r"continuar leyendo|continua a leggere|leggi tutto|lees verder|"
             r"continue lendo|leia mais|czytaj dalej")
# The post's title as a theme quotes it in its read-on link
# ('Continue reading "Title"', German themes' '„Title“ weiterlesen').
_QUOTED = r'(?:„[^“\n]*“|"[^"\n]*"|“[^”\n]*”|«[^»\n]*»)'
_READ_ON = r"(?:%s\s*)?(?:%s)(?:\s*%s)?" % (_QUOTED, _CONTINUE, _QUOTED)
# In a summary: the ellipsis at its end, with a read-on link after it.
_READ_MORE = re.compile(r"\s*(?:%s)(?:\s*%s)?\W*$" % (_ELLIPSIS, _READ_ON), re.IGNORECASE)
# At the end of a body: the ellipsis, a link that says to read on, or a
# last line that says only that.
_TEASER_END = re.compile(
    r"(?:%s|\[[^\]\n]*\b(?:%s)\b[^\]\n]*\]\([^)]*\)|(?:^|\n)\W*%s\W*)\s*$"
    % (_ELLIPSIS, _CONTINUE, _READ_ON), re.IGNORECASE)

_SUMMARY_CHARS = 280
# A first paragraph shorter than this, before a longer one, is a kicker
# or a label ("TEIL 00"), not the opening sentence.
_KICKER_CHARS = 25
_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_SPACE_BEFORE_MARK = re.compile(r"\s+([.,;:!?)])")
_EMAIL = re.compile(r"^[^@\s()<>]+@[^@\s()<>]+\.[^@\s()<>]+$")
_EMAIL_WITH_NAME = re.compile(r"^\S+@\S+\s*\((.+)\)$")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# A line's block marks (heading, quote, list item), emphasis, and an
# autolink's brackets. A ">" in the middle of a sentence ("rocks > 5")
# is a character.
_MD_MARKS = re.compile(r"^[ \t]*(?:[#>]+|[-+]|\d+[.)])[ \t]+|(?<!\\)[*_`~]+|<(https?://[^>]+)>",
                       re.MULTILINE)
_BARE_URL = re.compile(r"https?://\S+")


def hashtag(raw: str) -> str:
    """One category as a Nostr hashtag: no ``#``, lower case, no spaces
    (a ``t`` tag with a space is a hashtag no client can link)."""
    return "".join((raw or "").strip().lstrip("#").lower().split())


def _merge_hashtags(
    item_categories: Iterable[str],
    extra: Iterable[str],
) -> Tuple[str, ...]:
    """As hashtags (:func:`hashtag`), deduped in first-seen order.
    WordPress's default "Uncategorized" is left out."""
    seen: list[str] = []
    categories = [hashtag(raw) for raw in item_categories]
    for tag in (*(c for c in categories if c not in _UNFILED),
                *(hashtag(raw) for raw in extra)):
        if tag and tag not in seen:
            seen.append(tag)
    return tuple(seen)


def plain_text(html_text: Optional[str]) -> str:
    """HTML as the words it shows, on one line."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", html_text or ""))).strip()


def author_name(raw: Optional[str]) -> str:
    """The name to credit a post to, or "" when the source names nobody.

    RSS writes an author as an e-mail address, often with the name after
    it (``jane@example.com (Jane Doe)``): the name is kept, an address
    alone is never published."""
    value = plain_text(raw)
    match = _EMAIL_WITH_NAME.match(value)
    if match:
        value = match.group(1).strip()
    if _EMAIL.match(value):
        return ""
    return value


def plain_summary(summary_html: Optional[str]) -> str:
    """A feed's summary as the plain text NIP-23 asks for: no tags, no
    entities, one line, without the "[…]" a teaser ends on or the
    "The post X appeared first on Y." line SEO plugins add."""
    text = plain_text(summary_html)
    text = _SPACE_BEFORE_MARK.sub(r"\1", text)
    plugin_line = APPEARED_FIRST.search(text)
    if plugin_line and len(text) - plugin_line.end() < 120:
        text = text[:plugin_line.start()].strip()
    trimmed = _READ_MORE.sub("", text).strip()
    if trimmed != text and trimmed:
        text = trimmed + "…"
    return _shorten(text)


def _prose(markdown: str) -> List[str]:
    """The paragraphs of a body as a reader sees their words: no code,
    headings, tables or images, a link as its words, and a paragraph
    that is only an address left out."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", without_code_blocks(markdown or "")):
        stripped = block.strip()
        if not stripped or stripped.startswith(("#", "|", "---", "*Originally")):
            continue
        text = _MD_MARKS.sub(lambda m: m.group(1) or "",
                             _MD_LINK.sub(r"\1", _MD_IMAGE.sub(" ", stripped)))
        text = _SPACE.sub(" ", unescape(text)).strip()
        if text and not _BARE_URL.fullmatch(text):
            paragraphs.append(text)
    return paragraphs


def reading_text(markdown: str) -> str:
    """What a body says in words, without the addresses in it: a body of
    links ("Article URL: https://… Comments URL: https://…") says little."""
    return _SPACE.sub(" ", _BARE_URL.sub("", " ".join(_prose(markdown)))).strip()


def summary_from_body(markdown: str) -> str:
    """A summary made of the article's opening words, for a post whose
    source gives none: what a reader sees first, cut at a word. Code,
    headings, tables and a line that is only an address are skipped, and
    so is a short kicker before the first real sentence ("TEIL 00")."""
    paragraphs = _prose(markdown)
    start = next((i for i, text in enumerate(paragraphs) if len(text) >= _KICKER_CHARS), 0)
    words: List[str] = []
    for text in paragraphs[start:]:
        words.append(text)
        if sum(len(w) for w in words) >= _SUMMARY_CHARS // 2:
            break
    return _shorten(" ".join(words))


def ends_as_teaser(markdown: str) -> bool:
    """Whether a body ends the way a teaser does ("[…]", "…", "Continue
    reading"): the post goes on on its page, however long the part in
    the feed is (WordPress's "Summary" feeds give 55 words)."""
    return bool(_TEASER_END.search((markdown or "").strip()))


def _shorten(text: str) -> str:
    if len(text) <= _SUMMARY_CHARS:
        return text
    cut = text[:_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return cut + "…"


def source_link_footer(link: str, author: Optional[str] = None) -> str:
    """Footer block appended to a feed item's body when ``link`` is set,
    crediting the post's author when the source names one."""
    name = author_name(author)
    if name:
        credit = escape(name)
        return (f"{_SOURCE_FOOTER_SEPARATOR}*Originally published by {credit} "
                f"at [{link}]({link})*")
    return f"{_SOURCE_FOOTER_SEPARATOR}*Originally published at [{link}]({link})*"


def item_to_article(
    item: FeedItem,
    *,
    hashtags: Iterable[str] = (),
    identifier_prefix: Optional[str] = None,
    append_source_link: bool = True,
) -> ArticleTemplate:
    """Convert a :class:`FeedItem` to an :class:`ArticleTemplate`.

    The d-tag identifier is derived deterministically from
    ``(guid, link, title)`` so re-importing the same feed produces the
    same draft slot on the relay (idempotent replacement).

    Content precedence: a source-supplied ``content_markdown`` (Nostr
    long-form, NostrHub NIPs, MDX) is authoritative and taken as written;
    otherwise the item's HTML body is converted. Either way a first
    heading that only repeats the title is dropped: the title is shown
    above the body.

    The details are made fit to publish whatever the source wrote: the
    summary as plain text (or the opening words when the source has
    none), the cover as a whole address, the hashtags without spaces.
    """
    if (item.content_markdown or "").strip():
        markdown = str(item.content_markdown).strip()
    else:
        markdown = html_to_markdown(item.content_html, base_url=item.link).strip()
    body = _without_title_heading(markdown, item.title)
    footer = source_link_footer(item.link, item.author) if append_source_link and item.link \
        else ""

    slug = derive_identifier(
        guid=item.guid,
        link=item.link,
        title=item.title,
        prefix=identifier_prefix,
    )

    return ArticleTemplate(
        slug=slug,
        body=body,
        footer=footer,
        title=item.title or "",
        summary=plain_summary(item.summary) or summary_from_body(body),
        image=cover_address(item.image, item.link),
        published_at=item.published_at,
        hashtags=_merge_hashtags(item.categories, hashtags),
    )


_HEADING = re.compile(r"\A#{1,2}[ \t]+(.+?)[ \t#]*(\n+|\Z)")


def _without_title_heading(markdown: str, title: Optional[str]) -> str:
    """The body without a first heading that only repeats the title (a
    page read for its full text, a Markdown file): the title is shown
    above the article already."""
    match = _HEADING.match(markdown)
    if match and title and _comparable(match.group(1)) == _comparable(title):
        return markdown[match.end():].lstrip()
    return markdown


def _comparable(text: str) -> str:
    return "".join(ch for ch in html.unescape(text).lower() if ch.isalnum())


def cover_address(image: Optional[str], page: Optional[str]) -> str:
    """The cover as a whole web address (made whole against the post's
    page), or "" for anything else (a ``data:`` picture, a bare path
    with no page to place it)."""
    value = (whole_url(image, page) or "").strip()
    return value if is_web(value) else ""
