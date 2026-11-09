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

A long list is split into pages (``Paging``; the rules are
``nostr/paged_list.py``'s, from STANDUP's editorial contract 13). A
write is one complete generation: the later pages go to every relay
first, and page 0, which binds them by their hashes, goes last and only
to the relays that took every later page, so no relay ever holds a page
0 whose pages it lacks. A read that finds a page missing or of another
generation is incomplete (``ReadStatus.INCOMPLETE``): it keeps every
page that is the account's own, and the next write builds a complete
generation from them. A list that is not paged keeps the format it
always had: one event, the plaintext exactly as ``encode`` gives it.

Every boundary (relay queries, publishing, signer, clock) is passed in,
so a list is testable without relays, a signer or wall-clock time.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject

from . import CLIENT_NAME
from .bunker import is_signer_silent, readable_back
from .drafts import supersedes
from .events import build_event
from .list_relays import LIST_KIND, event_mark, is_list_of, relay_key
from .list_results import (
    ListRead,
    ListWords,
    PageRead,
    PublishResult,
    PublishStatus,
    ReadStatus,
)
from .outbox import ask_private_relays
from .outbox.policy import replacement_created_at
from .paged_list import (
    PAGE_PAYLOAD_MAX,
    Page,
    PageError,
    Paging,
    join,
    page_d_tag,
    parse_page,
    serialize,
    split,
    stale_pages,
)

__all__ = ["EncryptedList"]

_log = logging.getLogger(__name__)


_NOT_READ = {ReadStatus.NO_ANSWER: PublishStatus.NO_ANSWER,
             ReadStatus.UNREADABLE: PublishStatus.UNREADABLE}


@dataclass
class _Request:
    """A publish asked for: what it merges and who hears how it ended. A
    request that waits takes over the hooks of a newer one and keeps
    every listener (each hears the outcome of the write that carried
    its state)."""

    profile: Any
    state: Callable[[], Tuple[Any, Any, Optional[Tuple[int, str]]]]
    on_done: List[Callable[[PublishResult], None]]
    on_merged: Optional[Callable[[Any, Any, Optional[dict]], None]]
    up_to_date: Optional[Callable[[Any, Any], bool]]

    def carrying(self, earlier: Optional["_Request"]) -> "_Request":
        if earlier is None:
            return self
        return _Request(self.profile, self.state, earlier.on_done + self.on_done,
                        self.on_merged, self.up_to_date)


