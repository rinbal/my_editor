# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fakes for the nostr/outbox tests: relays, a signer, and lookups, plus
a relay directory for the code that routes through one.

Signatures are real (throwaway keys), because every rule under test
starts with "only a validly signed event counts".
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from nostr import crypto, events
from nostr.outbox import defaults, policy
from nostr.outbox.lookup import Lookup
from nostr.outbox.policy import LookupState, RelayList

SK = bytes.fromhex("2a" * 32)
PK = crypto.get_public_key(SK).hex()
OTHER_SK = bytes.fromhex("3b" * 32)
OTHER_PK = crypto.get_public_key(OTHER_SK).hex()
NOW = 1_800_000_000


def signed(kind: int, tags=None, content: str = "", *, sk: bytes = SK,
           created_at: int = NOW - 100) -> dict:
    return events.sign_event({"kind": kind, "content": content, "tags": tags or [],
                              "created_at": created_at}, sk)


def settle(rounds: int = 6) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


class Profile:
    def __init__(self, pubkey: str = PK):
        self.user_pubkey = pubkey
        self.bunker_relays = []


class FakeClient:
    def __init__(self, *, sk: bytes = SK, fail: Optional[str] = None, tamper=None):
        self.sk = sk
        self.fail = fail
        self.tamper = tamper
        self.requests: List[dict] = []

    def sign_event(self, unsigned, on_success, on_failure, **_kw):
        self.requests.append(dict(unsigned))
        if self.fail:
            on_failure(self.fail)
            return
        event = events.sign_event(dict(unsigned), self.sk)
        if self.tamper:
            event = self.tamper(event)
        on_success(event)


class FakeSessionPool:
    def __init__(self, client: FakeClient):
        self.client = client

    def get(self, profile, on_ready, on_error):
        on_ready(self.client)


class FakeJob(QObject):
    first_accept = Signal(str)
    all_done = Signal(list)


class FakeSubscription(QObject):
    """A subscription a test drives by hand: ``answer``, ``refuse`` and
    ``fail`` play one relay's part, as the real Subscription reports it."""

    event = Signal(dict)
    eose = Signal()
    closed = Signal(str)
    relay_eose = Signal(str)
    relay_closed = Signal(str, str)
    relay_failed = Signal(str, str)

    def __init__(self, urls=(), filters=()):
        super().__init__()
        self.urls = list(urls)
        self.filters = list(filters)
        self.is_closed = False
        self._ended: set = set()

    def close(self):
        self.is_closed = True

    def answer(self, url, *events_):
        """``url`` sends ``events_`` and ends its stored events."""
        for event in events_:
            self.event.emit(event)
        self._end(url)
        self.relay_eose.emit(url)
        if self._ended >= set(self.urls):
            self.eose.emit()

    def refuse(self, url, reason="blocked: not today"):
        self._end(url)
        self.relay_closed.emit(url, reason)
        self.closed.emit(reason)

    def fail(self, url, reason="socket error: host not found"):
        self._end(url)
        self.relay_failed.emit(url, reason)

    def _end(self, url):
        self._ended.add(url)


class FakePool:
    """Relays that accept (or refuse) publishes and remember what they kept."""

    def __init__(self, *, refuse=(), keep=True):
        self.refuse = set(refuse)
        self.keep = keep
        self.published: List[tuple] = []
        self.stored: Dict[str, dict] = {}
        self.subscriptions: List[tuple] = []

    def publish(self, urls, event):
        self.published.append((list(urls), event))
        results = [(u, u not in self.refuse, "" if u not in self.refuse else "blocked")
                   for u in urls]
        if self.keep and any(ok for _u, ok, _m in results):
            self.stored[event["id"]] = event
        job = FakeJob()
        QTimer.singleShot(0, lambda: job.all_done.emit(results))
        return job

    def subscribe(self, urls, filters):
        sub = FakeSubscription(urls, filters)
        self.subscriptions.append((list(urls), filters))

        def answer():
            for flt in filters:
                for event_id in flt.get("ids", []):
                    if event_id in self.stored:
                        sub.event.emit(self.stored[event_id])
            sub.eose.emit()
        QTimer.singleShot(0, answer)
        return sub


class FakeQuery:
    """Stands in for lookup.fetch_replaceable (and, as ``many``, for
    fetch_replaceable_many): answers from a table by kind."""

    def __init__(self, answers: Optional[Dict[int, Lookup]] = None):
        self.answers = answers or {}
        self.calls: List[dict] = []

    def __call__(self, pool, relays, *, kind, author, on_done, timeout_ms=6000, parent=None):
        self.calls.append({"relays": list(relays), "kind": kind, "author": author})
        on_done(self.answers.get(kind, Lookup(LookupState.UNKNOWN)))

    def many(self, pool, relays, *, kind, authors, on_done, timeout_ms=6000, parent=None):
        self.calls.append({"relays": list(relays), "kind": kind, "authors": list(authors)})
        answer = self.answers.get(kind, Lookup(LookupState.UNKNOWN))
        on_done({author: answer for author in authors})


