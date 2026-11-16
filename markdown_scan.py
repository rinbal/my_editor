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
from typing import Dict, List, NamedTuple, Optional, Tuple

# A fence's line: three backticks or tildes or more, and what it says;
# a backtick fence says nothing with a backtick in it (CommonMark 4.5:
# "```npm run build```" is code in a line).
FENCE_LINE = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})(.*)$")
LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
# A list item with nothing in it, not even a task's box.
EMPTY_ITEM_LINE = re.compile(r"^ *(?:[-*+]|\d{1,9}[.)])[ \t]*$")
ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_AUTOLINK_PATTERN = r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|[^\s<>@]+@[^\s<>]+)>"
_AUTOLINK = re.compile(_AUTOLINK_PATTERN)
_AUTOLINK_OR_TAG = re.compile(
    _AUTOLINK_PATTERN + r"|<(?:/?[A-Za-z][\w-]*(?:\s[^<>]*)?/?|!--.*?--)>", re.S)
_CODE_INDENT = re.compile(r"^(?: {4}|\t)")
# Where an escape, a code span, an autolink or tag, or a picture may start.
_STARTS_INLINE = re.compile(r"[\\`<]|!\[")
# The same, and where a link may start.
_STARTS_INLINE_OR_LINK = re.compile(r"[\\`<\[]|!\[")
# The same, where brackets close, and where a paragraph ends.
_STARTS_INLINE_BRACKET_OR_BLANK = re.compile(r"[\\`<\[\]]|!\[|\n[ \t]*\n")
# A bracket that may open no picture: one without a "!" before it, or
# after an escaped one ("\\![a](u)" is a link).
_OWN_BRACKET = re.compile(r"(?<!!)\[|\\!\[")
_BACKTICKS = re.compile(r"`+")
_EMPHASIS_MARKS = ("*", "_", "~")
# Where an escape, or something an escape inside is no escape of, may
# start: code, an autolink or a tag, a picture's description, a link's
# address. A web address that holds an escape is none Qt's reader links
# ("www.x.com/\*y" is text), so its escapes are the text's.
_STARTS_ESCAPE = re.compile(r"[\\`<]|!\[|\]\(")
# A line's quote mark or list marker ("> ", "- ", "1. "), and all of
# them ("> - ").
_CONTAINER = r"[ \t]*(?:>|(?:[-*+]|\d{1,9}[.)])(?=[ \t]))"
_CONTAINERS = rf"(?:{_CONTAINER})*"
_TITLE = r"""(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|\((?:[^()\\\n]|\\.)*\))"""
# A link reference definition's label, as readers take one: no bracket
# in it that is not escaped, at most 999 characters, on one line or on
# lines that go on in its quote or list item ("> [a\n> b]"; a blank line
# or a list item ends it); a footnote's ("[^1]: note") is text.
_LABEL = (r"\[(?!\^)(?:[^\[\]\\\n]|\\."
          r"|\n[ \t>]*(?=[^ \t>\n])(?![-*+][ \t]|\d{1,9}[.)][ \t])){1,999}\]")
# What follows a link reference definition's label ("[a]: /url 'title'"),
# its address and title, in a quote or a list item too, the address or
# the title on the next line.
_DEFINITION = re.compile(
    rf"^{_CONTAINERS}[ \t]*{_LABEL}"
    rf"(:(?:[ \t]*\n{_CONTAINERS})?[^\n]*(?:\n{_CONTAINERS}[ \t]*{_TITLE}[ \t]*$)?)", re.M)
# A link reference definition Qt's reader does not keep as written: in a
# quote or a list item (the quote or the item goes, and the paragraph
# after it can join the one before), or with a title (the title goes);
# outside code (lossy_definition).
_ADDRESS_AFTER_LABEL = rf":[ \t]*(?:\n{_CONTAINERS}[ \t]*)?(?:<[^<>\n]*>|[^\s<]\S*)"
_TITLE_AFTER_ADDRESS = rf"(?:[ \t]+|[ \t]*\n{_CONTAINERS}[ \t]*){_TITLE}"
_LOSSY_DEFINITION = re.compile(
    rf"^(?:(?:{_CONTAINER})+[ \t]*{_LABEL}{_ADDRESS_AFTER_LABEL}(?:{_TITLE_AFTER_ADDRESS})?"
    rf"|{_CONTAINERS}[ \t]*{_LABEL}{_ADDRESS_AFTER_LABEL}{_TITLE_AFTER_ADDRESS})[ \t]*$", re.M)


# What a line of code is (code_lines): the line that opens a fence or the
# one that closes it, a line in a fence, a line of indented code.
_OPENS, _CLOSES, _FENCED, _INDENTED = "opens", "closes", "fenced", "indented"


def code_lines(lines: List[str]) -> List[str]:
    """For each line, what of a code block it is (_OPENS, _CLOSES, _FENCED,
    _INDENTED: after a blank line, outside a list), "" for none; in a
    quote too, after its marks (a fence ends with the quote it is in)."""
    out: List[str] = []
    fence, fence_depth = "", 0
    in_list = False
    previous_blank, previous_depth = True, 0
    for line in lines:
        if fence:
            inner = _inside_quotes(line, fence_depth)
            if inner is not None:
                found = FENCE_LINE.match(inner)
                if found and found.group(1)[0] == fence[0] and len(found.group(1)) >= len(fence) \
                        and not found.group(2).strip():
                    fence = ""
                out.append(_FENCED if fence else _CLOSES)
                previous_blank = True       # (indented code may follow a fence)
                continue
            fence = ""
        depth = _quote_depth(line)
        rest = _inside_quotes(line, depth)
        found = FENCE_LINE.match(rest)
        if found:
            fence, fence_depth = found.group(1), depth
            out.append(_OPENS)
            in_list, previous_depth = in_list and rest[:1].isspace(), depth
            continue
        blank = not rest.strip()
        if depth < previous_depth and not previous_blank and not blank \
                and not LIST_LINE.match(rest) and not _HEADING_OR_BREAK.fullmatch(rest):
            out.append("")              # the words of the quote's paragraph go on
            continue
        if depth != previous_depth:
            # A list ends with its quote; code may be the first of one.
            in_list, previous_blank = False, previous_blank or depth > previous_depth
        code = bool(_CODE_INDENT.match(rest)) and previous_blank and not in_list
        closed = _HEADING_OR_BREAK.fullmatch(rest) is not None
        if LIST_LINE.match(rest) and not code and not closed:
            in_list = True                          # ("    - a" there is code, "- - -" a divider)
        elif not blank and not rest[:1].isspace():
            in_list = False
        out.append(_INDENTED if code else "")
        # (Indented code may follow a heading or a divider right away.)
        previous_blank, previous_depth = blank or code or closed, depth
    return out