class EncryptedList(QObject):
    """One account's encrypted list under one ``d`` tag: read, merged,
    written. Holds no state of its own between calls; the caller keeps
    what it last saw (the base), what it has (local), and where it came
    from, and hands them in.

    ``decode(payload_dict)`` gives the value of a plaintext, ``encode(value,
    now)`` the plaintext JSON object of a value, ``merge(base, local,
    remote)`` both sides' edits since ``base`` in one value (``remote`` may
    be None: no list yet), and ``same(a, b)`` whether two values say the
    same (default ``==``). A paged list's ``merge`` must be a union: an
    incomplete read's value lacks what was on the pages it did not find.

    ``publisher(relays, signed, *, on_done, every_relay=False)`` publishes
    one event and answers ``on_done(relays that took it)``, see
    ``list_relays.publish_once``.

    ``untagged`` is what an event without an ``encrypted`` tag holds:
    None, plaintext (the feed list before it was encrypted), or
    ``"nip44"`` (a list whose format says NIP-44 to self without a tag,
    as STANDUP's do). Events written here always carry the tag.
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
        kind: int = LIST_KIND,
        untagged: Optional[str] = None,
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
        self._untagged = untagged or ""
        self._on_status = on_status or (lambda _text: None)
        self._name = name or d_tag
        # One write at a time per account (13): two at once would date
        # their pages alike and mix them. Accounts with a write on its
        # way, and the request that goes next for each.
        self._running: set = set()
        self._next: Dict[str, _Request] = {}

    @property
    def busy(self) -> bool:
        """Whether a write is on its way, for any account."""
        return bool(self._running)

    def is_writing(self, profile) -> bool:
        """Whether a write for ``profile``'s account is on its way (it
        carries the latest state: what comes now goes next)."""
        return profile.user_pubkey.lower() in self._running

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

    def read(self, profile, relays, on_done: Callable[[ListRead], None]) -> None:
        """The newest list, as a :class:`ListRead`. ``relays`` is
        ``(read, own)`` (see :meth:`relays`)."""
        if self._query is None:
            on_done(ListRead(ReadStatus.NO_ANSWER))
            return
        _Reading(self, profile, relays, on_done).start()

    def _decrypt(self, profile, event: dict, on_text, on_unreadable) -> None:
        """The plaintext of a list, by its ``encrypted`` tag: NIP-44, NIP-04
        when the signer offers it, or none (see ``untagged``)."""
        algorithm = next((t[1] for t in event.get("tags", [])
                          if isinstance(t, list) and len(t) >= 2 and t[0] == "encrypted"),
                         self._untagged)
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
        (default: ``same``); an incomplete list is always written, so it
        is complete again. ``on_done`` gets a PublishResult.

        One write runs at a time per account. A publish asked while one
        runs waits and goes next, with ``state()`` asked then; several
        asked meanwhile go as one, the newest's hooks, every ``on_done``
        answered with its outcome.
        """
        account = profile.user_pubkey.lower()
        request = _Request(profile, state, [on_done], on_merged, up_to_date)
        if account in self._running:
            self._next[account] = request.carrying(self._next.get(account))
            return
        self._running.add(account)
        self._run(account, request)

    def _run(self, account: str, request: _Request) -> None:
        def finished(result: PublishResult) -> None:
            following = self._next.pop(account, None)
            if following is None:
                self._running.discard(account)
            for listener in request.on_done:
                listener(result)
            if following is not None:
                # What a listener asked for meanwhile goes along with it.
                asked = self._next.pop(account, None)
                self._run(account, asked.carrying(following) if asked else following)

        self._write(request, finished)

    def _write(self, request: _Request, on_done: Callable[[PublishResult], None]) -> None:
        profile, state = request.profile, request.state
        on_merged, up_to_date = request.on_merged, request.up_to_date

        def _on_read(read: ListRead) -> None:
            if not read.readable:
                # Never write over a list that could not be read, or one
                # nobody answered for: a newer one may be on the relays.
                on_done(PublishResult(_NOT_READ[read.status]))
                return
            base, local, base_mark = state()
            remote, event, reusable = read.value, read.event, read.later
            mark = event_mark(event)
            if (event is not None and base_mark is not None and mark is not None
                    and not supersedes(mark[0], mark[1], *base_mark)):
                # A copy no newer than the last seen (a slow or fallback
                # relay's): it changes nothing; what is new here goes out.
                # Its pages are reused only when it is the very list last
                # seen: then they are what the relays hold.
                if tuple(mark) != tuple(base_mark):
                    reusable = {}
                remote, event = base, None
            merged = self._merge(base, local, remote)
            if on_merged is not None:
                on_merged(merged, remote, event)
            if (read.status is ReadStatus.COMPLETE and remote is not None
                    and (up_to_date or self._same)(merged, remote)):
                on_done(PublishResult(PublishStatus.UP_TO_DATE, remote=remote))
                return
            _Writing(self, profile, copy.deepcopy(merged), read, reusable, state,
                     on_done).start()

        self.relays(profile, lambda relays: self.read(profile, relays, _on_read), reading=True)

    def _plaintexts(self, value, now: int) -> List[str]:
        """Page 0's plaintext first. Raises ValueError when the list
        cannot be written (an entry too large, too many pages)."""
        payload = self._encode(value, now)
        if self._paging is None:
            return [serialize(payload)]
        return split(payload, self._paging)

    def _page_event(self, profile, index: int, ciphertext: str, created_at: int) -> dict:
        return build_event(
            kind=self._kind, content=ciphertext,
            tags=[["d", page_d_tag(self.d_tag, index)], ["client", CLIENT_NAME],
                  ["encrypted", "nip44"]],
            pubkey_hex=profile.user_pubkey, created_at=created_at)


# --------------------------------------------------------------------------- #
# One read and one write                                                       #
# --------------------------------------------------------------------------- #

