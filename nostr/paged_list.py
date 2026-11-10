# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A list too long for one event, in pages (STANDUP's editorial contract
13, which both apps follow). Pure: no relays, no signer, no clock;
``nostr/encrypted_lists.py`` reads and writes the pages.

**Size.** Every device reads every page back through a signer app,
over whatever connection it has, so every page is written small enough
for a connection whose relays cap what they carry at 65,536 characters,
whatever the writer's own connection is (contract 12.5 "Pages fit every
connection", 13; STANDUP 007ded2): at most 28,672 bytes of plaintext,
whose NIP-44 payload pads to at most 28,672 and whose ``nip44_decrypt``
request stays within the 40,960 bytes such a connection carries, and the
signer's answer handing it back, measured with the worst-case escaper
(``nostr/answer_size.py``: a quote two bytes, a ``<`` six), at most
40,896: a page is split by both. A list has at most 64 pages; a longer
one is refused when written and unreadable when read. Over a connection
whose relays cap what they carry lower still, pages are split smaller,
and by the ``nip44_encrypt`` request that carries them too
(``signer_limits.page_bounds``).

**Shape.** Page 0 has ``d`` = the list's own, page n > 0 ``<d>:<n>``.
Every page's plaintext is ``{"v":1,"page":i,"pages":n,<field>:...}``,
the field being a list or a map split across the pages. Page 0 also
carries the list's other keys and ``"parts"``: the SHA-256 of the exact
plaintext of pages 1 to n-1, in order.

