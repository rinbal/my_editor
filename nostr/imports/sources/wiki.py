# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-54 wiki (kind 30818) content normalisation.

The content field of a wiki article has drifted across sources that
disagree: the current NIP-54 spec says Djot, older references say
AsciiDoc, and real kind-30818 events in the wild are plain Markdown.
Djot and Markdown share the syntax for everything that matters here, so
the body is treated as Markdown and only the two constructs that are
NOT valid Markdown are rewritten, leaving the common case untouched:

1. Wikilinks. Legacy ``[[Target]]`` / ``[[target|display]]`` and the
   Djot empty-reference form ``[display][]`` point at another wiki
   article, which has no portable URL in an imported draft, so the
   human display text is kept and the link mechanics dropped.
2. AsciiDoc section headings (``== Section``): a leading run of ``=``
   then a space is never valid Markdown or Djot, so mapping it to ``#``
   recovers legacy articles without disturbing Markdown bodies.

Deliberately conservative: ambiguous inline markup (``*word*`` is bold
in AsciiDoc but italic in Markdown) is left as-is rather than guessed
at. Fenced code is protected from every rewrite.
"""

from __future__ import annotations

import re

_WIKILINK_PIPE_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")  # [[target|display]]
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")                   # [[Target]]
_DJOT_WIKILINK_RE = re.compile(r"\[([^\]]+)\]\[\]")              # [display][]
# Horizontal whitespace only; \s would swallow the trailing newline
# (and the blank line after the heading) in multiline mode.
_ASCIIDOC_HEADING_RE = re.compile(
    r"^(={1,6})[ \t]+(.+?)[ \t]*=*[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```[\s\S]*?```")


def normalize_wiki_content(content: str) -> str:
    """Normalise a kind-30818 wiki body to clean Markdown."""
    text = str(content or "")

    # Protect fenced code from every rewrite below.
    fences: list[str] = []

    def _stash(match: re.Match) -> str:
        fences.append(match.group(0))
        return f"\x00F{len(fences) - 1}\x00"

    text = _FENCE_RE.sub(_stash, text)

    text = _WIKILINK_PIPE_RE.sub(lambda m: m.group(2).strip(), text)
    text = _WIKILINK_RE.sub(lambda m: m.group(1).strip(), text)
    text = _DJOT_WIKILINK_RE.sub(lambda m: m.group(1), text)
    text = _ASCIIDOC_HEADING_RE.sub(
        lambda m: f"{'#' * len(m.group(1))} {m.group(2).strip()}", text)

    text = re.sub(
        r"\x00F(\d+)\x00", lambda m: fences[int(m.group(1))], text)
    return text.strip()