class _Reading:
    """One read: page 0, then the later pages it names, one decryption
    at a time."""

    def __init__(self, owner: EncryptedList, profile, relays,
                 on_done: Callable[[ListRead], None]) -> None:
        self._list = owner
        self._profile = profile
        self._read_relays, own = relays
        self._own = {relay_key(u) for u in own}
        self._on_done = on_done
        self._event: Optional[dict] = None
        self._first: Optional[Page] = None
        self._found: Dict[int, dict] = {}
        self._later: Dict[int, PageRead] = {}

    def start(self) -> None:
        self._query([self._list.d_tag], self._on_first, limit=1)

    def _query(self, d_tags: Sequence[str], on_done, limit: Optional[int] = None) -> None:
        filter_: Dict[str, Any] = {"kinds": [self._list._kind],
                                   "authors": [self._profile.user_pubkey], "#d": list(d_tags)}
        if limit is not None:
            filter_["limit"] = limit
        self._list._query.events(self._read_relays, [filter_], on_done)

    def _answered_by_own(self, answered) -> bool:
        answered = {relay_key(u) for u in answered}
        return bool(answered) and not (self._own and not (answered & self._own))

    def _newest(self, events, d_tag: str) -> Optional[dict]:
        newest = None
        for event in events:
            if not is_list_of(event, self._profile.user_pubkey, d_tag, self._list._kind):
                continue
            if newest is None or supersedes(int(event["created_at"]), event["id"],
                                            int(newest["created_at"]), newest["id"]):
                newest = event
        if newest is not None and not str(newest.get("content") or "").strip():
            return None
        return newest

    def _end(self, status: ReadStatus, value: Any = None, stale: Tuple[int, ...] = ()) -> None:
        self._on_done(ListRead(status, value=value, event=self._event, stale=stale,
                               later=dict(self._later)))

    def _unreadable(self, words: str, why: str = "") -> None:
        if why:
            _log.warning("the list %s is not a list (%s)", self._list._name, why)
        self._list._on_status(words)
        self._end(ReadStatus.UNREADABLE)

    def _on_first(self, events, answered) -> None:
        if not self._answered_by_own(answered):
            self._on_done(ListRead(ReadStatus.NO_ANSWER))
            return
        self._event = self._newest(events, self._list.d_tag)
        if self._event is None:
            self._end(ReadStatus.MISSING)
            return
        if self._too_long(self._event):
            self._unreadable(self._list._words.unreadable, "its first page is longer than a page")
            return
        self._list._decrypt(self._profile, self._event, self._on_first_text, self._unreadable)

    def _too_long(self, event: dict) -> bool:
        """A page whose ciphertext holds more than a page (13): never
        sent to a signer app, whose answer might not fit."""
        return self._list._paging is not None and len(event["content"]) > PAGE_PAYLOAD_MAX

    def _on_first_text(self, text: str) -> None:
        paging = self._list._paging
        if paging is None:
            try:
                payload = _parse_object(text)
            except PageError as exc:
                self._unreadable(self._list._words.unreadable, str(exc))
                return
            self._decoded(payload, ())
            return
        try:
            self._first = parse_page(text, index=0, paging=paging)
        except PageError as exc:
            self._unreadable(self._list._words.unreadable, f"its first page: {exc}")
            return
        if self._first.pages == 1:
            self._joined()
            return
        tags = [page_d_tag(self._list.d_tag, n) for n in range(1, self._first.pages)]
        self._query(tags, self._on_rest)

    def _on_rest(self, events, answered) -> None:
        if not self._answered_by_own(answered):
            self._on_done(ListRead(ReadStatus.NO_ANSWER))
            return
        for index in range(1, self._first.pages):
            newest = self._newest(events, page_d_tag(self._list.d_tag, index))
            if newest is not None and self._too_long(newest):
                _log.info("page %d of the list %s is longer than a page", index,
                          self._list._name)
            elif newest is not None:
                self._found[index] = newest
        self._next(sorted(self._found))

    def _next(self, indices: List[int]) -> None:
        if not indices:
            self._joined()
            return
        index, rest = indices[0], indices[1:]
        event = self._found[index]

        def got(text: str) -> None:
            try:
                page = parse_page(text, index=index, paging=self._list._paging,
                                  first=self._first)
            except PageError as exc:
                # Not a page of this list: it counts as missing (13).
                _log.info("page %d of the list %s does not count: %s", index,
                          self._list._name, exc)
            else:
                self._later[index] = PageRead(event, page)
            self._next(rest)

        # A page that cannot be decrypted is unreadable, not missing: it
        # is the account's own, and writing a new generation would put
        # something else in its place.
        self._list._decrypt(self._profile, event, got, self._unreadable)

    def _joined(self) -> None:
        later = {index: read.page for index, read in self._later.items()}
        stale = stale_pages(self._first, later)
        if stale:
            _log.info("the list %s is incomplete: pages %s", self._list._name, stale)
            if self._list._words.missing_page:
                self._list._on_status(self._list._words.missing_page)
        self._decoded(join(self._first, list(later.values()), self._list._paging), stale)

    def _decoded(self, payload: dict, stale: Tuple[int, ...]) -> None:
        try:
            value = self._list._decode(payload)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # The account's own, but not what this list holds: as
            # unreadable as a page that cannot be decrypted.
            self._unreadable(self._list._words.unreadable, f"not taken in: {exc}")
            return
        self._end(ReadStatus.INCOMPLETE if stale else ReadStatus.COMPLETE, value, stale)