def code_tabs(markdown: str) -> int:
    """How many tabs the lines of code hold, fenced or indented, in a quote
    too: what a reading must keep of them. (Qt's reader makes some of
    those in a line's indent spaces, and a Makefile stops working.) The
    first four columns of a line of indented code are its indent, not its
    code; elsewhere a tab is counted wherever it stands, so that the count
    is never less than the code holds."""
    lines = markdown.split("\n")
    count = 0
    for line, kind in zip(lines, code_lines(lines)):
        if kind in (_FENCED, _INDENTED):
            rest = _inside_quotes(line, _quote_depth(line)) or ""
            if kind == _INDENTED:
                column = at = 0
                while at < len(rest) and column < 4 and rest[at] in " \t":
                    column = column + 1 if rest[at] == " " else (column // 4 + 1) * 4
                    at += 1
                rest = rest[at:]
            count += rest.count("\t")
    return count


def fence_words(lines: List[str]) -> List[Tuple[int, str]]:
    """What each fence says where it opens ("js title=x" of "```js
    title=x"), with its line's number."""
    found = []
    for number, kind in enumerate(code_lines(lines)):
        if kind == _OPENS:
            line = lines[number]
            fence = FENCE_LINE.match(_inside_quotes(line, _quote_depth(line)))
            found.append((number, fence.group(2).strip()))
    return found


# A quote's mark at a line's start, and a list item's start after the
# quote marks: its indent, its marker and the words after it.
_QUOTE_MARK = re.compile(r" {0,3}> ?")
_ITEM = re.compile(r"( *)([-*+]|\d{1,9}[.)])(?=[ \t]|$)[ \t]*(.*)")
_TASK_BOX = re.compile(r"\[[ xX]\][ \t]*")
# A line that may be such an item with nothing in it (empty_items).
_MAYBE_EMPTY_ITEM = re.compile(r"^[ >]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+\[[ xX]\])?[ \t]*$", re.M)
# A line no paragraph goes on past, after its indent: an ATX heading, or a
# thematic break ("---" under a paragraph is its heading's underline,
# which ends it all the same).
_HEADING_OR_BREAK = re.compile(r" *(?:#{1,6}(?:[ \t].*)?|([-*_])(?:[ \t]*\1){2,}[ \t]*)")
# A setext heading's underline after its indent ("---" is a break too).
_UNDERLINE = re.compile(r"(?:=+|-+)[ \t]*")
# A table's delimiter row ("| --- | :-: |"), and a pipe between cells.
DELIMITER_ROW = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*(?::?-+:?)?[ \t]*$")
_CELL_PIPE = re.compile(r"(?<!\\)\|")


def _inside_quotes(line: str, depth: int) -> Optional[str]:
    """``line`` without the marks of ``depth`` quotes; None when it has
    fewer."""
    at = 0
    for _quote in range(depth):
        found = _QUOTE_MARK.match(line, at)
        if found is None:
            return None
        at = found.end()
    return line[at:]


def _quote_depth(line: str) -> int:
    """How many quotes ``line`` stands in, by the marks it starts with."""
    depth, at = 0, 0
    while True:
        found = _QUOTE_MARK.match(line, at)
        if found is None:
            return depth
        depth, at = depth + 1, found.end()


def _indent(text: str) -> Optional[int]:
    """How many spaces ``text`` starts with; None for a tab among them,
    whose width depends on the column."""
    spaces = len(text) - len(text.lstrip(" "))
    return None if text[spaces:spaces + 1] == "\t" else spaces


def empty_items(markdown: str) -> Tuple[List[int], bool]:
    """Where the line of each list item with nothing in it ("-", "2.",
    "- [ ]") ends, outside code, when words written there leave the lists
    as they are (_empty_item_alone); and whether such an item stands
    where words would change them, or where it is not known whether it
    is one (_item_starts).

    Qt's reader leaves an item with nothing in it out (md4c gives it no
    text), and keeps one with a word in it."""
    if _MAYBE_EMPTY_ITEM.search(markdown) is None:
        return [], False
    lines = markdown.split("\n")
    code = code_lines(lines)
    starts = _item_starts(lines, code).starts
    places: List[int] = []
    unsafe = False
    start = 0
    for number, line in enumerate(lines):
        line_start, start = start, start + len(line) + 1
        if starts[number] is False:
            continue                    # no item, or a paragraph's words
        depth = _quote_depth(line)
        item = _ITEM.fullmatch(_inside_quotes(line, depth))
        words = item.group(3)
        box = _TASK_BOX.fullmatch(words)
        if words.strip() and box is None:
            continue
        if starts[number] is None:
            unsafe = True
        elif box is None:
            alone = _empty_item_alone(lines, code, number, depth,
                                      len(item.group(1)) + len(item.group(2)) + 1)
            if alone:
                places.append(line_start + len(line))
            elif alone is not None:
                unsafe = True
        elif _empty_item_alone(lines, code, number, depth, item.start(3), boxed=True):
            # (A task's box is a paragraph already: a line that would join
            # the words joins the box, and the task is not empty.)
            places.append(line_start + len(line))
    return places, unsafe


def misread_blocks(markdown: str) -> bool:
    """Whether ``markdown`` holds blocks Qt's reader reads otherwise than
    they are, outside code. A heading or a thematic break in a list item
    or a quote ("- # a", "- a\\n  ---", "- a\\n\\n  # b", "> ---"): it
    takes it out of its list or quote, or makes it the item's words in
    bold. A quote with nothing in it (">"). An item that opens with
    another list's item, a quote or a fence ("- ```", "- - a",
    "- > a"), what goes on in an item after a blank line ("- a\\n\\n  b":
    it takes that out of the list), a quote in an item ("- a\\n  > q"), and a marker alone, with spaces after
    it, under words ("a\\n* "). A heading with no words, a fence with nothing in it or
    one that starts the text with a blank line, which it leaves out. A
    table in a quote, under words ("Text\\n| a |\\n| - |") or with fewer
    columns than its header has, a pipe in a row's code ("| `a|b` |":
    code to it, the cell's end to others), and a heading or a fence right
    under a table's rows. And a list or a fence on the line that opens a
    quote, set in from the quote's mark (">   - a"): it reads that line
    as not set in (md4c takes the spaces for the mark's), so an item
    under it ("\\n>   - b") is one inside it, and the fence's code keeps
    the spaces it is set in with."""
    lines = markdown.split("\n")
    return _item_starts(lines, code_lines(lines)).misread


def ends_in_break(line: str) -> bool:
    """Whether ``line`` ends as a hard line break does: in two spaces or
    more, or in a backslash (one no backslash before it escapes)."""
    return line.endswith("  ") or (len(line) - len(line.rstrip("\\"))) % 2 == 1


def hard_breaks(markdown: str) -> List[int]:
    """The number of each line that ends as a hard line break does
    (ends_in_break), after words a paragraph may hold, with a line of
    words under it. Whether the paragraph goes on there, and the line's
    end breaks it, is the reader's to say. None where the line's end is
    no paragraph's: in code, a heading, a divider or an underline, a
    table's row, a reference definition, an item with nothing in it."""
    if "  \n" not in markdown and "\\\n" not in markdown:
        return []
    lines = markdown.split("\n")
    code = code_lines(lines)
    defined = _Blocked([definition.span() for definition in _DEFINITION.finditer(markdown)])
    found: List[int] = []
    start = 0
    for number, line in enumerate(lines[:-1]):
        start += len(line) + 1
        if not ends_in_break(line) or code[number] or defined.end(start - 2) is not None:
            continue
        rest = _inside_quotes(line, _quote_depth(line))
        under = lines[number + 1]
        if not _inside_quotes(under, _quote_depth(under)).strip():
            continue
        words = rest
        item = _ITEM.fullmatch(rest)
        if item is not None:
            words = _item_words(rest, item)[1]
            words = words[_TASK_BOX.match(words).end():] if _TASK_BOX.match(words) else words
        underline = (_UNDERLINE.fullmatch(words.strip()) is not None and number > 0
                     and bool(lines[number - 1].strip()))
        if not (not words.strip() or _HEADING_OR_BREAK.fullmatch(words) or underline
                or DELIMITER_ROW.match(rest) or words.rstrip().endswith("|")):
            found.append(number)
    return found


class _Line(NamedTuple):
    """What set_apart keeps of the last line with something on it."""

    depth: int                      # how many quotes it stands in
    code: str                       # what of a code block it is (code_lines)


def set_apart(markdown: str) -> Tuple[List[Tuple[int, int]], bool]:
    """Where the words, or the code, start of each block that Qt's reader
    runs into the block before it: a code block right after another, a
    quote after another's end, a list item after a blank line (a loose
    list's). With each, how many of the quotes the two stand in end
    between them (those the blank line does not stand in). And whether
    such a block has
    no words to stand before: a fence with no line in it, an item with
    nothing in it, or one that starts with a heading, a divider, a
    table's row or a quote."""
    lines = markdown.split("\n")
    code = code_lines(lines)
    lists = _item_starts(lines, code)
    places: List[Tuple[int, int]] = []
    unsafe = False
    before: Optional[_Line] = None
    gap: Optional[int] = None           # the fewest quotes a blank line since stands in
    start = 0
    for number, line in enumerate(lines):
        line_start, start = start, start + len(line) + 1
        kind = code[number]
        if kind in (_FENCED, _CLOSES):
            before = _Line(before.depth, kind)      # (in the quotes of its fence)
            continue
        depth = _quote_depth(line)
        rest = _inside_quotes(line, depth)
        if not kind and not rest.strip():
            gap = depth if gap is None else min(gap, depth)
            continue
        kept: Optional[int] = None
        if before is not None:
            shared = min(before.depth, depth)
            if kind and before.code in (_CLOSES, _INDENTED) \
                    and not kind == before.code == _INDENTED:   # (not indented code going on)
                kept = shared if gap is None else min(gap, shared)
            elif gap is not None and gap < shared:
                kept = gap
            elif gap is not None and not kind and lists.starts[number] is not False \
                    and lists.listed[number]:
                kept = min(gap, shared)
        if kept is not None:
            at = _apart_place(lines, number, depth, rest, kind,
                              number + 1 < len(lines) and code[number + 1] == _FENCED)
            if at is None:
                unsafe = True
            else:
                places.append((line_start + at, shared - kept))
        if gap is None and before is not None and not kind and lists.starts[number] is False \
                and _HEADING_OR_BREAK.fullmatch(rest) is None:
            depth = max(depth, before.depth)    # (the words of a quote's paragraph go on)
        before, gap = _Line(depth, kind), None
    return places, unsafe


def _apart_place(lines: List[str], number: int, depth: int, rest: str,
                 kind: str, holds: bool) -> Optional[int]:
    """Where, counted from the start of line ``number`` (in ``depth``
    quotes, ``rest`` after their marks, ``kind`` of code), the words or
    the code of the block start that starts on it; for a fence, in its
    first line (``holds``: it has one). None when there are none
    (_words_start)."""
    line = lines[number]
    if kind == _INDENTED:
        return len(line) - len(rest) + _CODE_INDENT.match(rest).end()
    if kind == _OPENS:
        first = _inside_quotes(lines[number + 1], depth) if holds else None
        if first is None:
            return None                 # (nothing in the fence)
        # (A fence set in takes as many spaces off its lines.)
        return (len(line) + 1 + len(lines[number + 1]) - len(first)
                + min(_indent(rest) or 0, _indent(first) or 0))
    words = _words_start(rest)
    return None if words is None else len(line) - len(rest) + words


def _words_start(rest: str) -> Optional[int]:
    """Where in ``rest`` (a line after its quote marks) the words of the
    block start that starts on it: after the markers of the items it
    starts and a task's box. None when no words start it: nothing, a
    heading, a divider, a table's row, a quote or a fence."""
    words = rest.lstrip(" \t")
    item = _ITEM.fullmatch(rest)
    if item is not None:
        words = _item_words(rest, item)[1]
        box = _TASK_BOX.match(words)
        if box is not None:
            words = words[box.end():]
    if (not words.strip() or _HEADING_OR_BREAK.fullmatch(words)
            or words.startswith((">", "|")) or FENCE_LINE.match(words)):
        return None
    return len(rest) - len(words)


class _Lists(NamedTuple):
    """What _item_starts tells of a text's lines."""

    starts: List[Optional[bool]]    # whether an item starts on the line (None: not known)
    listed: List[bool]              # whether an item is open, in its quote, where the line comes
    misread: bool                   # whether Qt's reader reads a block otherwise (misread_blocks)


def _item_starts(lines: List[str], code: List[str]) -> _Lists:
    """For each line, whether a list item starts on it, by the lines above
    it as CommonMark reads them: False for a line that is no item, or the
    words of the paragraph going on above it ("Text\\n-" is a heading,
    "Text\\n*" one paragraph, "- a\\n  -" a heading in the item); None
    where this cannot tell. An item with nothing in it, or numbered other
    than 1, does not interrupt a paragraph in its place: in its quote, at
    the words of the paragraph's item or right of them ("- a\\nb\\n-" is
    two items, "> a\\n-" a quote and an item). And whether Qt's reader
    reads a block otherwise than it is (misread_blocks)."""
    starts: List[Optional[bool]] = []
    listed: List[bool] = []
    # The paragraph going on: its quote depth and the column of the words
    # of the item it is in (0: in none; None: not known), its first and
    # its latest line, and how many it has; the quote depth of a table
    # going on.
    paragraph: Optional[Tuple[int, Optional[int]]] = None
    first, latest, count = "", "", 0
    table: Optional[int] = None
    # The columns of the words of the items open, outermost first, in the
    # quote the line before stands in (an item ends with its quote).
    items: List[int] = []
    items_depth = 0
    # How many quotes the line before stands in, and the columns an item
    # stands in that Qt's reader takes for one inside the quote's first.
    quoted = 0
    shifted: Optional[Tuple[int, int]] = None
    # Whether a blank line, in the quote, came since the last line of words.
    gap = False
    # For each quote open, outermost first, whether anything stands in it.
    held: List[bool] = []
    misread = False
    for number, line in enumerate(lines):
        starts.append(False)
        depth = _quote_depth(line)
        rest = _inside_quotes(line, depth)
        item = None if code[number] else _ITEM.fullmatch(rest)
        heading = not code[number] and _HEADING_OR_BREAK.fullmatch(rest) is not None
        # The words of a quote's paragraph go on in a line without its
        # marks: the quote and its items stay open.
        lazy = (paragraph is not None and paragraph[0] > depth and bool(rest.strip())
                and not code[number] and item is None and not heading)
        opens = depth > quoted
        if not lazy:
            misread = _close_quotes(held, depth) or misread
            held += [False] * (depth - len(held))
        if held and (lazy or rest.strip()):
            held[-1] = True
        if not lazy:
            if depth > quoted and items and items_depth == quoted and not code[number]:
                # A quote that opens in an item (its mark at the item's
                # words or right of them): Qt's reader takes it out of the
                # list.
                at = _indent(_inside_quotes(line, quoted) or "")
                misread = misread or at is None or at >= items[0]
            quoted = depth
            if depth != items_depth:
                items, items_depth, shifted, gap = [], depth, None, False
        listed.append(bool(items))
        indent = _indent(rest)
        if gap and items and item is None and rest.strip() and (indent is None or indent >= items[0]):
            # What goes on in an item after a blank line (its second
            # paragraph, a quote or code in it): Qt's reader takes it out
            # of the list.
            misread = True
        if rest.strip():
            gap = False
        elif not code[number]:
            gap = True
        if code[number] or not rest.strip():
            if code[number] == _OPENS:
                # A fence right under a table is a row of it to Qt's reader.
                misread = (misread or table is not None
                           or _fence_loses(lines, code, number, opens and bool(indent)))
            paragraph = table = None
            if code[number] and indent is not None:
                _close_items(items, indent)
            continue
        if indent is None:              # a tab in the indent: its column is not known
            starts[number] = None if item else False
            paragraph, table = (depth, None), None
            continue
        in_place = paragraph is not None and paragraph[0] == depth
        column = paragraph[1] if in_place else None
        if column is not None and _UNDERLINE.fullmatch(rest.lstrip(" ")) \
                and column <= indent < column + 4:
            # The paragraph's heading (a "-" under its words is no item);
            # in an item when the paragraph's words are.
            misread = misread or column > 0 or depth > 0
            paragraph, table = None, None
            continue
        if not (lazy or (in_place and item is None and not heading)):
            _close_items(items, indent)     # (words going on keep the items open)
            if shifted is not None and indent < shifted[0]:
                shifted = None
        if shifted is not None and item is not None and shifted[0] <= indent < shifted[1]:
            misread = True
        if table == depth and heading and rest.lstrip(" ").startswith("#"):
            misread = True              # (a row of the table above it, to Qt's reader)
        if heading:
            misread = (misread or depth > 0 or bool(items) and indent - items[-1] < 4
                       or _EMPTY_HEADING.fullmatch(rest) is not None)
            # (Indented 4 or more it may be code, or words going on.)
            paragraph, table = (None if indent < 4 else (depth, None)), None
            continue
        if item is not None:
            start = _item_start(item, paragraph if in_place else None, indent)
            starts[number] = start
            if start is False:
                # A marker alone with spaces after it: Qt's reader makes the
                # words above an item ("a\\n* "), or ends the paragraph at
                # the line break ("a\\n2.  ").
                spaces = 0 if item.group(3).strip() else len(rest) - item.end(2)
                misread = misread or spaces > (0 if _may_interrupt(item.group(2)) else 1)
                count, latest = count + 1, rest     # the paragraph's words
                continue
            columns, words = _item_words(rest, item)
            items += columns
            table = None
            if opens and indent > 0:
                # (Read as not set in: its words ``indent`` columns left.)
                shifted = (columns[0] - indent, columns[0])
            # A heading or a break in it; and an item that opens with another
            # list's item, a quote or code ("- - a", "- > a", "- ```"): Qt's
            # reader hangs that list under the item before, takes the quote
            # or the code out, leaves an empty block out with its item.
            inside = _HEADING_OR_BREAK.fullmatch(words) is not None
            misread = (misread or inside or len(columns) > 1 or words.startswith(">")
                       or FENCE_LINE.match(words) is not None)
            if start is None:
                paragraph = (depth, None)
            elif not words.strip() or inside:
                paragraph = None            # nothing in it, or no paragraph
            else:
                # The column of its words, not known when they open a quote,
                # a fence or HTML.
                opened = (words.startswith(">") or FENCE_LINE.match(words) is not None
                          or (words.startswith("<") and _AUTOLINK.match(words) is None))
                paragraph = (depth, None if opened else columns[-1])
                first, latest, count = words, words, 1
            continue
        if table == depth:              # a row of the table
            misread = misread or _pipe_in_code(rest)
            continue
        table = None
        if in_place:
            if DELIMITER_ROW.match(rest):
                # A table when the line above is its header alone, as
                # readers agree (Qt's takes it out of a quote). Else none to
                # Qt's reader where others see one (a header under words),
                # or one that loses the cells its header has more.
                header = count == 1 and "|" in first
                if header and len(_cells(first)) == len(_cells(rest)):
                    paragraph, table = None, depth
                    misread = misread or depth > 0
                else:
                    paragraph = (depth, None)
                    misread = misread or "|" in latest
            else:
                count, latest = count + 1, rest     # words going on
            continue
        if paragraph is not None and paragraph[0] > depth:
            count, latest = count + 1, rest     # words of a quote's paragraph, its marks left out
            continue
        paragraph, first, latest, count = (depth, 0 if indent == 0 else None), rest, rest, 1
    misread = _close_quotes(held, 0) or misread
    return _Lists(starts, listed, misread)


def _close_quotes(held: List[bool], depth: int) -> bool:
    """End the quotes in ``held`` deeper than ``depth``; whether one of
    them held nothing (Qt's reader leaves an empty quote out, and runs the
    blocks on either side of it together). A quote holds what a quote in
    it holds."""
    empty = False
    while len(held) > depth:
        holds = held.pop()
        empty = empty or not holds
        if holds and held:
            held[-1] = True
    return empty


# A heading with no words ("#", "## ##").
_EMPTY_HEADING = re.compile(r" *#{1,6}[ \t]*(?:#+[ \t]*)?")


def _fence_loses(lines: List[str], code: List[str], number: int, shifted: bool) -> bool:
    """Whether Qt's reader leaves out what the fence opened on line
    ``number`` holds: all of it when no line is in it; its first line
    when that is blank and nothing stands before the fence (the
    document's first, empty paragraph is taken for it). One blank line
    alone it keeps there where the text ends with the fence. Or adds to
    it: ``shifted``, set in on the line that opens its quote, it is read
    as not set in, and its lines keep the spaces they are set in with."""
    last = number + 1
    while last < len(lines) and code[last] == _FENCED:
        last += 1
    inside = lines[number + 1:last]
    if last == len(lines) and inside[-1:] == [""]:
        inside.pop()                    # (what stands after the text's last line end)
    if not inside:
        return True
    depth = _quote_depth(lines[number])
    if shifted and any((_inside_quotes(line, depth) or "").startswith(" ") for line in inside):
        return True
    if inside[0].strip("> \t") or any(line.strip() for line in lines[:number]):
        return False
    return not (len(inside) == 1 and not any(line.strip() for line in lines[last + 1:]))


def _close_items(items: List[int], indent: int) -> None:
    """End the items a line ``indent`` spaces in stands left of the words of."""
    while items and indent < items[-1]:
        items.pop()


def _item_start(item: re.Match, paragraph: Optional[Tuple[int, Optional[int]]],
                indent: int) -> Optional[bool]:
    """Whether the item matched (``indent`` spaces in) starts one, by the
    ``paragraph`` going on in its quote (None for none): it does when it
    stands left of the words of the paragraph's item, or interrupts the
    paragraph (has words, is bulleted or numbered 1, and stands less than
    4 columns right of them); None when the paragraph's item is not known
    and it may not interrupt."""
    if paragraph is None:
        return True
    interrupts = bool(item.group(3).strip()) and _may_interrupt(item.group(2))
    column = paragraph[1]
    if column is None:
        return True if interrupts and indent < 4 else None
    return indent < column or (interrupts and indent - column < 4)


def _may_interrupt(marker: str) -> bool:
    """Whether an item with this marker may interrupt a paragraph: a
    bulleted one, or one numbered 1."""
    return marker[-1] not in ".)" or int(marker[:-1]) == 1


def _item_words(rest: str, item: re.Match) -> Tuple[List[int], str]:
    """The columns of the words of the item matched on the line ``rest``
    and of each item it starts with ("- - a": two), and the words after
    their markers (an item's with nothing in it: right of its marker)."""
    columns: List[int] = []
    while True:
        words = item.group(3)
        columns.append(item.start(3) if words.strip() else item.end(2) + 1)
        if not words.strip() or _HEADING_OR_BREAK.fullmatch(words):
            return columns, words
        inner = _ITEM.fullmatch(rest, item.start(3))
        if inner is None:
            return columns, words
        item = inner


def _cells(row: str) -> List[str]:
    """The cells of a table's row, its outer pipes left out."""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return _CELL_PIPE.split(row)


def _pipe_in_code(row: str) -> bool:
    """Whether a pipe stands in code in a table's ``row``."""
    at = row.find("`")
    while at >= 0:
        end = code_span_end(row, at)
        if _CELL_PIPE.search(row, at, end):
            return True
        at = row.find("`", end)
    return False


def _empty_item_alone(lines: List[str], code: List[str], number: int, depth: int,
                      column: int, boxed: bool = False) -> Optional[bool]:
    """Whether words written in the empty item on line ``number`` (in
    ``depth`` quotes, its words' column ``column``) stay alone: True when
    the text ends after it, a blank line ends them, or an item follows
    (one inside it numbered 1 or bulleted, as an item must be to follow a
    paragraph); False when a line would join them (one left of the
    column, as a paragraph's lazy line, or a second paragraph, which an
    item that starts empty cannot have, but one ``boxed`` can: a task's
    box is its first); None when the next line holds the item's words
    (it is not empty)."""
    following = number + 1
    if following == len(lines):
        return True
    if code[following]:
        return False
    rest = _inside_quotes(lines[following], depth)
    if rest is None and lines[following].strip():
        # Out of the quote: an item there starts a list of its own; other
        # words are a lazy line of the quote's paragraph.
        own = _inside_quotes(lines[following], _quote_depth(lines[following]))
        return _ITEM.fullmatch(own or "") is not None
    if rest is None or not rest.strip():
        # A blank line ends the words; the first line after it with words
        # is the item's second paragraph when it stands in the column.
        if boxed:
            return True
        for later in lines[following + 1:]:
            inside = _inside_quotes(later, depth)
            if inside is None:
                return True             # the quote ended
            if inside.strip():
                indent = _indent(inside)
                return indent is not None and indent < column
        return True
    indent = _indent(rest)
    if indent is None:
        return False
    item = _ITEM.fullmatch(rest)
    if item is None:
        return None if indent >= column else False
    if indent < column:
        return True                     # an item of this list or one around it
    return indent < column + 4 and _may_interrupt(item.group(2))


def code_span_end(text: str, start: int, found: Optional[Found] = None) -> int:
    """Where the code span opening at ``start`` (a run of backticks) ends,
    or where the run ends when nothing closes it (then it is text);
    ``found`` keeps where a paragraph's end and backticks were found."""
    run = 0
    while start + run < len(text) and text[start + run] == "`":
        run += 1
    at = start + run
    # A code span ends with its paragraph (_PARAGRAPH_END: a lone backtick
    # in each of two items, rows or blocks of a quote is no code).
    limit = _paragraph_end(text, at, found)
    ticks = "`" * run
    while True:
        close = _next_of(text, ticks, at, found)
        if close >= limit:
            return start + run
        end = close + run
        if end < len(text) and text[end] == "`":
            at = end
            while at < len(text) and text[at] == "`":
                at += 1
            continue
        return end


def skip_inline(text: str, at: int, found: Optional[Found] = None) -> Optional[int]:
    """Where an escape, a code span, an autolink or a tag at ``at`` ends;
    None when none starts there. ``found`` as for code_span_end."""
    char = text[at]
    if char == "\\" and at + 1 < len(text) and text[at + 1] in ESCAPABLE:
        return at + 2
    if char == "`":
        return code_span_end(text, at, found)
    if char == "<":
        found = _AUTOLINK_OR_TAG.match(text, at)
        return found.end() if found else None
    return None


class Found:
    """What the scans of one text found: the ``]`` closing each ``[`` a
    scan for a bracket passed (None: nothing does), where each address
    a scan passed ends (_bare_address_end), and where the closer of a
    title, a ``>``, a line's or a paragraph's end and backticks were
    looked for last. A scan that starts where an earlier one passed steps
    as it did from there on, so the earlier answer is its own: a text full
    of openers nothing closes is read once, not once for each of them to
    the end of the paragraph or the text."""

    def __init__(self) -> None:
        self.brackets: Dict[int, Optional[int]] = {}
        self.addresses: Dict[int, int] = {}
        self.closers: Dict[str, Tuple[int, int]] = {}
        self.next_of: Dict[str, Tuple[int, int]] = {}


def label_end(text: str, at: int, found: Optional[Found] = None) -> Optional[int]:
    """The ``]`` closing the bracket at ``at``, or None (a paragraph ends
    first, or nothing closes it); ``found`` keeps what this scan passed."""
    known = found.brackets if found is not None else {}
    if at in known:
        return known[at]
    opened: List[int] = []
    index = at
    while index < len(text):
        if text.startswith("\n\n", index) or text.startswith("\n \n", index):
            break
        skipped = skip_inline(text, index, found)
        if skipped is not None:
            index = skipped
            continue
        char = text[index]
        if char == "[":
            opened.append(index)
        elif char == "]" and opened:
            known[opened.pop()] = index
            if not opened:
                return index
        index += 1
    for start in opened:
        known[start] = None
    return None


def _closer_at(text: str, closer: str, start: int, found: Optional[Found]) -> int:
    """Where the first ``closer`` no backslash escapes is from ``start``
    (the text's length or past it: none)."""
    known = found.closers.get(closer) if found is not None else None
    if known is not None and known[0] <= start <= known[1]:
        return known[1]
    index = start
    while index < len(text) and text[index] != closer:
        index += 2 if text[index] == "\\" else 1
    if found is not None:
        found.closers[closer] = (start, index)
    return index


def _next_of(text: str, char: str, start: int, found: Optional[Found]) -> int:
    """Where the first ``char`` is from ``start`` (the text's length: none)."""
    known = found.next_of.get(char) if found is not None else None
    if known is not None and known[0] <= start <= known[1]:
        return known[1]
    index = text.find(char, start)
    if index < 0:
        index = len(text)
    if found is not None:
        found.next_of[char] = (start, index)
    return index


# Where a paragraph ends: at a line that is blank (in its quote too), or
# starts a list item, a table's row, a heading or a fence, or is a
# heading's underline or a divider.
_PARAGRAPH_END = re.compile(
    r"\n(?=[ \t>]*(?:\n|$)|[ \t>]*(?:[-*+]|\d{1,9}[.)])[ \t]|[ \t>]*\||[ \t>]*#{1,6}(?:[ \t\n]|$)"
    r"|[ \t>]*(?:`{3,}|~{3,})|[ \t>]*(?:=+|-+|(?:[*_][ \t]*){3,})[ \t]*(?:\n|$))")
_HEADING_LINE = re.compile(r"[ \t>]*#{1,6}(?:[ \t]|$)", re.MULTILINE)


def _paragraph_end(text: str, start: int, found: Optional[Found]) -> int:
    """Where the paragraph ``start`` stands in ends (the text's length:
    with the text), kept as _next_of keeps what it found."""
    known = found.next_of.get("paragraph") if found is not None else None
    if known is not None and known[0] <= start <= known[1]:
        return known[1]
    line_start = text.rfind("\n", 0, start) + 1
    if _HEADING_LINE.match(text, line_start):
        end = text.find("\n", start)               # a heading is its line
        index = len(text) if end < 0 else end
    else:
        end = _PARAGRAPH_END.search(text, start)
        index = len(text) if end is None else end.start()
    if found is not None:
        found.next_of["paragraph"] = (start, index)
    return index


def _bare_address_end(text: str, start: int, found: Optional[Found]) -> int:
    """Where an address not in <...> that starts at ``start`` ends: at a
    space, the text's end, or the first ``)`` that closes more than it
    opened.

    One pass over the characters up to the next space answers every
    address that starts after a ``(`` among them, as a link's does
    ("](x](x"). ``found`` keeps the answers."""
    known = found.addresses if found is not None else {}
    if start in known:
        return known[start]
    length = len(text)
    waiting: Dict[int, List[int]] = {0: [start]}
    depth = 0
    index = start
    while index < length and not text[index].isspace():
        char = text[index]
        if char == "\\" and index + 1 < length:
            index += 2
            continue
        if char == "(":
            depth += 1
            waiting.setdefault(depth, []).append(index + 1)
        elif char == ")":
            for begun in waiting.pop(depth, ()):
                known[begun] = index
            depth -= 1
        index += 1
    for starts in waiting.values():
        for begun in starts:
            known[begun] = index
    return known[start]


def destination_end(text: str, at: int, found: Optional[Found] = None) -> Optional[int]:
    """The ``)`` closing an inline link's destination and title, opened
    at ``at``, or None; ``found`` as for label_end."""
    ended = _destination(text, at, found)
    return None if ended is None else ended[0]


def titled_link(markdown: str) -> bool:
    """Whether an inline link or picture outside code has a title
    ("[a](/u 'Titel')"), which Qt's reader drops."""
    blocked = _Blocked(_code_ranges(markdown))
    scanned = Found()
    index = 0
    while True:
        nearest = _STARTS_ESCAPE.search(markdown, index)
        if nearest is None:
            return False
        index = nearest.start()
        end = blocked.end(index)
        if end is not None:
            index = end + 1
        elif markdown.startswith("](", index):
            ended = _destination(markdown, index + 1, scanned)
            if ended is not None and ended[1]:
                return True
            index += 2
        else:
            skipped = skip_inline(markdown, index, scanned)
            index = index + 1 if skipped is None else skipped


def _destination(text: str, at: int, found: Optional[Found]) -> Optional[Tuple[int, bool]]:
    """destination_end's ``)``, and whether a title stands before it."""
    index = at + 1
    length = len(text)

    def spaces(i: int) -> int:
        while i < length and text[i] in " \t\n":
            i += 1
        return i
    index = spaces(index)
    if index < length and text[index] == "<":
        # An address in <...> ends on its line.
        close = _next_of(text, ">", index, found)
        if close >= _next_of(text, "\n", index, found):
            return None
        index = close + 1
    else:
        index = _bare_address_end(text, index, found)
    index = spaces(index)
    titled = index < length and text[index] in "\"'("
    if titled:
        closer = ")" if text[index] == "(" else text[index]
        index = spaces(_closer_at(text, closer, index + 1, found) + 1)
    return (index, titled) if index < length and text[index] == ")" else None


def descriptions(markdown: str) -> List[Tuple[int, int]]:
    """Where each picture's description is (start, end) in ``markdown``:
    an inline picture ``![...](...)`` or one with a reference
    (``![...][label]``, ``![...][]``), outside code."""
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
    scanned = Found()
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
        skipped = skip_inline(markdown, index, scanned)
        if skipped is not None:
            index = skipped
            continue
        if markdown.startswith("![", index):
            end = label_end(markdown, index + 1, scanned)
            if end is not None:
                after = markdown[end + 1:end + 2]
                close = destination_end(markdown, end + 1, scanned) if after == "(" else None
                if close is None and after == "[":
                    # (With its label, or with "[]": its description is it.)
                    close = label_end(markdown, end + 1, scanned)
                if close is not None:
                    found.append((index + 2, end, close + 1))
                    index = close + 1
                    continue
            index += 2
            continue
        index += 1
    return found


def bracketed(markdown: str) -> Tuple[List[int], List[Tuple[int, int]], List[Tuple[int, int]]]:
    """What of ``markdown`` stands inside brackets, outside code, where the
    words of a link may hold it: each picture, by where it starts (its
    "!"; "[![a](p.png)](u)", "[see ![a](p.png)](u)"), and each tag on
    one line ("[x <y>](u)"), by where it starts and ends. A tag in a
    picture's description is the description's. And the brackets, by
    where their words start and end, that an address or a second bracket
    follows and whose words hold what may be a web or an email address
    ("[see https://a.example](u)"): Qt's reader links such a one by
    itself, in the link's words too."""
    if _OWN_BRACKET.search(markdown) is None:
        return [], [], []               # no bracket but the pictures' own
    found = pictures(markdown)
    after = {start - 2: end for start, _end, end in found}
    blocked = _Blocked(_code_ranges(markdown))
    scanned = Found()
    pairs: List[Tuple[int, int]] = []
    tags: List[Tuple[int, int]] = []
    opened: List[int] = []
    index = 0
    while True:
        nearest = _STARTS_INLINE_BRACKET_OR_BLANK.search(markdown, index)
        if nearest is None:
            break
        index = nearest.start()
        end = blocked.end(index)
        if end is not None:
            index = end + 1
            continue
        char = markdown[index]
        if index in after:
            # A picture is passed whole, as a reader takes it first: its
            # description's brackets and its address's are its own.
            index = after[index]
        elif char == "\n":
            opened.clear()              # a paragraph ends
            index = nearest.end()
        elif char == "[":
            opened.append(index)
            index += 1
        elif char == "]":
            if opened:
                pairs.append((opened.pop(), index))
            index += 1
        else:
            skipped = skip_inline(markdown, index, scanned)
            if (skipped is not None and char == "<" and _AUTOLINK.match(markdown, index) is None
                    and "\n" not in markdown[index:skipped]):
                tags.append((index, skipped))
            index = index + 1 if skipped is None else skipped
    pairs.sort()
    held = set(_held(tags, pairs))
    return (_held([(start - 2, end) for start, _end, end in found], pairs),
            [(start, end) for start, end in tags if start in held],
            [(opener + 1, closer) for opener, closer in pairs
             if markdown[closer + 1:closer + 2] in ("(", "[")
             and _ADDRESS.search(markdown, opener + 1, closer)])


def _held(spans: List[Tuple[int, int]], pairs: List[Tuple[int, int]]) -> List[int]:
    """The start of each of ``spans`` (start, end; rising, none holding a
    bracket) that a pair of brackets of ``pairs`` (by opener; nested or
    apart) opens before and closes after."""
    out: List[int] = []
    furthest, at = -1, 0
    for start, end in spans:
        while at < len(pairs) and pairs[at][0] < start:
            furthest = max(furthest, pairs[at][1])
            at += 1
        if furthest >= end:
            out.append(start)
    return out


def lossy_definition(markdown: str) -> bool:
    """Whether a link reference definition Qt's reader does not keep as
    written stands in ``markdown``, outside code: one shown in a fence or
    as indented code ("> [r]: /u" in a README) is text."""
    blocked = _Blocked(_code_ranges(markdown))
    return any(blocked.end(found.start()) is None
               for found in _LOSSY_DEFINITION.finditer(markdown))


def emphasized_code(markdown: str) -> bool:
    """Whether emphasis or strike-through holds no words of its own, only
    code spans, pictures and links that hold only them ("*`code`*",
    "**[`npm`](u)**", "*`c`![a](p.png)*"), outside code. Qt's reader gives
    code a font of its own and a picture none, without the emphasis, and
    what is written then reads back the same, so the check by reading it
    twice does not see the loss (with words in it, it does)."""
    blocked = _Blocked(_code_ranges(markdown))
    scanned = Found()
    index = 0
    while True:
        nearest = _STARTS_INLINE_OR_LINK.search(markdown, index)
        if nearest is None:
            return False
        index = nearest.start()
        end = blocked.end(index)
        if end is not None:
            index = end + 1
            continue
        if _after_emphasis_mark(markdown, index):
            close = _wordless_end(markdown, index, scanned)
            if close > index and markdown[close:close + 1] in _EMPHASIS_MARKS:
                return True
        skipped = skip_inline(markdown, index, scanned)
        index = index + 1 if skipped is None else skipped


def _after_emphasis_mark(text: str, at: int) -> bool:
    """Whether an emphasis or strike-through mark is right before ``at``,
    not escaped (an odd number of backslashes before it escapes it)."""
    if text[at - 1:at] not in _EMPHASIS_MARKS:
        return False
    start = at - 1
    while start > 0 and text[start - 1] == "\\":
        start -= 1
    return (at - 1 - start) % 2 == 0


def _wordless_end(text: str, at: int, found: Found, links: bool = True) -> int:
    """Where the code spans, pictures and (``links``) links holding only
    code spans and pictures that follow each other from ``at`` end
    (``at``: none starts there). A link holds no link."""
    index = at
    while index < len(text):
        char = text[index]
        if char == "`":
            end = code_span_end(text, index, found)
            if end == _BACKTICKS.match(text, index).end():
                break               # backticks nothing closes: text
            index = end
            continue
        picture = char == "!" and text.startswith("![", index)
        if not (picture or (links and char == "[")):
            break
        label = label_end(text, index + picture, found)
        close = None if label is None or text[label + 1:label + 2] != "(" else \
            destination_end(text, label + 1, found)
        if close is None or not (picture or _wordless_end(text, index + 1, found, False) == label):
            break
        index = close + 1
    return index


def escapes(markdown: str) -> List[int]:
    """Where each backslash escape of ``markdown`` is that a reader takes
    for the character after it (``\\*`` for a star that is no emphasis):
    outside code, an autolink or a tag, a picture's description and a
    link's address (a reference definition's too)."""
    return _inline(markdown, False).escapes


def autolinks(markdown: str) -> Tuple[List[int], bool]:
    """Where each autolink of ``markdown`` starts ("<https://a.example>",
    "<a@b.example>": its "<"), where escapes are (escapes); and whether a
    link has no words or no address ("[](u)", "[x]()"): Qt's reader
    leaves the one out, and the other's link."""
    found = _inline(markdown, False)
    return found.autolinks, found.wordless


def entities(markdown: str) -> List[Tuple[int, int, bool]]:
    """Where each entity of ``markdown`` is (start, end) that a reader
    takes for its character ("&amp;" for "&"), where escapes are
    (escapes), but in a word that may be a web or an email address (Qt's
    reader links none with an entity in it: _ADDRESS); and whether it is
    shown,
    not in a reference definition's label, which the reader takes away."""
    labels = _Blocked([(definition.start(), definition.start(1))
                       for definition in _DEFINITION.finditer(markdown)])
    addresses = _Blocked([word.span() for word in _WORD.finditer(markdown)
                          if _ADDRESS.search(word.group(0))])
    return [(start, end, labels.end(start) is None)
            for start, end in _inline(markdown, True).entities
            if addresses.end(start) is None]


# An entity as Qt's reader takes one (md4c's md_is_entity_str): "&", then a
# name of 2 to 48 letters and digits, the first a letter, or "#" and 1 to
# 7 digits, or "#x" and 1 to 6 hexadecimal ones; then ";".
_ENTITY = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,47}|#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6});")
_STARTS_ESCAPE_OR_ENTITY = re.compile(r"[\\`<&]|!\[|\]\(")
# A word, up to a space or a bracket (a link's address after its words
# is no part of them), and what makes it a web or an email address that
# Qt's reader may link by itself.
_WORD = re.compile(r"[^ \t\n\[\]()<>]+")
_ADDRESS = re.compile(r"://|www\.|@[^@]*\.", re.IGNORECASE)


