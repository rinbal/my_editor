# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finding things in Markdown as written, before Qt's reader reads it.

markdown_writer reads a file in steps around Qt's reader (a picture's
description read whole, a line break between pictures, a soft line
break kept), and each step must know where Markdown takes a character
for text and where for markup: in code (a fence, an indented block, a
code span), in an autolink or a tag, in a link's address, or after a
backslash. This module finds those places in the Markdown text alone.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_AUTOLINK_OR_TAG = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|[^\s<>@]+@[^\s<>]+"
    r"|/?[A-Za-z][\w-]*(?:\s[^<>]*)?/?|!--.*?--)>", re.S)
_CODE_INDENT = re.compile(r"^(?: {4}|\t)")
# Where an escape, a code span, an autolink or tag, or a picture may start.
_STARTS_INLINE = re.compile(r"[\\`<]|!\[")
# Where an escape, or something an escape inside is no escape of, may
# start: code, an autolink or a tag, a picture's description, a link's
# address. A web address that holds an escape is none Qt's reader links
# ("www.x.com/\*y" is text), so its escapes are the text's.
_STARTS_ESCAPE = re.compile(r"[\\`<]|!\[|\]\(")
# A line's quote marks and list markers ("> ", "- ", "1. ", "> - ").
_CONTAINERS = r"(?:[ \t]*(?:>|(?:[-*+]|\d{1,9}[.)])(?=[ \t])))*"
_TITLE = r"""(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|\((?:[^()\\\n]|\\.)*\))"""
# What follows a link reference definition's label ("[a]: /url 'title'"),
# its address and title, in a quote or a list item too, the address or
# the title on the next line; a footnote's ("[^1]: note") is text.
_DEFINITION = re.compile(
    rf"^{_CONTAINERS}[ \t]*\[(?!\^)(?:[^\]\\\n]|\\.)+\]"
    rf"(:(?:[ \t]*\n{_CONTAINERS})?[^\n]*(?:\n{_CONTAINERS}[ \t]*{_TITLE}[ \t]*$)?)", re.M)


def code_lines(lines: List[str]) -> List[bool]:
    """For each line, whether it is code: in a fence, or indented code
    (after a blank line, outside a list)."""
    out: List[bool] = []
    fence = ""
    in_list = False
    previous_blank = True
    for line in lines:
        found = FENCE_LINE.match(line)
        if fence:
            out.append(True)
            if found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence) \
                    and not found.group(2).strip():
                fence = ""
            continue
        if found:
            fence = found.group(1)
            out.append(True)
            continue
        blank = not line.strip()
        if LIST_LINE.match(line):
            in_list = True
        elif not blank and not line[:1].isspace():
            in_list = False
        code = bool(_CODE_INDENT.match(line)) and previous_blank and not in_list
        out.append(code)
        previous_blank = blank or code
    return out


def code_span_end(text: str, start: int) -> int:
    """Where the code span opening at ``start`` (a run of backticks) ends,
    or where the run ends when nothing closes it (then it is text)."""
    run = 0
    while start + run < len(text) and text[start + run] == "`":
        run += 1
    at = start + run
    # A code span ends with its paragraph.
    limit = text.find("\n\n", at)
    limit = len(text) if limit < 0 else limit
    while True:
        close = text.find("`" * run, at, limit)
        if close < 0:
            return start + run
        end = close + run
        if end < len(text) and text[end] == "`":
            at = end
            while at < len(text) and text[at] == "`":
                at += 1
            continue
        return end


def skip_inline(text: str, at: int) -> Optional[int]:
    """Where an escape, a code span, an autolink or a tag at ``at`` ends;
    None when none starts there."""
    char = text[at]
    if char == "\\" and at + 1 < len(text) and text[at + 1] in ESCAPABLE:
        return at + 2
    if char == "`":
        return code_span_end(text, at)
    if char == "<":
        found = _AUTOLINK_OR_TAG.match(text, at)
        return found.end() if found else None
    return None