def _parse_object(text: str) -> dict:
    """An unpaged list's plaintext: a JSON object, or PageError."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise PageError("not JSON") from None
    if not isinstance(payload, dict):
        raise PageError("not a JSON object")
    return payload


class _Writing:
    """One write of a complete generation: every page signed (a later
    page whose plaintext is on the relays already is not signed again),
    the later pages published to every relay, then page 0 to the relays
    that took all of them."""

    def __init__(self, owner: EncryptedList, profile, value, read: ListRead,
                 reusable: Mapping[int, PageRead], state, on_done) -> None:
        self._list = owner
        self._profile = profile
        self._value = value
        self._read = read
        self._reusable = reusable
        self._state = state
        self._on_done = on_done
        self._plaintexts: List[str] = []
        self._signed: Dict[int, dict] = {}
        self._dated: Optional[int] = None
        self._client = None
        self._pending: set = set()
        self._took: Dict[str, str] = {}

    def start(self) -> None:
        try:
            self._plaintexts = self._list._plaintexts(self._value, int(self._list._clock()))
        except ValueError as exc:
            self._fail(str(exc))
            return
        # Written only what this account can read again: a list the signer
        # app encrypts but cannot hand back would be unreadable, and an
        # unreadable list is never written over (contract 12.5, 13).
        for index, text in enumerate(self._plaintexts):
            if not readable_back(self._profile, text):
                self._fail(f"page {index} ({len(text.encode('utf-8'))} bytes) is too large "
                           f"for this account to read back")
                return
        self._list._session_pool.get(self._profile, on_ready=self._ready, on_error=self._fail)

    def _fail(self, reason: str) -> None:
        self._on_done(PublishResult(PublishStatus.FAILED, reason=reason))

    def _ready(self, client) -> None:
        self._client = client
        # The later pages first, page 0 last: it is the one that says how
        # many pages there are and binds them.
        self._sign(list(range(len(self._plaintexts) - 1, 0, -1)) + [0])

    def _created_at(self) -> int:
        """After every page read and after the last list seen, even when
        this computer's clock runs behind; the same for every page."""
        if self._dated is None:
            known = self._state()[2] or (0, "")
            replaced = self._read.events() or [None]
            self._dated = max(max(replacement_created_at(event, self._list._clock())
                                  for event in replaced), known[0] + 1)
        return self._dated

    def _sign(self, order: List[int]) -> None:
        if not order:
            self._list.relays(self._profile, self._publish_later)
            return
        index, rest = order[0], order[1:]
        reused = self._reusable.get(index) if index > 0 else None
        if reused is not None and reused.page.plaintext == self._plaintexts[index]:
            self._signed[index] = reused.event
            self._sign(rest)
            return

        def encrypted(ciphertext: str) -> None:
            unsigned = self._list._page_event(self._profile, index, ciphertext,
                                              self._created_at())
            self._client.sign_event(unsigned, on_success=signed, on_failure=self._fail)

        def signed(event: dict) -> None:
            self._signed[index] = event
            self._sign(rest)

        self._client.nip44_encrypt_self(self._plaintexts[index], on_success=encrypted,
                                        on_failure=self._fail)

    def _publish_later(self, relays: List[str]) -> None:
        later = [index for index in self._signed if index > 0]
        if not later:
            self._publish_first(list(relays))
            return
        self._pending = set(later)
        self._took = {relay_key(url): url for url in relays}
        for index in later:
            self._list._publisher(relays, self._signed[index], every_relay=True,
                                  on_done=lambda accepted, n=index: self._later_done(n, accepted))

    def _later_done(self, index: int, accepted: Collection[str]) -> None:
        took = {relay_key(url) for url in accepted}
        self._took = {key: url for key, url in self._took.items() if key in took}
        self._pending.discard(index)
        if self._pending:
            return
        if not self._took:
            self._on_done(PublishResult(PublishStatus.NOT_ACCEPTED))
            return
        self._publish_first(list(self._took.values()))

    def _publish_first(self, relays: List[str]) -> None:
        def done(accepted: Collection[str]) -> None:
            if not accepted:
                self._on_done(PublishResult(PublishStatus.NOT_ACCEPTED))
                return
            self._on_done(PublishResult(PublishStatus.SENT, value=self._value,
                                        signed=self._signed[0]))

        self._list._publisher(relays, self._signed[0], on_done=done)
