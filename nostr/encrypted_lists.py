# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A person's private list, kept on their relays as kind 30078 events
(NIP-78) encrypted to themselves (NIP-44).

What the list holds is the caller's: a ``decode`` and an ``encode`` turn
the plaintext JSON into the caller's value and back, and a ``merge``
brings two devices' or two apps' edits together. The rest is the same
for every such list, and lives here once:

- Only the account's own list counts: each event read is checked (kind,
  ``d`` tag, author, signature) before the newest is chosen, so a relay
  can neither plant a list nor hide the real one behind a newer forged
  one.
- No answer is never "no list": a read counts only when one of the
  account's own relays answered. Then nothing is written.
- A list that cannot be read (older encryption, a signer that does not
  answer, a format from a newer app) is never written over: that would
  delete what is in it.
- Only a newer list counts: a copy no newer than the last one seen (a
  slow or a fallback relay's) changes nothing.
- Writing reads first and merges, so neither side's edits are lost, and
  each new version is dated after the one it replaces, even when this
  computer's clock runs behind.

A long list is split into pages (``Paging``). A NIP-44 message carries
at most 65,535 bytes, a signer app at most 40,960
(``bunker.SIGNER_NIP44_MAX_PLAINTEXT``), and the signer's answer to
``nip44_decrypt`` carries the plaintext as a JSON string, in which every
quote and backslash takes two bytes. A page is therefore at most 32,000
bytes of plaintext: twice that, with the answer around it, still fits
one message (``nak bunker`` 0.20.7 returned a 32,000-byte page of
nothing but quotes, and not one of 32,800). Page 0 has ``d`` = the
list's own; page n > 0 has ``d`` = ``<d>:<n>``. Every page's plaintext
is ``{"v":1,"page":i,"pages":n,<field>:...}``, the field being a list
or a map split across the pages (the contract both apps follow,
STANDUP's docs/editorial-contract.md). A list that is not paged keeps
the format it always had: one event, the plaintext exactly as
``encode`` gives it.

A page that page 0 names but no relay has (lost, or not arrived yet) holds
entries nobody here knows. It never stops the list: the other pages are
read (the read says which are missing), and writing goes on for what is
known, leaving that page's place as it is, never written over, so what
is on it is still read once it turns up.

Every boundary (relay queries, publishing, signer, clock) is passed in,
so a list is testable without relays, a signer or wall-clock time.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QObject

from . import CLIENT_NAME
from .bunker import is_signer_silent
from .drafts import supersedes
from .events import build_event, verify_event
from .outbox import ask_private_relays
from .outbox.policy import normalize_relay_url, replacement_created_at
from .queries import fetch_events

__all__ = [
    "APP_DATA_KIND", "PAGE_PLAINTEXT_MAX", "NO_ANSWER", "UNREADABLE",
    "UP_TO_DATE", "SENT", "NOT_ACCEPTED", "FAILED",
    "EncryptedList", "ListQuery", "ListWords", "Paging", "PublishResult",
    "event_mark", "is_list_of", "layout_pages", "page_d_tag", "publish_once", "split_pages",
]

_log = logging.getLogger(__name__)

APP_DATA_KIND = 30078           # NIP-78 application data

# Most plaintext in one page (see the module's docstring for why).
PAGE_PLAINTEXT_MAX = 32_000
# The paged format's version, in every page. A page with a higher one is
# a newer app's: unreadable here, so never written over.
PAGED_VERSION = 1
# A sane end for ``pages``: 100 pages are 3.2 MB of plaintext, and a
# number larger than that is a broken list, not a long one.
MAX_PAGES = 100

# What a read can turn out to be, besides the value or None (no list).
UNREADABLE = "unreadable"
NO_ANSWER = "no answer"

# How a publish ended (PublishResult.status).
UP_TO_DATE = "up to date"       # what is here is on the relays already
SENT = "sent"                   # every page accepted by at least one relay
NOT_ACCEPTED = "not accepted"   # no relay took a page
FAILED = "failed"               # the signer refused or could not be reached


@dataclass(frozen=True)
class ListWords:
    """What the person is told when the list cannot be read, in the
    caller's words (they name the list)."""

    unreadable: str
    signer_silent: str
    old_encryption: str
    # A page of the list is missing (shown, when given, while the rest is
    # read and written as usual).
    missing_page: str = ""


@dataclass(frozen=True)
class Paging:
    """How a long list is split: ``field`` is the list or map in the
    plaintext that is spread over pages, at most ``max_bytes`` each.

    ``combine(earlier, later)`` is the value of a map key found on two
    pages (a page that was missing and turned up again holds an older
    copy). Without it the later page's value counts. A list's entries
    are all kept, in page order, for ``decode`` to sort out."""

    field: str
    max_bytes: int = PAGE_PLAINTEXT_MAX
    combine: Optional[Callable[[Any, Any], Any]] = None


@dataclass
class PublishResult:
    status: str
    remote: Any = None          # UP_TO_DATE: the list read from the relays
    value: Any = None           # SENT: what was written
    signed: Optional[dict] = None   # SENT: the event of page 0
    reason: str = ""            # FAILED: why
    missing: Tuple[int, ...] = ()   # pages no relay had, left as they are


@dataclass
class _Page:
    event: Optional[dict]       # newest valid event for the page's d tag
    plaintext: Optional[str] = None


class ListQuery(QObject):
    """Asks relays for events and says which relays answered."""

    def __init__(self, pool, parent: Optional[QObject] = None, *,
                 timeout_ms: int = 8_000) -> None:
        super().__init__(parent)
        self._pool = pool
        self._timeout_ms = timeout_ms

    def events(self, relays, filters, on_done) -> None:
        """``on_done(events, answered)``: every validly signed event, and
        the relays that answered (sent all their stored events); one that
        refused or could not be reached is not among them."""
        try:
            fetch_events(self._pool, list(relays), list(filters),
                         lambda fetched: on_done(list(fetched.events), set(fetched.answered)),
                         timeout_ms=self._timeout_ms, parent=self)
        except Exception:  # noqa: BLE001, settle the callback contract
            on_done([], set())


def publish_once(relay_pool, relays, signed, *, on_done) -> None:
    """Publish ``signed`` and answer once: as soon as one relay accepted
    it (the list is safe then; a relay that never answers must not hold
    up quitting for the publish timeout), or when all have answered and
    none did."""
    relays = list(relays)
    try:
        job = relay_pool.publish(relays, signed)
    except Exception as exc:  # noqa: BLE001, publish must never raise into UI
        _log.warning("list publish failed: %s", exc)
        on_done(0, len(relays))
        return
    answered = []

    def accepted(_url: str) -> None:
        if not answered:
            answered.append(True)
            on_done(1, len(relays))

    def all_done(results) -> None:
        if not answered:
            answered.append(True)
            on_done(sum(1 for _, ok, _ in results if ok), len(results))

    job.first_accept.connect(accepted)
    job.all_done.connect(all_done)


def event_mark(event: Optional[dict]) -> Optional[Tuple[int, str]]:
    """Which event a list came from: its time and id."""
    if not isinstance(event, dict):
        return None
    try:
        return int(event.get("created_at", 0)), str(event.get("id") or "")
    except (TypeError, ValueError):
        return None


def is_list_of(event, pubkey: str, d_tag: str, kind: int = APP_DATA_KIND) -> bool:
    """Whether ``event`` is ``pubkey``'s own list under ``d_tag``: the
    kind, the ``d`` tag, the author and a valid signature."""
    if not isinstance(event, dict) or event.get("kind") != kind:
        return False
    if str(event.get("pubkey", "")).lower() != pubkey.lower():
        return False
    tag = next((t[1] for t in event.get("tags", [])
                if isinstance(t, list) and len(t) >= 2 and t[0] == "d"), None)
    if tag != d_tag:
        return False
    try:
        int(event.get("created_at"))
    except (TypeError, ValueError):
        return False
    return verify_event(event)


def page_d_tag(d_tag: str, page: int) -> str:
    """The ``d`` tag of a page: the list's own for page 0, ``<d>:<n>`` after."""
    return d_tag if page == 0 else f"{d_tag}:{page}"


def _missing(pages) -> List[int]:
    """The numbers of the pages read that no relay had (page 0 is the
    list itself: without it there is no list)."""
    return [n for n, page in enumerate(pages) if n > 0 and page.event is None]


def _serialize(payload) -> str:
    # Compact, ASCII only (non-ASCII as \u escapes): the feed list has
    # always been written so, and every page is measured as written.
    return json.dumps(payload, separators=(",", ":"))


def _size(value) -> int:
    return len(_serialize(value).encode("utf-8"))


def split_pages(payload: dict, paging: Paging) -> List[str]:
    """The plaintext of each page of ``payload``, whose ``paging.field`` is
    a list or a map. Every other key goes on page 0. Raises ValueError for
    an entry too large for any page."""
    return list(layout_pages(payload, paging).values())


def layout_pages(payload: dict, paging: Paging, keep: Sequence[int] = ()) -> Dict[int, str]:
    """``split_pages``, leaving the pages in ``keep`` (never 0) out: the
    plaintext of every other page by its number, in order. The entries
    fill the other places from page 0 on; a place below the highest kept
    one that no entry needs gets an empty page, so no older page there
    is read as part of the list, and ``pages`` counts every place, the
    kept ones included, so readers still look for them."""
    entries = payload.get(paging.field)
    if not isinstance(entries, (list, dict)):
        raise ValueError(f"{paging.field!r} must be a list or a map")
    kept = sorted({int(n) for n in keep if int(n) > 0})
    header = {k: v for k, v in payload.items() if k not in (paging.field, "v", "page", "pages")}
    as_map = isinstance(entries, dict)
    items = list(entries.items()) if as_map else list(entries)
    # ``page`` and ``pages`` are written as numbers no wider than this one
    # (there are never more places than entries and kept pages, nor than
    # the highest kept page), so no page written is longer than it was
    # measured with it.
    widest = max(1, len(items) + len(kept), (kept[-1] + 1) if kept else 0)

    def shell(first: bool) -> Dict[str, Any]:
        page = {"v": PAGED_VERSION, "page": widest, "pages": widest}
        if first:
            page.update(header)
        page[paging.field] = {} if as_map else []
        return page

    def cost(item) -> int:
        if as_map:
            return _size(str(item[0])) + 1 + _size(item[1])   # "key":value
        return _size(item)

    chunks: List[list] = [[]]
    used = _size(shell(True))
    for item in items:
        grow = cost(item) + (1 if chunks[-1] else 0)   # the comma before it
        if used + grow > paging.max_bytes and chunks[-1]:
            chunks.append([])
            used = _size(shell(False))
            grow = cost(item)
        if used + grow > paging.max_bytes:
            raise ValueError(f"an entry of {cost(item)} bytes does not fit a "
                             f"{paging.max_bytes}-byte page")
        chunks[-1].append(item)
        used += grow

    places: List[int] = []
    number = 0
    while len(places) < len(chunks) or (kept and number < kept[-1]):
        if number not in kept:
            places.append(number)
        number += 1
    chunks += [[] for _ in range(len(places) - len(chunks))]
    total = max(places[-1], kept[-1] if kept else 0) + 1
    pages: Dict[int, str] = {}
    for index, chunk in zip(places, chunks):
        page = {"v": PAGED_VERSION, "page": index, "pages": total}
        if index == 0:
            page.update(header)
        page[paging.field] = dict(chunk) if as_map else chunk
        text = _serialize(page)
        if len(text.encode("utf-8")) > paging.max_bytes:  # measured too short
            raise ValueError(f"page {index} came to {len(text)} bytes")
        pages[index] = text
    return pages


class EncryptedList(QObject):
    """One account's encrypted list under one ``d`` tag: read, merged,
    written. Holds no state of its own between calls; the caller keeps
    what it last saw (the base), what it has (local), and where it came
    from, and hands them in.

    ``decode(payload_dict)`` gives the value of a plaintext, ``encode(value,
    now)`` the plaintext JSON object of a value, ``merge(base, local,
    remote)`` both sides' edits since ``base`` in one value (``remote`` may
    be None: no list yet), and ``same(a, b)`` whether two values say the
    same (default ``==``).
    """

    def __init__(
        self,
        *,
        d_tag: str,
        decode: Callable[[dict], Any],
        encode: Callable[[Any, int], dict],
        merge: Callable[[Any, Any, Any], Any],
        words: ListWords,
        session_pool,
        relay_directory,
        query,
        publisher: Callable[..., None],
        clock: Callable[[], int],
        entitled_relays=None,
        same: Optional[Callable[[Any, Any], bool]] = None,
        paging: Optional[Paging] = None,
        kind: int = APP_DATA_KIND,
        on_status: Optional[Callable[[str], None]] = None,
        name: str = "",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.d_tag = d_tag
        self._decode = decode
        self._encode = encode
        self._merge = merge
        self._same = same or (lambda a, b: a == b)
        self._words = words
        self._session_pool = session_pool
        self._relay_directory = relay_directory
        self._query = query
        self._publisher = publisher
        self._clock = clock
        self._entitled_relays = entitled_relays
        self._paging = paging
        self._kind = kind
        self._on_status = on_status or (lambda _text: None)
        self._name = name or d_tag

    # -- relays ------------------------------------------------------------

    def relays(self, profile, on_done: Callable[..., None], *, reading: bool = False) -> None:
        """The account's private relays: where the list is written
        (``on_done(relays)``), or with ``reading`` where it is read, which
        also asks the relays a list written before the account's own was
        known went: ``on_done((read, own))``, ``own`` being the relays it
        is written to, the only ones whose answer counts."""
        if not reading:
            ask_private_relays(self._relay_directory, profile, on_done,
                               entitled=self._entitled_relays, reading=False)
            return

        def _own(own: List[str]) -> None:
            ask_private_relays(self._relay_directory, profile,
                               lambda read: on_done((read, own)),
                               entitled=self._entitled_relays, reading=True)

        ask_private_relays(self._relay_directory, profile, _own,
                           entitled=self._entitled_relays, reading=False)

    # -- reading -------------------------------------------------------------

    def read(self, profile, relays, on_done,
             on_missing: Optional[Callable[[List[int]], None]] = None) -> None:
        """The newest list: ``on_done(result, event)`` with the decoded
        value, None when there is none, NO_ANSWER when none of the
        account's own relays answered (never taken for "none"), or
        UNREADABLE (with the reason already reported). ``event`` is the
        list's event (page 0's). ``relays`` is ``(read, own)``.

        A page no relay has leaves the value without its entries:
        ``on_missing(numbers)`` hears which pages, before ``on_done``."""
        def done(result, pages) -> None:
            missing = _missing(pages)
            if missing and on_missing is not None and result not in (UNREADABLE, NO_ANSWER):
                on_missing(missing)
            on_done(result, pages[0].event if pages else None)

        self._read_pages(profile, relays, done)

    def _read_pages(self, profile, relays, on_done) -> None:
        """``read``, answering ``on_done(result, pages)`` with every page
        read (its event and plaintext), for writing only what changed."""
        read, own = relays
        if self._query is None:
            on_done(None, [])
            return
        counted = {normalize_relay_url(u) or u for u in own}

        def answered_by_own(answered) -> bool:
            answered = {normalize_relay_url(u) or u for u in answered}
            return bool(answered) and not (counted and not (answered & counted))

        def newest_of(events, d_tag: str) -> Optional[dict]:
            newest = None
            for event in events:
                if not is_list_of(event, profile.user_pubkey, d_tag, self._kind):
                    continue
                if newest is None or supersedes(int(event["created_at"]), event["id"],
                                                int(newest["created_at"]), newest["id"]):
                    newest = event
            return newest

        def _on_first(events, answered: Set[str]) -> None:
            if not answered_by_own(answered):
                on_done(NO_ANSWER, [])
                return
            newest = newest_of(events, self.d_tag)
            if newest is None or not str(newest.get("content") or "").strip():
                on_done(None, [_Page(newest)])
                return
            self._decrypt(profile, newest,
                          lambda text: _on_first_text(newest, text),
                          lambda reason: _unreadable(reason, [_Page(newest)]))

        def _on_first_text(event: dict, text: str) -> None:
            payload = _parse(text)
            if payload is None:
                on_done(UNREADABLE, [_Page(event, text)])
                return
            first = _Page(event, text)
            if self._paging is None:
                _finish(payload, [first])
                return
            count = payload.get("pages")
            if (payload.get("v") != PAGED_VERSION or not isinstance(count, int)
                    or isinstance(count, bool) or not 1 <= count <= MAX_PAGES
                    or not isinstance(payload.get(self._paging.field), (list, dict))):
                _not_a_list("its first page")
                on_done(UNREADABLE, [first])
                return
            if count == 1:
                _finish(self._joined([payload]), [first])
                return
            tags = [page_d_tag(self.d_tag, n) for n in range(1, count)]
            self._query.events(read, [{"kinds": [self._kind], "authors": [profile.user_pubkey],
                                       "#d": tags}],
                               lambda found, answered: _on_rest(found, answered, first, payload,
                                                                tags))

        def _on_rest(events, answered, first: _Page, payload: dict, tags) -> None:
            if not answered_by_own(answered):
                on_done(NO_ANSWER, [])
                return
            pages = [first] + [_Page(newest_of(events, tag)) for tag in tags]
            for page in pages[1:]:
                if page.event is not None and not str(page.event.get("content") or "").strip():
                    page.event = None
            if _missing(pages):
                # Written but not on these relays (lost, or not arrived
                # yet): read without it, and written around it, so what
                # is on it is neither lost nor in the way.
                _log.info("the list %s is missing pages %s", self._name, _missing(pages))
                if self._words.missing_page:
                    self._on_status(self._words.missing_page)
            payloads = [payload]

            def next_page(index: int) -> None:
                if index == len(pages):
                    _finish(self._joined(payloads), pages)
                    return
                page = pages[index]
                if page.event is None:
                    next_page(index + 1)
                    return

                def got(text: str) -> None:
                    page.plaintext = text
                    parsed = _parse(text)
                    kind = type(payload[self._paging.field])
                    if (parsed is None or parsed.get("v") != PAGED_VERSION
                            or not isinstance(parsed.get(self._paging.field), kind)):
                        if parsed is not None:
                            _not_a_list(f"page {index}")
                        on_done(UNREADABLE, pages)
                        return
                    payloads.append(parsed)
                    next_page(index + 1)

                self._decrypt(profile, page.event, got,
                              lambda reason: _unreadable(reason, pages))

            next_page(1)

        def _finish(payload: dict, pages) -> None:
            try:
                value = self._decode(payload)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                # The account's own, but not what this list holds: as
                # unreadable as a page that cannot be decrypted.
                _log.warning("the list %s could not be taken in: %s", self._name, exc)
                self._on_status(self._words.unreadable)
                on_done(UNREADABLE, pages)
                return
            on_done(value, pages)

        def _parse(text: str) -> Optional[dict]:
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                _not_a_list("it")
                return None
            return payload

        def _not_a_list(what: str) -> None:
            _log.warning("the list %s is not a list (%s)", self._name, what)
            self._on_status(self._words.unreadable)

        def _unreadable(reason: str, pages) -> None:
            self._on_status(reason)
            on_done(UNREADABLE, pages)

        self._query.events(read, [{
            "kinds": [self._kind],
            "authors": [profile.user_pubkey],
            "#d": [self.d_tag],
            "limit": 1,
        }], _on_first)

    def _joined(self, payloads: Sequence[dict]) -> dict:
        """One plaintext object from a paged list's pages: page 0's other
        keys, and the field put back together in page order."""
        name = self._paging.field
        joined = {k: v for k, v in payloads[0].items() if k not in ("v", "page", "pages")}
        combine = self._paging.combine
        if isinstance(payloads[0][name], dict):
            together: Any = {}
            for page in payloads:
                for key, value in page[name].items():
                    if combine is not None and key in together:
                        value = combine(together[key], value)
                    together[key] = value
        else:
            together = []
            for page in payloads:
                together.extend(page[name])
        joined[name] = together
        return joined

    def _decrypt(self, profile, event: dict, on_text, on_unreadable) -> None:
        """The plaintext of a list, by its ``encrypted`` tag, as STANDUP
        reads it: NIP-44, NIP-04 when the signer offers it, or none."""
        algorithm = next((t[1] for t in event.get("tags", [])
                          if isinstance(t, list) and len(t) >= 2 and t[0] == "encrypted"), "")
        content = str(event.get("content") or "")
        if algorithm not in ("nip44", "nip44_v2", "nip04"):
            on_text(content)  # no encryption (a list from before it was used)
            return

        def failed(reason: str, words: str) -> None:
            """A signer app that did not answer is said as such, whatever
            else ``words`` would advise (CP3 review L9: a timeout read as
            the older encryption)."""
            _log.warning("the list %s (%s) was not read: %s", self._name, algorithm, reason)
            silent = is_signer_silent(reason) or reason == "not connected"
            on_unreadable(self._words.signer_silent if silent else words)

        def _on_ready(client) -> None:
            if algorithm == "nip04":
                decrypt = getattr(client, "nip04_decrypt_self", None)
                if decrypt is None:
                    on_unreadable(self._words.old_encryption)
                    return
                # A signer app that refuses NIP-04: what to do is the same
                # as without one.
                decrypt(content, on_success=on_text,
                        on_failure=lambda reason: failed(reason, self._words.old_encryption))
                return
            client.nip44_decrypt_self(content, on_success=on_text,
                                      on_failure=lambda reason: failed(reason,
                                                                       self._words.unreadable))

        self._session_pool.get(profile, on_ready=_on_ready,
                               on_error=lambda reason: failed(reason, self._words.signer_silent))

    # -- writing -------------------------------------------------------------

    def publish(self, profile, *, state: Callable[[], Tuple[Any, Any, Optional[Tuple[int, str]]]],
                on_done: Callable[[PublishResult], None],
                on_merged: Optional[Callable[[Any, Any, Optional[dict]], None]] = None,
                up_to_date: Optional[Callable[[Any, Any], bool]] = None) -> None:
        """Read the newest list, merge, and write what came of it.

        ``state()`` answers ``(base, local, base_mark)``: the version last
        known to be on the relays, the one here, and which event the base
        came from. It is asked when the relays have answered and again
        when the new version is dated, so it may change in between.
        ``on_merged(merged, remote, event)`` hears the merge before
        anything is written (``remote`` is the base again when what the
        relays had was not newer, and ``event`` then None).
        ``up_to_date(merged, remote)`` says when nothing needs writing
        (default: ``same``). ``on_done`` gets a PublishResult.
        """

        def _on_remote(remote, pages) -> None:
            if remote == UNREADABLE:
                # Never write over a list that could not be read. The
                # reason is reported already.
                on_done(PublishResult(UNREADABLE))
                return
            if remote == NO_ANSWER:
                # Never write over a list nobody answered for: a newer one
                # may be on the relays.
                on_done(PublishResult(NO_ANSWER))
                return
            base, local, base_mark = state()
            event = pages[0].event if pages else None
            mark = event_mark(event)
            written = pages
            if (event is not None and base_mark is not None and mark is not None
                    and not supersedes(mark[0], mark[1], *base_mark)):
                # A copy no newer than the last seen (a slow or fallback
                # relay's): it changes nothing; what is new here goes out.
                remote, event, written = base, None, []
            merged = self._merge(base, local, remote)
            if on_merged is not None:
                on_merged(merged, remote, event)
            if remote is not None and (up_to_date or self._same)(merged, remote):
                on_done(PublishResult(UP_TO_DATE, remote=remote))
                return
            self._write(profile, copy.deepcopy(merged), written, pages, state, on_done)

        self.relays(profile, lambda relays: self._read_pages(profile, relays, _on_remote),
                    reading=True)

    def _plaintexts(self, value, now: int, keep: Sequence[int] = ()) -> Dict[int, str]:
        payload = self._encode(value, now)
        if self._paging is None:
            return {0: _serialize(payload)}
        return layout_pages(payload, self._paging, keep)

    def _write(self, profile, content, remote_pages: List[_Page], read_pages: List[_Page],
               state, on_done) -> None:
        """Write ``content``. ``remote_pages`` are the pages on the relays
        that count (none when they were older than the last list seen),
        ``read_pages`` every page read, which each new page is dated after."""
        def failed(reason: str) -> None:
            on_done(PublishResult(FAILED, reason=reason))

        # A page no relay had is left as it is: its place stays in the
        # list, and nothing is written over it.
        missing = tuple(_missing(read_pages))
        try:
            plaintexts = self._plaintexts(content, int(self._clock()), missing)
        except ValueError as exc:
            failed(str(exc))
            return
        # The pages after the first go out first, and page 0, which says
        # how many there are and is the one a reader starts from, last:
        # a reader who sees the new page 0 finds the pages it names. A
        # page after the first that reads as written already is not
        # signed again; page 0 always is, because only a newer page 0
        # tells the other devices there is something new.
        later = [i for i in sorted(plaintexts, reverse=True) if i > 0
                 and not (i < len(remote_pages) and remote_pages[i].event is not None
                          and remote_pages[i].plaintext == plaintexts[i])]
        order = later + [0]
        dated: List[int] = []

        def created_at() -> int:
            if not dated:
                # After the list it replaces and after the last one seen,
                # even when this computer's clock runs behind.
                known = state()[2] or (0, "")
                replaced = [page.event for page in read_pages if page.event] or [None]
                dated.append(max(max(replacement_created_at(event, self._clock())
                                     for event in replaced), known[0] + 1))
            return dated[0]

        def _on_ready(client) -> None:
            def page(step: int) -> None:
                index = order[step]
                client.nip44_encrypt_self(
                    plaintexts[index],
                    on_success=lambda ciphertext: _sign(step, index, ciphertext),
                    on_failure=failed)

            def _sign(step: int, index: int, ciphertext: str) -> None:
                unsigned = build_event(
                    kind=self._kind,
                    content=ciphertext,
                    tags=[["d", page_d_tag(self.d_tag, index)], ["client", CLIENT_NAME],
                          ["encrypted", "nip44"]],
                    pubkey_hex=profile.user_pubkey,
                    created_at=created_at(),
                )
                client.sign_event(unsigned, on_success=lambda signed: _publish(step, signed),
                                  on_failure=failed)

            def _publish(step: int, signed: dict) -> None:
                self.relays(profile, lambda relays: self._publisher(
                    relays, signed,
                    on_done=lambda accepted, total: _done(step, accepted, signed)))

            def _done(step: int, accepted: int, signed: dict) -> None:
                if accepted <= 0:
                    on_done(PublishResult(NOT_ACCEPTED))
                    return
                if step + 1 < len(order):
                    page(step + 1)
                    return
                on_done(PublishResult(SENT, value=content, signed=signed, missing=missing))

            page(0)

        self._session_pool.get(profile, on_ready=_on_ready, on_error=failed)
