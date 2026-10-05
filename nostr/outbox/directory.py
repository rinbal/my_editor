# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RelayDirectory: the one place that knows where anyone reads and writes.

It answers "where are this person's relays" (looked up, verified, cached)
and, through the pure rules in policy.py, "where does this go":

    publish_plan    a public note or article: the author's outbox, plus the
                    inbox of everyone it mentions (NIP-65)
    private_relays  drafts and other private records; reading asks where
                    writing goes, plus the fallback relays records went to
                    while the list was unknown, so other devices find them
                    (ask_private_relays asks it for a profile)
    outbox_of       where to read what someone else wrote

Caching. A FOUND list is trusted for half an hour; after that it is still
answered at once while a refresh runs (stale while revalidate). A refresh
only ever replaces it with a newer validly signed list: a timeout or an
empty answer never downgrades a list we know. ABSENT is trusted for three
minutes, UNKNOWN for thirty seconds, just long enough not to hammer relays.

The user's own lists are kept on disk as the signed events themselves
(re-verified on load), so routing is right from the first second after a
launch, even offline. Everyone else's are kept in memory only, at most
DIRECTORY_CAP of them, the least recently used dropped first. Ages are
measured on a monotonic clock, so a clock change cannot make a list look
fresh for hours, or stale at once.

Every answer is a copy: a caller changing the RelayList it was handed
changes nothing here.

Every list MyEditor publishes goes through ``remember``, which seeds the
cache with the known-good event instead of asking relays that may not
have it yet.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from .. import events
from . import defaults, policy
from .lookup import Lookup, classify, fetch_replaceable, fetch_replaceable_many
from .policy import KIND_RELAY_LIST, LookupState, RelayList

RELAY_LISTS_FILE = Path.home() / ".config" / "my_editor" / "nostr_relay_lists.json"

logger = logging.getLogger(__name__)

_TTL = {
    LookupState.FOUND: defaults.TTL_FOUND_S,
    LookupState.ABSENT: defaults.TTL_ABSENT_S,
    LookupState.UNKNOWN: defaults.TTL_UNKNOWN_S,
}


@dataclass
class _Pending:
    """A lookup under way: who waits for it, which relays it asked, and
    the best answer so far from each request it made."""

    callbacks: List[Callable[[RelayList], None]] = field(default_factory=list)
    asked: set = field(default_factory=set)
    outstanding: int = 0
    best: Optional[dict] = None
    answered: set = field(default_factory=set)
    unchecked: bool = False