def _wordless(text: str, close: int) -> bool:
    """Whether the brackets that end at ``close`` hold no words ("[]",
    "[ ]"), and are no picture's nor escaped."""
    at = close - 1
    while at >= 0 and text[at] in " \t":
        at -= 1
    return at >= 0 and text[at] == "[" and text[at - 1:at] not in ("!", "\\")


class _Inline(NamedTuple):
    """What _inline finds between the code, the descriptions and the
    addresses of a text."""

    escapes: List[int]                  # where each escape is (escapes)
    entities: List[Tuple[int, int]]     # each entity, when asked for (entities)
    autolinks: List[int]                # where each autolink starts (autolinks)
    wordless: bool                      # whether a link has no words, or no address


def _inline(markdown: str, entities_too: bool) -> _Inline:
    """One walk for escapes, entities and autolinks."""
    blocked = _Blocked(_code_ranges(markdown)
                       + [definition.span(1) for definition in _DEFINITION.finditer(markdown)])
    found = _Inline([], [], [], False)
    starts = _STARTS_ESCAPE_OR_ENTITY if entities_too else _STARTS_ESCAPE
    scanned = Found()
    index = 0
    while True:
        nearest = starts.search(markdown, index)
        if nearest is None:
            return found
        index = nearest.start()
        end = blocked.end(index)
        if end is not None:
            index = end + 1
        elif markdown.startswith("![", index):
            close = label_end(markdown, index + 1, scanned)
            index = index + 2 if close is None else close
        elif markdown.startswith("](", index):
            close = destination_end(markdown, index + 1, scanned)
            if close is not None and (_wordless(markdown, index)
                                      or not markdown[index + 2:close].strip(" \t\n<>")):
                found = found._replace(wordless=True)
            index = index + 2 if close is None else close + 1
        elif markdown[index] == "&":
            entity = _ENTITY.match(markdown, index)
            if entity is not None:
                found.entities.append(entity.span())
            index = index + 1 if entity is None else entity.end()
        else:
            skipped = skip_inline(markdown, index, scanned)
            if skipped == index + 2 and markdown[index] == "\\":
                found.escapes.append(index)
            elif skipped is not None and _AUTOLINK.match(markdown, index):
                found.autolinks.append(index)
            index = index + 1 if skipped is None else skipped