**Generations.** One write is one generation, and ``parts`` binds it: a
later page whose hash is not in ``parts`` is of another generation (a
write that was interrupted, or another device's), one that is not there
is missing. Either makes a read incomplete (:func:`stale_pages`), never
shorter: every page that is the account's own still counts, and the
merge of every such list is a union, so a page of another generation can
add an entry or an older copy of one, never remove one. A list written
before ``parts`` existed has none: it is read as one generation of
unknown completeness, and the next write gives it ``parts``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .answer_size import (
    decrypt_answer_size, encrypt_request_size, escaped_text_size, json_text_size,
)
from .crypto import NIP44_MAX_PLAINTEXT_LEN, payload_length

__all__ = [
    "MAX_PAGES", "PAGE_ANSWER_MAX", "PAGE_PAYLOAD_MAX", "PAGE_PLAINTEXT_MAX", "PAGED_VERSION",
    "Page", "PageError", "Paging", "READ_PAGE_PAYLOAD_MAX", "digest", "join", "page_d_tag",
    "page_fits", "parse_page", "serialize", "split", "stale_pages",
]

# What one page written holds at most, and what the signer app's answer
# handing it back takes at most, escaped at its largest: the contract's
# numbers (12.5, 13), which are what a connection capped at 65,536
# characters carries (tests/test_relay_caps.py measures them so). One
# byte more would pad to 32,768 and need 43,780 characters.
PAGE_PLAINTEXT_MAX = 28_672
PAGE_ANSWER_MAX = 40_896
# A written page's NIP-44 payload at its longest: 38,320 characters (a
# keyring page's too, 12.3).
PAGE_PAYLOAD_MAX = payload_length(PAGE_PLAINTEXT_MAX)
# The longest payload read as a page: pages were written up to 32,000
# bytes before 007ded2, and such a page is still read wherever the
# connection carries it. A longer one holds more than a page ever did and
# is never sent to a signer app to decrypt (its answer might not fit, and
# the read would wait it out).
READ_PAGE_PAYLOAD_MAX = payload_length(32_000)
MAX_PAGES = 64
# The paged format's version, in every page.
PAGED_VERSION = 1

_FRAME = frozenset({"v", "page", "pages", "parts"})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class PageError(ValueError):
    """A page that is not one of this list's (:func:`parse_page`)."""


@dataclass(frozen=True)
class Paging:
    """How a long list is split: ``field`` is the list or map in the
    plaintext that is spread over pages, at most ``max_bytes`` each.

    ``combine(earlier, later)`` is the value of a map key found on two
    pages (two generations); without it the later page's value counts. A
    list's entries are all kept, in page order, for the caller's union
    merge to sort out."""

    field: str
    max_bytes: int = PAGE_PLAINTEXT_MAX
    combine: Optional[Callable[[Any, Any], Any]] = None
    # What writing and reading a page back through a signer app may take:
    # the answer to nip44_decrypt (escaped at its largest) and the
    # nip44_encrypt request (as sent). The request binds only over the
    # writer's own capped relays; both are lower over relays capped
    # below 65,536 characters.
    answer_max: int = PAGE_ANSWER_MAX
    request_max: int = NIP44_MAX_PLAINTEXT_LEN


@dataclass(frozen=True)
class Page:
    """One page, parsed. ``header`` (the list's other keys) and ``parts``
    are page 0's; ``parts`` is None for a list written before them."""

    index: int
    pages: int
    body: Union[list, dict]
    header: Mapping[str, Any]
    parts: Optional[Tuple[str, ...]]
    plaintext: str = field(repr=False)

    @property
    def digest(self) -> str:
        return digest(self.plaintext)


def digest(plaintext: str) -> str:
    """A page's hash as ``parts`` names it: over its exact plaintext bytes."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def page_fits(plaintext: str) -> bool:
    """Whether ``plaintext`` may be written as a page: at most
    :data:`PAGE_PLAINTEXT_MAX` bytes, and the signer app's answer handing
    it back at most :data:`PAGE_ANSWER_MAX` (contract 12.5, 13). What a
    write asks of every page before it encrypts one, whatever split it."""
    return (len(plaintext.encode("utf-8")) <= PAGE_PLAINTEXT_MAX
            and decrypt_answer_size(plaintext) <= PAGE_ANSWER_MAX)


def page_d_tag(d_tag: str, index: int) -> str:
    """The ``d`` tag of a page: the list's own for page 0, ``<d>:<n>`` after."""
    return d_tag if index == 0 else f"{d_tag}:{index}"


def serialize(payload: Any) -> str:
    """Compact, ASCII only (non-ASCII as \\u escapes): the feed list has
    always been written so, and every page is measured as written."""
    return json.dumps(payload, separators=(",", ":"))


def _size(value: Any) -> int:
    return len(serialize(value).encode("utf-8"))


def _answer(value: Any) -> int:
    """What ``value``'s text adds to a signer app's answer handing the
    page back, escaped at its largest (nostr/answer_size.py)."""
    return escaped_text_size(serialize(value))


def _request(value: Any) -> int:
    """What ``value``'s text adds to the nip44_encrypt request carrying
    the page, as MyEditor sends it."""
    return json_text_size(serialize(value))


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #

def parse_page(text: str, *, index: int, paging: Paging,
               first: Optional[Page] = None) -> Page:
    """Page ``index`` of a list, from its plaintext. ``first`` is page 0,
    given for every later page: its field must be of the same kind.

    Raises :class:`PageError` for anything that is not such a page: not a
    JSON object, ``v`` other than 1, a ``page`` other than ``index``,
    ``pages`` outside 1 to 64 or not above ``index``, ``parts`` on page 0
    that do not name every later page, or a field that is missing or of
    another kind. Page 0 failing makes the list unreadable; a later page
    failing counts as missing (contract 13)."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise PageError("not JSON") from None
    if not isinstance(payload, dict):
        raise PageError("not a JSON object")
    version = payload.get("v")
    if not _is_count(version) or version != PAGED_VERSION:
        raise PageError("another format version")
    if not _is_count(payload.get("page")) or payload["page"] != index:
        raise PageError("another page number than its d tag")
    pages = payload.get("pages")
    if not _is_count(pages) or not index < pages <= MAX_PAGES:
        raise PageError(f"a page count outside 1 to {MAX_PAGES}")
    body = payload.get(paging.field)
    expected = (list, dict) if first is None else type(first.body)
    if not isinstance(body, expected):
        raise PageError(f"no {paging.field!r} of the list's kind")
    parts: Optional[Tuple[str, ...]] = None
    header: Dict[str, Any] = {}
    if index == 0:
        header = {k: v for k, v in payload.items() if k not in _FRAME and k != paging.field}
        if "parts" in payload:
            listed = payload["parts"]
            if (not isinstance(listed, list) or len(listed) != pages - 1
                    or not all(isinstance(p, str) and _HEX64.match(p) for p in listed)):
                raise PageError("parts that do not name every later page")
            parts = tuple(listed)
    return Page(index=index, pages=pages, body=body, header=header, parts=parts,
                plaintext=text)


def stale_pages(first: Page, later: Mapping[int, Page]) -> Tuple[int, ...]:
    """The pages 1 to ``pages``-1 that are missing from ``later`` or not
    of page 0's generation: their hash is not the one ``parts`` names, or,
    in a list written before ``parts``, they count another number of
    pages. None of them: the read is complete."""
    stale = []
    for index in range(1, first.pages):
        page = later.get(index)
        if page is None:
            stale.append(index)
        elif first.parts is not None:
            if page.digest != first.parts[index - 1]:
                stale.append(index)
        elif page.pages != first.pages:
            stale.append(index)
    return tuple(stale)


def join(first: Page, later: Sequence[Page], paging: Paging) -> Dict[str, Any]:
    """One plaintext object from page 0 and the later pages that count:
    page 0's other keys, and the field put back together in page order."""
    joined: Dict[str, Any] = dict(first.header)
    pages = [first] + sorted(later, key=lambda page: page.index)
    if isinstance(first.body, dict):
        together: Dict[str, Any] = {}
        for page in pages:
            for key, value in page.body.items():
                if paging.combine is not None and key in together:
                    value = paging.combine(together[key], value)
                together[key] = value
        joined[paging.field] = together
    else:
        joined[paging.field] = [item for page in pages for item in page.body]
    return joined


# --------------------------------------------------------------------------- #
# Writing                                                                      #
# --------------------------------------------------------------------------- #

def split(payload: Mapping[str, Any], paging: Paging) -> List[str]:
    """The plaintexts of one complete generation of ``payload``, page 0
    first, whose ``paging.field`` is a list or a map. Every other key
    goes on page 0, with the ``parts`` of the later pages. Raises
    ValueError for an entry too large for any page, or a list that needs
    more than 64 pages."""
    entries = payload.get(paging.field)
    if not isinstance(entries, (list, dict)):
        raise ValueError(f"{paging.field!r} must be a list or a map")
    header = {k: v for k, v in payload.items() if k not in _FRAME and k != paging.field}
    as_map = isinstance(entries, dict)
    items = list(entries.items()) if as_map else list(entries)
    # Page 0 holds a hash per later page, so how much of it is left for
    # entries depends on how many pages there are: measure again with the
    # count found until it holds (the count only grows, at most to 64).
    count = 1
    while True:
        chunks = _fill(items, as_map, header, count - 1, paging)
        if len(chunks) > MAX_PAGES:
            raise ValueError(f"the list needs more than {MAX_PAGES} pages")
        if len(chunks) <= count:
            break
        count = len(chunks)

    def body(chunk: list) -> Union[list, dict]:
        return dict(chunk) if as_map else list(chunk)

    total = len(chunks)
    later = [serialize({"v": PAGED_VERSION, "page": index, "pages": total,
                        paging.field: body(chunk)})
             for index, chunk in enumerate(chunks[1:], start=1)]
    first: Dict[str, Any] = {"v": PAGED_VERSION, "page": 0, "pages": total}
    first.update(header)
    first["parts"] = [digest(text) for text in later]
    first[paging.field] = body(chunks[0])
    texts = [serialize(first)] + later
    for index, text in enumerate(texts):
        size = len(text.encode("utf-8"))
        if size > paging.max_bytes:
            raise ValueError(f"page {index} came to {size} bytes")
    return texts


def _fill(items: list, as_map: bool, header: Mapping[str, Any], parts: int,
          paging: Paging) -> List[list]:
    """The entries in pages, in order, page 0 measured with ``parts``
    hashes. ``page`` and ``pages`` are measured at their widest (64).
    A page holds at most ``paging.max_bytes`` of plaintext, the signer
    app's answer handing it back at most ``paging.answer_max`` bytes
    (40,896; contract 12.5, 13: a page of ``<`` is six times its size
    there), and the request encrypting it at most ``paging.request_max``
    (a quote is two bytes there; it binds over capped relays only)."""
    Sizes = Tuple[int, int, int]                # plaintext, answer, request
    bounds = (paging.max_bytes, paging.answer_max, paging.request_max)

    def shell(first: bool) -> Sizes:
        page: Dict[str, Any] = {"v": PAGED_VERSION, "page": MAX_PAGES, "pages": MAX_PAGES}
        if first:
            page.update(header)
            page["parts"] = ["0" * 64] * parts
        page[paging.field] = {} if as_map else []
        text = serialize(page)
        return _size(page), decrypt_answer_size(text), encrypt_request_size(text)

    def cost(item: Any) -> Sizes:
        if as_map:
            key, value = serialize(str(item[0])), serialize(item[1])
            return (len(key.encode("utf-8")) + 1 + len(value.encode("utf-8")),   # "key":value
                    escaped_text_size(key) + 1 + escaped_text_size(value),
                    json_text_size(key) + 1 + json_text_size(value))
        return _size(item), _answer(item), _request(item)

    def plus(used: Sizes, grow: Sizes) -> Sizes:
        return used[0] + grow[0], used[1] + grow[1], used[2] + grow[2]

    def fits(sizes: Sizes) -> bool:
        return all(size <= bound for size, bound in zip(sizes, bounds))

    chunks: List[list] = [[]]
    used = shell(True)
    for item in items:
        alone = cost(item)
        grow = plus(alone, (1, 1, 1)) if chunks[-1] else alone          # the comma
        if not fits(plus(used, grow)) and (chunks[-1] or len(chunks) == 1):
            chunks.append([])
            used = shell(False)
            grow = alone
        total = plus(used, grow)
        if total[0] > paging.max_bytes:
            raise ValueError(f"an entry of {alone[0]} bytes does not fit a "
                             f"{paging.max_bytes}-byte page")
        if total[1] > paging.answer_max:
            # It fits the page, but not the signer app's answer handing
            # the page back (escaped, it grows).
            raise ValueError(f"an entry of {alone[0]} bytes takes {alone[1]} bytes in the "
                             f"signer app's answer, more than a page's {paging.answer_max}")
        if total[2] > paging.request_max:
            raise ValueError(f"an entry of {alone[0]} bytes takes {alone[2]} bytes in the "
                             f"request to encrypt it, more than this connection carries "
                             f"({paging.request_max})")
        chunks[-1].append(item)
        used = total
    return chunks
