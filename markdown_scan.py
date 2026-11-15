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
from typing import Dict, List, Optional, Tuple

FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
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


# A quote's mark at a line's start, and a list item's start after the
# quote marks: its indent, its marker and the words after it.
_QUOTE_MARK = re.compile(r" {0,3}> ?")
_ITEM = re.compile(r"( *)([-*+]|\d{1,9}[.)])(?=[ \t]|$)[ \t]*(.*)")
_TASK_BOX = re.compile(r"\[[ xX]\][ \t]*")
# A line that may be such an item with nothing in it (empty_items).
_MAYBE_EMPTY_ITEM = re.compile(r"^[ >]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+\[[ xX]\])?[ \t]*$", re.M)


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
    """Where each list item with nothing in it ("-", "2.", "- [ ]") ends
    its marker, outside code, when words written there leave the lists
    as they are (_empty_item_alone); and whether such an item stands
    where words would change them.

    Qt's reader leaves an item with nothing in it out (md4c gives it no
    text), and keeps one with a word in it."""
    if _MAYBE_EMPTY_ITEM.search(markdown) is None:
        return [], False
    lines = markdown.split("\n")
    code = code_lines(lines)
    places: List[int] = []
    unsafe = False
    start = 0
    for number, line in enumerate(lines):
        line_start, start = start, start + len(line) + 1
        if code[number]:
            continue
        depth = _quote_depth(line)
        rest = _inside_quotes(line, depth)
        item = _ITEM.fullmatch(rest)
        if item is None:
            continue
        words = item.group(3)
        box = _TASK_BOX.fullmatch(words)
        if (words.strip() and box is None) \
                or not _starts_an_item(lines, code, number, depth, len(item.group(1))):
            continue
        before = line_start + len(line) - len(rest)
        if box is None:
            alone = _empty_item_alone(lines, code, number, depth,
                                      len(item.group(1)) + len(item.group(2)) + 1)
            if alone:
                places.append(before + item.end(2))
            elif alone is not None:
                unsafe = True
        elif _empty_item_alone(lines, code, number, depth, item.start(3)):
            # (A task's box is a paragraph already: a line that would join
            # the words joins the box, and the task is not empty.)
            places.append(before + item.start(3) + len(words.rstrip()))
    return places, unsafe


def _starts_an_item(lines: List[str], code: List[bool], number: int, depth: int,
                    indent: int) -> bool:
    """Whether the item on line ``number`` with nothing in it (in ``depth``
    quotes, ``indent`` spaces in) is one: no paragraph's words stand right
    above it in its place, which an empty item cannot follow ("Text\\n-"
    is a heading, "- a\\n    -" one in the item). Above it: nothing, a
    blank line, a code fence, or an item it is not inside of."""
    if number == 0 or code[number - 1] or not lines[number - 1].strip():
        return True
    above = _inside_quotes(lines[number - 1], depth)
    if above is not None and not above.strip():
        return True
    item = None if above is None else _ITEM.fullmatch(above)
    if item is None:
        return False                    # words of a paragraph, or what this does not tell
    return not item.group(3).strip() or indent < item.start(3)


def _empty_item_alone(lines: List[str], code: List[bool], number: int, depth: int,
                      column: int) -> Optional[bool]:
    """Whether words written in the empty item on line ``number`` (in
    ``depth`` quotes, its words' column ``column``) stay alone: True when
    the text ends after it, a blank line ends them, or an item follows
    (one inside it numbered 1 or bulleted, as an item must be to follow a
    paragraph); False when a line would join them (one left of the
    column, as a paragraph's lazy line, or a second paragraph, which an
    item that starts empty cannot have); None when the next line holds
    the item's words (it is not empty)."""
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
    marker = item.group(2)
    return indent < column + 4 and (marker[-1] not in ".)" or int(marker[:-1]) == 1)


def code_span_end(text: str, start: int, found: Optional[Found] = None) -> int:
    """Where the code span opening at ``start`` (a run of backticks) ends,
    or where the run ends when nothing closes it (then it is text);
    ``found`` keeps where a paragraph's end and backticks were found."""
    run = 0
    while start + run < len(text) and text[start + run] == "`":
        run += 1
    at = start + run
    # A code span ends with its paragraph.
    limit = _next_of(text, "\n\n", at, found)
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
                    label = label_end(markdown, end + 1, scanned)
                    close = label if label is not None and label > end + 2 else None
                if close is not None:
                    found.append((index + 2, end, close + 1))
                    index = close + 1
                    continue
            index += 2
            continue
        index += 1
    return found


def bracketed(markdown: str) -> Tuple[List[int], List[Tuple[int, int]]]:
    """What of ``markdown`` stands inside brackets, outside code, where the
    words of a link may hold it: each picture, by where it starts (its
    "!"; "[![a](p.png)](u)", "[see ![a](p.png)](u)"), and each tag on
    one line ("[x <y>](u)"), by where it starts and ends. A tag in a
    picture's description is the description's."""
    if _OWN_BRACKET.search(markdown) is None:
        return [], []                   # no bracket but the pictures' own
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
            [(start, end) for start, end in tags if start in held])


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
    blocked = _Blocked(_code_ranges(markdown)
                       + [definition.span(1) for definition in _DEFINITION.finditer(markdown)])
    found: List[int] = []
    scanned = Found()
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
            close = label_end(markdown, index + 1, scanned)
            index = index + 2 if close is None else close
        elif markdown.startswith("](", index):
            close = destination_end(markdown, index + 1, scanned)
            index = index + 2 if close is None else close + 1
        else:
            skipped = skip_inline(markdown, index, scanned)
            if skipped == index + 2 and markdown[index] == "\\":
                found.append(index)
            index = index + 1 if skipped is None else skipped