def one_by_one(query):
    """A many-author lookup made of a one-author fake, for tests that
    answer each person differently: each author is asked alone."""
    def many(pool, relays, *, kind, authors, on_done, timeout_ms=6000, parent=None):
        results: Dict[str, Lookup] = {}
        keys = list(dict.fromkeys(a.lower() for a in authors))

        def one(author):
            def done(result):
                results[author] = result
                if len(results) == len(keys):
                    on_done(dict(results))
            return done

        for author in keys:
            query(pool, relays, kind=kind, author=author, on_done=one(author),
                  timeout_ms=timeout_ms, parent=parent)
    return many


class FakeRelayDirectory(QObject):
    """RelayDirectory's routing surface, answering from known lists.

    Routing runs through the real policy functions, so a test sees where
    the app would really go with those lists. ``lists`` maps a pubkey to
    its RelayList (or to relay URLs, read as a list with no markers);
    anyone else is UNKNOWN. ``calls`` records each question asked.

    Like the real directory, every answer arrives on the next turn of the
    event loop (QTimer 0), never inside the call, so code that assumes
    otherwise fails here too; ``settle()`` delivers. Its public methods
    take what RelayDirectory's take (a test compares the two).
    """

    changed = Signal(str)

    def __init__(self, lists: Optional[dict] = None):
        super().__init__()
        self.lists: Dict[str, RelayList] = {}
        for pubkey, value in (lists or {}).items():
            self.set(pubkey, value)
        self.calls: List[tuple] = []
        self.shared: List[tuple] = []
        self.remembered: List[dict] = []

    def set(self, pubkey: str, value) -> None:
        if not isinstance(value, RelayList):
            value = RelayList(write=list(value), read=list(value), state=LookupState.FOUND)
        self.lists[pubkey.lower()] = value

    def cached(self, pubkey: str) -> RelayList:
        return copy.deepcopy(self.lists.get((pubkey or "").lower()) or RelayList())

    @staticmethod
    def _later(on_done, answer) -> None:
        QTimer.singleShot(0, lambda: on_done(answer))

    def lookup(self, pubkey, on_done, *, hints=(), fresh=False, timeout_ms=6_000):
        self.calls.append(("lookup", pubkey, tuple(hints)))
        self._later(on_done, self.cached(pubkey))

    def lookup_many(self, pubkeys, on_done, *, hints=None, timeout_ms=3_000):
        keys = list(dict.fromkeys(p.lower() for p in pubkeys if p))
        keys = keys[:defaults.MENTION_LOOKUP_CAP]
        self.calls.append(("lookup_many", tuple(keys), dict(hints or {})))
        self._later(on_done, {key: self.cached(key) for key in keys})

    def remember(self, event):
        self.remembered.append(event)
        return False

    def publish_plan(self, author, on_done, *, mentioned=(), entitled=()):
        mentioned = list(mentioned)
        self.calls.append(("publish_plan", author, tuple(mentioned), tuple(entitled)))
        looked_up = list(dict.fromkeys(p.lower() for p, _hint in mentioned))
        looked_up = looked_up[:defaults.MENTION_LOOKUP_CAP]
        self._later(on_done, policy.plan_publish(
            self.cached(author),
            mentioned={p: self.cached(p) for p in looked_up},
            hints={p.lower(): h for p, h in mentioned if h},
            entitled=entitled,
            own={author.lower()}))

    def private_relays(self, author, on_done, *, entitled=(), legacy=(), reading=False):
        self.calls.append(("private_relays", author, tuple(entitled), tuple(legacy), reading))
        self._later(on_done, policy.private_relays(self.cached(author), entitled=entitled,
                                                   legacy=legacy, reading=reading))

    def outbox_of(self, author, on_done, *, hints=()):
        self.calls.append(("outbox_of", author, tuple(hints)))
        self._later(on_done, policy.outbox_relays(self.cached(author), hints=hints))

    def share_relay_list(self, author, relays):
        self.shared.append((author, list(relays)))

    def asked(self, name: str) -> List[tuple]:
        return [call for call in self.calls if call[0] == name]


class HandPool:
    """A pool whose subscriptions answer only when a test drives them."""

    def __init__(self):
        self.subs: List[FakeSubscription] = []

    def subscribe(self, urls, filters):
        sub = FakeSubscription(urls, filters)
        self.subs.append(sub)
        return sub


def found(event: dict) -> Lookup:
    return Lookup(LookupState.FOUND, event=event, answered=("wss://a",))


ABSENT = Lookup(LookupState.ABSENT, answered=("wss://purplepag.es", "wss://nos.lol"))
UNKNOWN = Lookup(LookupState.UNKNOWN)