def label_end(text: str, at: int) -> Optional[int]:
    """The ``]`` closing the bracket at ``at``, or None (a paragraph ends
    first, or nothing closes it)."""
    depth = 0
    index = at
    while index < len(text):
        if text.startswith("\n\n", index) or text.startswith("\n \n", index):
            return None
        skipped = skip_inline(text, index)
        if skipped is not None:
            index = skipped
            continue
        char = text[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def destination_end(text: str, at: int) -> Optional[int]:
    """The ``)`` closing an inline link's destination and title, opened
    at ``at``, or None."""
    index = at + 1
    length = len(text)

    def spaces(i: int) -> int:
        while i < length and text[i] in " \t\n":
            i += 1
        return i
    index = spaces(index)
    if index < length and text[index] == "<":
        close = text.find(">", index)
        if close < 0 or "\n" in text[index:close]:
            return None
        index = close + 1
    else:
        depth = 0
        while index < length and not text[index].isspace():
            char = text[index]
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            index += 1
    index = spaces(index)
    if index < length and text[index] in "\"'(":
        closer = ")" if text[index] == "(" else text[index]
        index += 1
        while index < length and text[index] != closer:
            index += 2 if text[index] == "\\" else 1
        index = spaces(index + 1)
    return index if index < length and text[index] == ")" else None


def descriptions(markdown: str) -> List[Tuple[int, int]]:
    """Where each picture's description is (start, end) in ``markdown``:
    an inline picture ``![...](...)`` or one with a full reference
    ``![...][label]``, outside code."""
    return [(start, end) for start, end, _after in pictures(markdown)]


def _code_ranges(markdown: str) -> List[Tuple[int, int]]:
    """Where the code lines of ``markdown`` are (start, end), in order."""
    lines = markdown.split("\n")
    code = code_lines(lines)
    starts, at = [], 0
    for line in lines:
        starts.append(at)
        at += len(line) + 1
    return [(starts[n], starts[n] + len(lines[n])) for n, is_code in enumerate(code) if is_code]


class _Blocked:
    """Ranges of a text to be stepped over, asked in rising order."""

    def __init__(self, ranges: List[Tuple[int, int]]):
        self.ranges = sorted(ranges)
        self.at = 0

    def end(self, index: int) -> Optional[int]:
        """The end of the range ``index`` is in, or None."""
        while self.at < len(self.ranges) and self.ranges[self.at][1] < index:
            self.at += 1
        if self.at < len(self.ranges) and self.ranges[self.at][0] <= index:
            return self.ranges[self.at][1]
        return None


def pictures(markdown: str) -> List[Tuple[int, int, int]]:
    """Each picture of ``markdown`` outside code: where its description
    starts and ends, and where the text after the picture starts."""
    blocked = _Blocked(_code_ranges(markdown))
    found: List[Tuple[int, int, int]] = []
    index = 0
    length = len(markdown)
    while index < length:
        nearest = _STARTS_INLINE.search(markdown, index)
        if nearest is None:
            break
        index = nearest.start()
        end = blocked.end(index)
        if end is not None:
            index = end + 1
            continue
        skipped = skip_inline(markdown, index)
        if skipped is not None:
            index = skipped
            continue
        if markdown.startswith("![", index):
            end = label_end(markdown, index + 1)
            if end is not None:
                after = markdown[end + 1:end + 2]
                close = destination_end(markdown, end + 1) if after == "(" else None
                if close is None and after == "[":
                    label = label_end(markdown, end + 1)
                    close = label if label is not None and label > end + 2 else None
                if close is not None:
                    found.append((index + 2, end, close + 1))
                    index = close + 1
                    continue
            index += 2
            continue
        index += 1
    return found


def escapes(markdown: str) -> List[int]:
    """Where each backslash escape of ``markdown`` is that a reader takes
    for the character after it (``\\*`` for a star that is no emphasis):
    outside code, an autolink or a tag, a picture's description and a
    link's address (a reference definition's too)."""
    blocked = _Blocked(_code_ranges(markdown)
                       + [definition.span(1) for definition in _DEFINITION.finditer(markdown)])
    found: List[int] = []
    index = 0
    while True:
        nearest = _STARTS_ESCAPE.search(markdown, index)
        if nearest is None:
            return found
        index = nearest.start()
        end = blocked.end(index)
        if end is not None:
            index = end + 1
        elif markdown.startswith("![", index):
            close = label_end(markdown, index + 1)
            index = index + 2 if close is None else close
        elif markdown.startswith("](", index):
            close = destination_end(markdown, index + 1)
            index = index + 2 if close is None else close + 1
        else:
            skipped = skip_inline(markdown, index)
            if skipped == index + 2 and markdown[index] == "\\":
                found.append(index)
            index = index + 1 if skipped is None else skipped