class RelayDirectory(QObject):
    """Relay lists by pubkey, and the routing built on them."""

    changed = Signal(str)       # pubkey whose known list became newer

    def __init__(self, pool, *, query=fetch_replaceable, query_many=fetch_replaceable_many,
                 store_path: Optional[Path] = RELAY_LISTS_FILE,
                 own_pubkeys: Callable[[], Iterable[str]] = tuple,
                 clock: Callable[[], float] = time.monotonic,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._query = query
        self._query_many = query_many
        self._store_path = Path(store_path) if store_path else None
        self._own_pubkeys = own_pubkeys
        self._clock = clock
        self._entries: "OrderedDict[str, RelayList]" = OrderedDict()
        self._pending: Dict[str, _Pending] = {}
        # author -> {relay: id of the relay list sent there}, so a list is
        # shared with a relay once, not with every note that reaches it.
        self._shared: Dict[str, Dict[str, str]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Knowing                                                            #
    # ------------------------------------------------------------------ #

    def cached(self, pubkey: str) -> RelayList:
        """What is known now, however old; UNKNOWN when nothing is."""
        entry = self._entries.get((pubkey or "").lower())
        return _copy(entry) if entry is not None else RelayList()

    def lookup(self, pubkey: str, on_done: Callable[[RelayList], None], *,
               hints: Sequence[str] = (), fresh: bool = False,
               timeout_ms: int = 6_000) -> None:
        """The person's relay list. Answered from cache when it is fresh; a
        stale FOUND list is answered at once and refreshed behind it."""
        key = (pubkey or "").lower()
        entry = self._entries.get(key)
        if entry is not None and not fresh:
            self._entries.move_to_end(key)
            answer = _copy(entry)
            if not self._expired(entry):
                QTimer.singleShot(0, lambda: on_done(answer))
                return
            if entry.found:
                QTimer.singleShot(0, lambda: on_done(answer))
                self._start(key, hints, timeout_ms, None)
                return
        self._start(key, hints, timeout_ms, on_done)

    def lookup_many(self, pubkeys: Sequence[str],
                    on_done: Callable[[Dict[str, RelayList]], None], *,
                    hints: Optional[Mapping[str, Sequence[str]]] = None,
                    timeout_ms: int = 3_000) -> None:
        """Several people's lists at once (the people a note mentions).

        Whoever is not answered from cache is asked for in one request, one
        filter naming every author, rather than one request each; ``hints``
        maps a pubkey to relays it was seen on (a mention's relay hint).
        At most MENTION_LOOKUP_CAP people are looked up.
        """
        keys = list(dict.fromkeys((p or "").lower() for p in pubkeys if p))
        keys = keys[:defaults.MENTION_LOOKUP_CAP]
        hints = {k.lower(): list(v or ()) for k, v in (hints or {}).items()}
        result: Dict[str, RelayList] = {}
        if not keys:
            QTimer.singleShot(0, lambda: on_done(result))
            return
        remaining = set(keys)

        def one(key):
            def done(relay_list):
                result[key] = relay_list
                remaining.discard(key)
                if not remaining:
                    on_done(result)
            return done

        batch: Dict[str, Optional[Callable[[RelayList], None]]] = {}
        for key in keys:
            entry = self._entries.get(key)
            if entry is not None and (not self._expired(entry) or entry.found):
                self._entries.move_to_end(key)
                QTimer.singleShot(0, lambda e=_copy(entry), d=one(key): d(e))
                if not self._expired(entry):
                    continue
                callback = None                    # stale but known: refresh behind it
            else:
                callback = one(key)
            if key in self._pending:
                self._start(key, hints.get(key, ()), timeout_ms, callback)
            else:
                batch[key] = callback
        if batch:
            self._start_batch(batch, hints, timeout_ms)

    def remember(self, event: dict) -> bool:
        """Take a relay list we hold (one we just published, or were handed).
        Only a validly signed one newer than what is known is kept."""
        if not isinstance(event, dict) or event.get("kind") != KIND_RELAY_LIST:
            return False
        if policy.created_at_of(event) is None or not events.verify_event(event):
            return False
        key = str(event.get("pubkey", "")).lower()
        current = self._entries.get(key)
        if current is not None and current.found and not policy.is_newer(event, current.event):
            return False
        self._store(key, self._found(event))
        return True

    # ------------------------------------------------------------------ #
    # Routing                                                            #
    # ------------------------------------------------------------------ #

    def publish_plan(self, author: str, on_done: Callable[[policy.PublishPlan], None], *,
                     mentioned: Sequence[Tuple[str, str]] = (),
                     entitled: Sequence[str] = ()) -> None:
        """Where a public event goes; mentions are ``(pubkey, hint)`` pairs."""
        state: dict = {}
        keys = list(dict.fromkeys(p.lower() for p, _hint in mentioned))
        hints: Dict[str, List[str]] = {}
        for pubkey, hint in mentioned:
            if hint:
                hints.setdefault(pubkey.lower(), []).append(hint)

        def maybe_done():
            if "author" in state and "mentions" in state:
                # In the order the event mentions them, whatever order the
                # answers came in: the inbox slots go round in that order.
                lists = state["mentions"]
                mentions = {k: lists[k] for k in keys if k in lists}
                on_done(policy.plan_publish(state["author"], mentioned=mentions,
                                            hints={k: v[0] for k, v in hints.items()},
                                            entitled=entitled,
                                            own={author.lower(), *self._own_keys()}))

        def got_author(relay_list):
            state["author"] = relay_list
            maybe_done()

        def got_mentions(lists):
            state["mentions"] = lists
            maybe_done()

        self.lookup(author, got_author)
        self.lookup_many(keys, got_mentions, hints=hints)

    def private_relays(self, author: str, on_done: Callable[[List[str]], None], *,
                       entitled: Sequence[str] = (), legacy: Sequence[str] = (),
                       reading: bool = False) -> None:
        """Where the author's private records are written, or with
        ``reading`` read from (policy.private_relays)."""
        self.lookup(author, lambda relay_list: on_done(policy.private_relays(
            relay_list, entitled=entitled, legacy=legacy, reading=reading)))

    def outbox_of(self, author: str, on_done: Callable[[List[str]], None], *,
                  hints: Sequence[str] = ()) -> None:
        """Where to read what ``author`` wrote (policy.outbox_relays)."""
        own = self._is_own(author)
        self.lookup(author, lambda relay_list: on_done(policy.outbox_relays(
            relay_list, hints=hints, own=own)), hints=hints)

    def share_relay_list(self, author: str, relays: Sequence[str]) -> None:
        """Send the author's signed relay list to relays it just published to
        but that do not hold it (NIP-65). Needs no signer: it is the event
        we already have. A relay that already took this very list is not
        sent it again; one that refused it is tried again next time."""
        key = (author or "").lower()
        entry = self._entries.get(key)
        if entry is None or not entry.found or entry.event is None:
            return
        event_id = str(entry.event.get("id", ""))
        listed = set(entry.write) | set(entry.read)
        sent = self._shared.setdefault(key, {})
        extra = [r for r in policy.dedupe_relays(relays)
                 if r not in listed and sent.get(r) != event_id]
        if not extra:
            return
        for url in extra:
            sent[url] = event_id
        while len(sent) > defaults.DIRECTORY_CAP:
            sent.pop(next(iter(sent)))
        job = self._pool.publish(extra, entry.event)

        def done(results):
            for url, ok, _message in results:
                url = policy.normalize_relay_url(url) or url
                if not ok and sent.get(url) == event_id:
                    del sent[url]

        job.all_done.connect(done)

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _own_keys(self) -> set:
        return {p.lower() for p in self._own_pubkeys() if p}

    def _is_own(self, pubkey: str) -> bool:
        return (pubkey or "").lower() in self._own_keys()

    def _expired(self, entry: RelayList) -> bool:
        return (self._clock() - entry.fetched_at) >= _TTL[entry.state]

    def _start(self, key: str, hints, timeout_ms: int, on_done) -> None:
        """Look ``key`` up, or join the lookup already running for it. A
        caller bringing relays that lookup does not ask (a new hint) has
        them asked as well, and the answer waits for both."""
        relays = policy.lookup_relays(hints=hints, known=self._entries.get(key),
                                      own=self._is_own(key))
        pending = self._pending.get(key)
        if pending is None:
            pending = self._pending[key] = _Pending()
        if on_done is not None:
            pending.callbacks.append(on_done)
        relays = [r for r in relays if r not in pending.asked]
        if not relays and pending.outstanding:
            return
        pending.asked.update(relays)
        pending.outstanding += 1
        self._query(self._pool, relays, kind=KIND_RELAY_LIST, author=key,
                    on_done=lambda result: self._contribute(key, result),
                    timeout_ms=timeout_ms, parent=self)

    def _start_batch(self, batch: Mapping[str, Optional[Callable[[RelayList], None]]],
                     hints: Mapping[str, Sequence[str]], timeout_ms: int) -> None:
        """One request for everyone in ``batch``: their lists are wherever
        the indexers and the relays they were seen on are."""
        keys = list(batch)
        relays = policy.lookup_relays(hints=[h for k in keys for h in hints.get(k, ())])
        for key in keys:
            pending = self._pending[key] = _Pending(asked=set(relays), outstanding=1)
            if batch[key] is not None:
                pending.callbacks.append(batch[key])

        def answered(results):
            for key in keys:
                self._contribute(key, results.get(key) or Lookup(LookupState.UNKNOWN))

        self._query_many(self._pool, relays, kind=KIND_RELAY_LIST, authors=keys,
                         on_done=answered, timeout_ms=timeout_ms, parent=self)

    def _contribute(self, key: str, result: Lookup) -> None:
        """One request's answer about ``key``; the lookup is answered when
        every request it made has."""
        pending = self._pending.get(key)
        if pending is None:
            return
        event = result.event if result.state is LookupState.FOUND else None
        if event is not None and policy.is_newer(event, pending.best):
            pending.best = event
        pending.answered.update(result.answered)
        pending.unchecked = pending.unchecked or result.unchecked
        pending.outstanding -= 1
        if pending.outstanding > 0:
            return
        del self._pending[key]
        answered = tuple(sorted(pending.answered))
        if pending.unchecked:
            final = Lookup(LookupState.UNKNOWN, answered=answered, unchecked=True)
        else:
            final = Lookup(classify(pending.best, answered), event=pending.best,
                           answered=answered)
        self._answered(key, pending.callbacks, final)

    def _answered(self, key: str, callbacks, result: Lookup) -> None:
        # The callers were let go of first (_contribute): whatever happens
        # below, a later lookup of this person starts afresh instead of
        # queueing behind this one.
        answer = RelayList(state=LookupState.UNKNOWN)
        try:
            answer = self._settle(key, result)
        except Exception:  # noqa: BLE001, a bad answer must not strand the callers
            logger.exception("relay list lookup for %s could not be settled", key)
        for callback in callbacks:
            try:
                callback(_copy(answer))
            except Exception:  # noqa: BLE001, one caller's bug must not break the others
                logger.exception("relay list callback for %s failed", key)

    def _settle(self, key: str, result: Lookup) -> RelayList:
        """Fold one lookup into what is known, and return what is known."""
        current = self._entries.get(key)
        event = result.event if result.state is LookupState.FOUND else None
        if event is not None and policy.created_at_of(event) is not None:
            if current is None or not current.found or policy.is_newer(event, current.event):
                self._store(key, self._found(event))
            else:
                current.fetched_at = self._clock()   # what we hold is as new, or newer
        elif current is not None and current.found:
            current.fetched_at = self._clock() - _TTL[LookupState.FOUND] + defaults.TTL_UNKNOWN_S
        else:
            state = LookupState.UNKNOWN if result.state is LookupState.FOUND else result.state
            self._put(key, RelayList(state=state, fetched_at=self._clock()))
        return self._entries[key]

    def _found(self, event: dict) -> RelayList:
        relay_list = policy.parse_relay_list(event)
        relay_list.fetched_at = self._clock()
        return relay_list

    def _store(self, key: str, relay_list: RelayList) -> None:
        previous = self._entries.get(key)
        self._put(key, relay_list)
        if self._is_own(key):
            self._save()
        if previous is None or previous.created_at != relay_list.created_at:
            self.changed.emit(key)

    def _put(self, key: str, relay_list: RelayList) -> None:
        """Keep ``relay_list`` as the newest used, and drop the least
        recently used of other people's beyond DIRECTORY_CAP."""
        self._entries[key] = relay_list
        self._entries.move_to_end(key)
        if len(self._entries) <= defaults.DIRECTORY_CAP:
            return
        own = self._own_keys()
        others = [k for k in self._entries if k not in own]
        for old in others[:len(others) - defaults.DIRECTORY_CAP]:
            del self._entries[old]

    # -- persistence of the user's own lists -----------------------------------

    def _load(self) -> None:
        if self._store_path is None:
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for event in (data.get("lists") or {}).values() if isinstance(data, dict) else ():
            if (isinstance(event, dict) and event.get("kind") == KIND_RELAY_LIST
                    and policy.created_at_of(event) is not None
                    and events.verify_event(event)):
                relay_list = policy.parse_relay_list(event)
                relay_list.fetched_at = float("-inf")   # known, but due for a refresh
                self._entries[str(event["pubkey"]).lower()] = relay_list

    def _save(self) -> None:
        if self._store_path is None:
            return
        own = self._own_keys()
        lists = {k: e.event for k, e in self._entries.items()
                 if k in own and e.found and e.event is not None}
        folder = self._store_path.parent
        tmp: Optional[str] = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".relay_lists_", dir=str(folder))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "lists": lists}, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._store_path)
            tmp = None
        except (OSError, TypeError, ValueError) as exc:
            # The lists are still known for this session; only the head
            # start on the next launch is lost.
            logger.warning("could not save the relay lists: %s", exc)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _copy(relay_list: RelayList) -> RelayList:
    """A RelayList nobody else holds, event and all."""
    return copy.deepcopy(relay_list)


def ask_private_relays(directory, profile, on_done: Callable[[List[str]], None], *,
                       entitled=(), reading: bool = False) -> None:
    """Where ``profile``'s private records live (drafts, synced settings,
    private files): written to, or with ``reading`` read from.

    ``entitled`` is a list of relays, or a callable answering one (asked
    now, so a membership that changed since is followed). The signer
    relays the profile was paired through are passed as the legacy set:
    MyEditor kept private records there before it read relay lists, so
    they keep their room in the set and those records stay readable.
    Reading also asks the fallback relays, where records written before
    the account's own list was known went (policy.private_relays).
    """
    directory.private_relays(profile.user_pubkey, on_done,
                             entitled=policy.relays_from(entitled),
                             legacy=list(getattr(profile, "bunker_relays", None) or ()),
                             reading=reading)
