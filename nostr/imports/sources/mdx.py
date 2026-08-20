# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MDX / Markdown to clean Markdown conversion.

MDX is Markdown with JSX mixed in (Astro/Next content). Importing it
into a long-form draft means producing plain, portable Markdown while
losing as little structure as possible: headings, prose, emphasis,
lists, code, and tables survive.

Pipeline (all pure, no I/O):

  1. Split off and parse the YAML frontmatter (keep ``title`` etc.).
  2. Protect fenced code blocks so nothing below rewrites them.
  3. Drop ESM ``import`` / ``export`` statements.
  4. Neutralise MDX expressions: the ``{' '}`` whitespace token becomes
     a space; JSX comment expressions are removed.
  5. Remove JSX *component* tags (uppercase-named, plus ``<Fragment>``),
     keeping their children. Lowercase HTML tags are left for step 6.
  6. Convert the remaining HTML islands (blank-line-delimited blocks
     that start with a tag) to Markdown via the shared converter; tidy
     stray inline HTML inside Markdown blocks. Whole-document
     conversion would escape every Markdown construct, so only the
     HTML blocks are converted, which lines up with CommonMark's own
     rule that an HTML block is delimited by blank lines.
  7. Restore code blocks and tidy whitespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from ...rss.normalize import html_to_markdown


_INLINE_MARKS_RE = re.compile(r"[*_`]")
_LEADING_MARKS_RE = re.compile(r"^[>*_\s-]+")


