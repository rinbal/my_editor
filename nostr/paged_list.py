# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A list too long for one event, in pages (STANDUP's editorial contract
13, which both apps follow). Pure: no relays, no signer, no clock;
``nostr/encrypted_lists.py`` reads and writes the pages.

**Size.** A NIP-44 message carries at most 65,535 bytes, a signer app
at most 40,960 (``signer_limits.SIGNER_NIP44_MAX_PLAINTEXT``), and the
signer's answer to ``nip44_decrypt`` carries the plaintext as a JSON string, in
which every quote and backslash takes two bytes. A page is therefore at
most 32,000 bytes of plaintext: twice that, with the answer around it,
still fits one message (``nak bunker`` 0.20.7 returned a 32,000-byte
page of nothing but quotes, and not one of 32,800). A list has at most
64 pages; a longer one is refused when written and unreadable when read.

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

from .crypto import payload_length

__all__ = [
    "MAX_PAGES", "PAGE_PAYLOAD_MAX", "PAGE_PLAINTEXT_MAX", "PAGED_VERSION", "Page", "PageError",
    "Paging", "digest", "join", "page_d_tag", "parse_page", "serialize", "split", "stale_pages",
]

PAGE_PLAINTEXT_MAX = 32_000
# The longest NIP-44 payload a page can be (43,780 characters): one that
# is longer holds more than a page and is never sent to a signer app to
# decrypt (its answer might not fit, and the read would wait it out).
PAGE_PAYLOAD_MAX = payload_length(PAGE_PLAINTEXT_MAX)
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


def page_d_tag(d_tag: str, index: int) -> str:
    """The ``d`` tag of a page: the list's own for page 0, ``<d>:<n>`` after."""
    return d_tag if index == 0 else f"{d_tag}:{index}"


def serialize(payload: Any) -> str:
    """Compact, ASCII only (non-ASCII as \\u escapes): the feed list has
    always been written so, and every page is measured as written."""
    return json.dumps(payload, separators=(",", ":"))


def _size(value: Any) -> int:
    return len(serialize(value).encode("utf-8"))


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
        if len(text.encode("utf-8")) > paging.max_bytes:
            raise ValueError(f"page {index} came to {len(text)} bytes")
    return texts


def _fill(items: list, as_map: bool, header: Mapping[str, Any], parts: int,
          paging: Paging) -> List[list]:
    """The entries in pages, in order, page 0 measured with ``parts``
    hashes. ``page`` and ``pages`` are measured at their widest (64)."""

    def shell(first: bool) -> int:
        page: Dict[str, Any] = {"v": PAGED_VERSION, "page": MAX_PAGES, "pages": MAX_PAGES}
        if first:
            page.update(header)
            page["parts"] = ["0" * 64] * parts
        page[paging.field] = {} if as_map else []
        return _size(page)

    def cost(item: Any) -> int:
        if as_map:
            return _size(str(item[0])) + 1 + _size(item[1])     # "key":value
        return _size(item)

    chunks: List[list] = [[]]
    used = shell(True)
    for item in items:
        grow = cost(item) + (1 if chunks[-1] else 0)       # the comma before it
        if used + grow > paging.max_bytes and (chunks[-1] or len(chunks) == 1):
            chunks.append([])
            used = shell(False)
            grow = cost(item)
        if used + grow > paging.max_bytes:
            raise ValueError(f"an entry of {cost(item)} bytes does not fit a "
                             f"{paging.max_bytes}-byte page")
        chunks[-1].append(item)
        used += grow
    return chunks
