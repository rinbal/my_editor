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
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

import user_dirs
from atomic_file import write_text

from .. import events
from . import defaults, policy
from .lookup import Lookup, classify, fetch_replaceable, fetch_replaceable_many
from .policy import KIND_RELAY_LIST, LookupState, RelayList

def relay_lists_file() -> Path:
    """nostr_relay_lists.json in the person's config folder, found each time
    (user_dirs)."""
    return user_dirs.config_dir() / "nostr_relay_lists.json"


# ``store_path`` not given: relay_lists_file(), found when the directory is
# made (None keeps nothing on disk).
_PERSONS_FILE = object()

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
                 store_path: Any = _PERSONS_FILE,
                 own_pubkeys: Callable[[], Iterable[str]] = tuple,
                 clock: Callable[[], float] = time.monotonic,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._query = query
        self._query_many = query_many
        if store_path is _PERSONS_FILE:
            store_path = relay_lists_file()
        self._store_path = Path(store_path) if store_path else None
        self._own_pubkeys = own_pubkeys
        self._clock = clock
        self._entries: "OrderedDict[str, RelayList]" = OrderedDict()
        self._pending: Dict[str, _Pending] = {}
        # author -> {relay: id of the relay list sent there}, so a list is
        # shared with a relay once, not with every note that reaches it.
        self._shared: Dict[str, Dict[str, str]] = {}
        # author -> the relays the account keeps its drafts on (NIP-37 kind
        # 10013, decrypted; empty: it has no such list). Absent: not known.
        self._draft_lists: Dict[str, List[str]] = {}
        # author -> {relay: since}: relays the account's drafts were routed
        # to before, that its draft relays no longer read but may still hold
        # drafts saved there since then (None: at any time). They are read,
        # each until its drafts are moved, however long that takes: nothing
        # is given up without the person knowing (leave_drafts_behind).
        self._drafts_left: Dict[str, Dict[str, Optional[int]]] = {}
        # author -> relays being chosen for its drafts right now (the Draft
        # Relays sheet moving drafts there): the account's own while so.
        self._draft_relays_chosen: Dict[str, List[str]] = {}
        # authors whose every draft relay asks for a sign-in before it shows
        # drafts and did not take it (NIP-42; the signer app declined, say):
        # drafts saved there could be read back by nobody, so they go to the
        # usual relays.
        self._draft_relays_locked: set = set()
        # author -> the newest draft list this device wrote or followed in
        # this session: id, created_at, relays (see ``follow_draft_relays``).
        self._draft_list_events: Dict[str, dict] = {}
        # author -> what waits for the draft list being read right now, and
        # until when it waits. One timer serves every wait: a timer per wait,
        # connected to a closure over this object, could outlive or destroy it.
        self._draft_waiting: Dict[str, List[Callable[[], None]]] = {}
        self._draft_deadlines: Dict[str, float] = {}
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.timeout.connect(self._draft_waits_ran_out)
        self._load()

    # ------------------------------------------------------------------ #
    # Knowing                                                            #
    # ------------------------------------------------------------------ #

    def cached(self, pubkey: str) -> RelayList:
        """What is known now, however old; UNKNOWN when nothing is."""
        entry = self._entries.get((pubkey or "").lower())
        return _copy(entry) if entry is not None else RelayList()

    def relay_hint(self, pubkey: str) -> str:
        """Where ``pubkey`` can be found, as far as is known now: the first
        of their write relays; "" when none is known."""
        written = self.cached(pubkey).write
        return written[0] if written else ""

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

    # -- drafts ------------------------------------------------------------

    def expect_draft_relays(self, author: str, *, timeout_ms: int = 15_000) -> None:
        """The account's draft list is being read: ``draft_relays`` waits
        for it (at most ``timeout_ms``), so no draft is saved where the
        person did not want it in the moments after starting up."""
        key = (author or "").lower()
        if key in self._draft_waiting:
            return
        self._draft_waiting[key] = []
        self._draft_deadlines[key] = self._clock() + timeout_ms / 1000
        self._arm_draft_timer()

    def set_draft_relays(self, author: str, relays: Optional[Sequence[str]]) -> None:
        """What reading the account's draft list found: its relays, an
        empty list for none, or None when it could not be read. None keeps
        what was known before, so an unreachable signer never moves drafts
        to relays the person did not choose."""
        key = (author or "").lower()
        if relays is not None:
            chosen = policy.dedupe_relays(relays, cap=defaults.PRIVATE_CAP)
            if self._draft_lists.get(key) != chosen:
                self._draft_lists[key] = chosen
                if self._is_own(key):
                    self._save()
                self.changed.emit(key)
        self._settle_drafts(key)

    def draft_list_known(self, author: str) -> Optional[dict]:
        """The newest draft list this device wrote or followed in this
        session (``id``, ``created_at`` and the decrypted ``relays``), or
        None."""
        known = self._draft_list_events.get((author or "").lower())
        return dict(known) if known is not None else None

    def remember_draft_list(self, author: str, event: dict, relays: Sequence[str]) -> None:
        """The draft list this device has just written: it is the newest
        known, and a read answered with an older one is not followed."""
        self._draft_list_events[(author or "").lower()] = {
            "id": event.get("id"), "created_at": event.get("created_at"),
            "relays": list(relays)}

    def follow_draft_relays(self, author: str, relays: Optional[Sequence[str]], *,
                            event: Optional[dict] = None) -> bool:
        """A draft list read now (``event``: the list's event, None when
        the account has none), perhaps changed on another device: drafts
        follow it (``set_draft_relays``), and the draft relays it no longer
        has are left behind with whatever this device saved there before it
        knew (``leave_drafts_behind``).

        A list older than the newest known here, or none while one is
        known, is an answer that arrived late (a read asked before a change
        reached the relays): it is not followed, and False is returned."""
        key = (author or "").lower()
        known = self._draft_list_events.get(key)
        if known is not None and (event is None or (
                event.get("id") != known["id"] and not policy.is_newer(event, known))):
            return False
        if event is not None and relays is not None:
            self._draft_list_events[key] = {"id": event.get("id"),
                                            "created_at": event.get("created_at"),
                                            "relays": list(relays)}
        before = self._draft_lists.get(key)
        if relays is not None and before is not None:
            now = policy.dedupe_relays(relays, cap=defaults.PRIVATE_CAP)
            left = [url for url in before if url not in now]
            if left:
                self.leave_drafts_behind(key, left, since=None)
        self.set_draft_relays(key, relays)
        return True

    def leave_drafts_behind(self, author: str, relays: Sequence[str], *,
                            since: Optional[int]) -> None:
        """``relays`` are no longer draft relays but may hold drafts saved
        ``since`` then (None: at any time): each is read with the draft
        relays until ``stop_reading_drafts_on`` (its drafts moved, or the person let
        them go). A relay
        left again keeps the earlier of its two dates."""
        key = (author or "").lower()
        left = self._drafts_left.setdefault(key, {})
        for url in policy.dedupe_relays(relays):
            if url in left:
                earlier = left[url]
                left[url] = None if earlier is None or since is None else min(earlier, since)
            else:
                left[url] = since
        self._drafts_left_changed(key)

    def drafts_left_behind(self, author: str) -> Dict[str, Optional[int]]:
        """Relays the account's drafts may still be left on, each with the
        time from which drafts there are to be moved (None: all of them)."""
        return dict(self._drafts_left.get((author or "").lower(), {}))

    def stop_reading_drafts_on(self, author: str, relays: Sequence[str]) -> None:
        """No longer read ``relays`` for drafts left behind: their drafts are
        where drafts go now, or the person chose to let them go."""
        key = (author or "").lower()
        left = self._drafts_left.get(key)
        if not left:
            return
        for url in relays:
            left.pop(url, None)
        if not left:
            del self._drafts_left[key]
        self._drafts_left_changed(key)

    def _drafts_left_changed(self, key: str) -> None:
        if self._is_own(key):
            self._save()
        self.changed.emit(key)            # what drafts are read from changed

    def own_relays(self, author: str) -> List[str]:
        """Every relay the account itself chose: its relay list, its draft
        relays, those it still reads drafts left behind on, and those being
        chosen for its drafts right now. Signing in to a relay that asks
        (nostr/relay_auth.py) is offered to these only."""
        key = (author or "").lower()
        mine = self.cached(key)
        return policy.dedupe_relays(
            list(mine.write) + list(mine.read) + list(self._draft_lists.get(key, ()))
            + list(self._drafts_left.get(key, {})) + list(self._draft_relays_chosen.get(key, ())))

    def choosing_draft_relays(self, author: str, relays: Sequence[str]) -> None:
        """The relays being chosen for the account's drafts right now
        (empty: none any more): they count as its own (``own_relays``)
        while its drafts are moved there and read back."""
        key = (author or "").lower()
        if relays:
            self._draft_relays_chosen[key] = list(relays)
        else:
            self._draft_relays_chosen.pop(key, None)

    def set_draft_relays_locked(self, author: str, locked: bool) -> None:
        """Whether every draft relay of the account asked for a sign-in
        and did not take it when its drafts were last read: while so,
        drafts are saved on the usual relays (they are still read from the
        draft relays too)."""
        key = (author or "").lower()
        if locked != (key in self._draft_relays_locked):
            (self._draft_relays_locked.add if locked else self._draft_relays_locked.discard)(key)
            self.changed.emit(key)

    def draft_relays_of(self, author: str) -> Optional[List[str]]:
        """The account's draft relays as known now, [] for none, None when
        not known."""
        known = self._draft_lists.get((author or "").lower())
        return list(known) if known is not None else None

    def draft_relays(self, author: str, on_done: Callable[[List[str]], None], *,
                     entitled: Sequence[str] = (), legacy: Sequence[str] = (),
                     reading: bool = False,
                     chosen: Optional[Sequence[str]] = None) -> None:
        """Where the author's drafts are written, or with ``reading`` read
        from (policy.draft_relays). ``chosen`` asks the same of another
        list of the account's draft relays than the one known ([] for
        none): where drafts would go with it, before it is made the one.
        Such a question does not wait for the account's list to be read."""
        key = (author or "").lower()
        if chosen is None and key in self._draft_waiting:
            self._draft_waiting[key].append(lambda: self.draft_relays(
                key, on_done, entitled=entitled, legacy=legacy, reading=reading))
            return
        mine = self._draft_lists.get(key, ()) if chosen is None else list(chosen)
        if chosen is None and not reading and key in self._draft_relays_locked:
            mine = ()
        left = list(self.drafts_left_behind(key)) if reading and chosen is None else []
        self.lookup(key, lambda relay_list: on_done(policy.dedupe_relays(
            policy.draft_relays(relay_list, mine, entitled=entitled, legacy=legacy,
                                reading=reading),
            left)))

    def _settle_drafts(self, key: str) -> None:
        self._draft_deadlines.pop(key, None)
        waiting = self._draft_waiting.pop(key, [])
        self._arm_draft_timer()
        for resume in waiting:
            resume()

    def _draft_waits_ran_out(self) -> None:
        now = self._clock()
        for key in [k for k, until in self._draft_deadlines.items() if until <= now]:
            self._settle_drafts(key)
        self._arm_draft_timer()

    def _arm_draft_timer(self) -> None:
        if not self._draft_deadlines:
            self._draft_timer.stop()
            return
        wait = max(0.0, min(self._draft_deadlines.values()) - self._clock())
        self._draft_timer.start(int(wait * 1000))

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
        drafts = data.get("drafts") if isinstance(data, dict) else None
        for author, relays in (drafts.items() if isinstance(drafts, dict) else ()):
            if isinstance(author, str) and isinstance(relays, list):
                self._draft_lists[author.lower()] = policy.dedupe_relays(
                    [r for r in relays if isinstance(r, str)], cap=defaults.PRIVATE_CAP)
        left = data.get("drafts_left") if isinstance(data, dict) else None
        for author, relays in (left.items() if isinstance(left, dict) else ()):
            if not isinstance(author, str) or not isinstance(relays, dict):
                continue
            kept = {url: since for url, since in relays.items()
                    if isinstance(url, str) and policy.normalize_relay_url(url) == url
                    and (since is None or (isinstance(since, int)
                                           and not isinstance(since, bool)))}
            if kept:
                self._drafts_left[author.lower()] = kept

    def _save(self) -> None:
        if self._store_path is None:
            return
        own = self._own_keys()
        lists = {k: e.event for k, e in self._entries.items()
                 if k in own and e.found and e.event is not None}
        drafts = {k: v for k, v in self._draft_lists.items() if k in own}
        left = {k: dict(v) for k, v in self._drafts_left.items() if k in own}
        try:
            write_text(self._store_path, json.dumps(
                {"version": 1, "lists": lists, "drafts": drafts, "drafts_left": left}))
        except (OSError, TypeError, ValueError) as exc:
            # The lists are still known for this session; only the head
            # start on the next launch is lost.
            logger.warning("could not save the relay lists: %s", exc)


def _copy(relay_list: RelayList) -> RelayList:
    """A RelayList nobody else holds, event and all."""
    return copy.deepcopy(relay_list)


def ask_draft_relays(directory, profile, on_done: Callable[[List[str]], None], *,
                     entitled=(), reading: bool = False,
                     chosen: Optional[Sequence[str]] = None) -> None:
    """Where ``profile``'s drafts are written, or with ``reading`` read
    from: the account's own draft relays when it has chosen some (NIP-37),
    otherwise wherever its private records live (``ask_private_relays``).
    ``chosen``: the same for another list than the one known
    (RelayDirectory.draft_relays)."""
    directory.draft_relays(profile.user_pubkey, on_done,
                           entitled=policy.relays_from(entitled),
                           legacy=list(getattr(profile, "bunker_relays", None) or ()),
                           reading=reading, chosen=chosen)


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