def derive_summary(markdown: str, *, max_len: int = 200, min_len: int = 25) -> str:
    """A prose one-liner for previews.

    Prefers the first *substantial* line (>= ``min_len`` chars) so short
    kickers or labels are skipped in favour of the real opening
    sentence, falling back to the first prose line when nothing longer
    exists. Headings, tables, and code fences are ignored.
    """
    first = ""
    for line in str(markdown or "").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```") \
                or line.startswith("|"):
            continue
        cleaned = _INLINE_MARKS_RE.sub(
            "", _LEADING_MARKS_RE.sub("", line)).strip()
        if not cleaned:
            continue
        if not first:
            first = cleaned
        if len(cleaned) >= min_len:
            return _clamp(cleaned, max_len)
    return _clamp(first, max_len) if first else ""


def _clamp(line: str, max_len: int) -> str:
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Frontmatter                                                                 #
# --------------------------------------------------------------------------- #

_FRONTMATTER_RE = re.compile(r"^﻿?---\r?\n([\s\S]*?)\r?\n---\r?\n?")
_FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")


def parse_frontmatter(raw: str) -> Tuple[Dict[str, str], str]:
    """Split ``---`` YAML frontmatter off the top; values may be quoted.

    Deliberately a flat key:value parse, not full YAML: content
    frontmatter in the wild is overwhelmingly flat, and a YAML
    dependency for ``title:`` extraction would be all cost.
    """
    match = _FRONTMATTER_RE.match(str(raw or ""))
    if not match:
        return {}, str(raw or "")
    frontmatter: Dict[str, str] = {}
    for line in match.group(1).split("\n"):
        kv = _FRONTMATTER_KV_RE.match(line)
        if not kv:
            continue
        value = kv.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        frontmatter[kv.group(1)] = value
    return frontmatter, raw[match.end():]


# --------------------------------------------------------------------------- #
# Code-fence protection                                                       #
# --------------------------------------------------------------------------- #

_FENCE_OPEN_RE = re.compile(r"^(\s*)(```+|~~~+)(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^(\s*)(```+|~~~+)\s*$")


def _protect_fences(text: str) -> Tuple[str, List[str]]:
    out: List[str] = []
    blocks: List[str] = []
    marker = None
    buffer: List[str] = []
    for line in text.split("\n"):
        if marker is None:
            opened = _FENCE_OPEN_RE.match(line)
            if opened:
                marker = opened.group(2)
                buffer = [line]
                continue
            out.append(line)
        else:
            buffer.append(line)
            closed = _FENCE_CLOSE_RE.match(line)
            if closed and closed.group(2)[0] == marker[0] \
                    and len(closed.group(2)) >= len(marker):
                out.append(f"\x00FENCE{len(blocks)}\x00")
                blocks.append("\n".join(buffer))
                marker = None
                buffer = []
    if marker is not None:
        out.extend(buffer)  # unterminated: leave as-is
    return "\n".join(out), blocks


def _restore_fences(text: str, blocks: List[str]) -> str:
    return re.sub(
        r"\x00FENCE(\d+)\x00",
        lambda m: blocks[int(m.group(1))]
        if int(m.group(1)) < len(blocks) else "",
        text,
    )


# --------------------------------------------------------------------------- #
# MDX / JSX stripping                                                         #
# --------------------------------------------------------------------------- #

# Precise ESM matchers so a prose line that merely starts with "import"
# or "export" is never mistaken for code.
_ESM_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:[^\n]*\bfrom\b\s*)?['\"][^'\"]+['\"]\s*;?\s*$")
_ESM_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:default|const|let|var|async|function|class|\{)[^\n]*$")

# Component tags are uppercase-named (React/Astro convention) or
# <Fragment>. ``[^>]`` spans newlines, so multi-line opening tags match.
_JSX_COMPONENT_TAG_RE = re.compile(r"</?[A-Z][A-Za-z0-9.]*(?:\s[^>]*)?/?>")

_MDX_COMMENT_RE = re.compile(r"\{/\*[\s\S]*?\*/\}")
_MDX_SPACE_TOKEN_RE = re.compile(r"\{\s*['\"]\s*['\"]\s*\}")


def _strip_esm(text: str) -> str:
    return "\n".join(
        line for line in text.split("\n")
        if not _ESM_IMPORT_RE.match(line) and not _ESM_EXPORT_RE.match(line)
    )


def _strip_mdx_expressions(text: str) -> str:
    text = _MDX_COMMENT_RE.sub("", text)
    return _MDX_SPACE_TOKEN_RE.sub(" ", text)


def _strip_jsx_components(text: str) -> str:
    return _JSX_COMPONENT_TAG_RE.sub("", text)


# --------------------------------------------------------------------------- #
# HTML islands to Markdown                                                    #
# --------------------------------------------------------------------------- #

_HTML_BLOCK_START_RE = re.compile(r"^</?[a-zA-Z][\s\S]*>")
_AUTOLINK_START_RE = re.compile(r"^<https?:", re.IGNORECASE)


def _clean_inline_html(block: str) -> str:
    """Light cleanup of stray inline HTML in an otherwise-Markdown block."""
    block = re.sub(r"</?strong>", "**", block, flags=re.IGNORECASE)
    block = re.sub(r"</?(?:em|i)>", "_", block, flags=re.IGNORECASE)
    block = re.sub(r"<br\s*/?>", "\n", block, flags=re.IGNORECASE)
    block = re.sub(r"</?span(?:\s[^>]*)?>", "", block, flags=re.IGNORECASE)
    block = re.sub(r"</?div(?:\s[^>]*)?>", "", block, flags=re.IGNORECASE)
    return block


def _convert_html_islands(text: str) -> str:
    blocks = re.split(r"\n[ \t]*\n", text)
    out: List[str] = []
    for block in blocks:
        trimmed = block.strip()
        if not trimmed:
            continue
        looks_html = (
            _HTML_BLOCK_START_RE.match(trimmed)
            and not _AUTOLINK_START_RE.match(trimmed)
        )
        if looks_html:
            try:
                markdown = html_to_markdown(trimmed).strip()
            except Exception:  # noqa: BLE001, keep the block over dropping content
                markdown = ""
            if markdown:
                out.append(markdown)
                continue
        cleaned = _clean_inline_html(block)
        if cleaned.strip():
            out.append(cleaned)
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# Public entry                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MdxDocument:
    title: str
    markdown: str
    summary: str
    frontmatter: Dict[str, str] = field(default_factory=dict)


def mdx_to_markdown(raw: str) -> MdxDocument:
    """Convert one MDX/Markdown document to clean Markdown."""
    frontmatter, body = parse_frontmatter(raw)
    text, fences = _protect_fences(body)

    text = _strip_esm(text)
    text = _strip_mdx_expressions(text)
    text = _strip_jsx_components(text)
    text = _convert_html_islands(text)
    text = _restore_fences(text, fences)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return MdxDocument(
        title=(frontmatter.get("title") or "").strip(),
        markdown=text,
        summary=derive_summary(text),
        frontmatter=frontmatter,
    )
