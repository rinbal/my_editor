# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Relays a test fills with events, answering every subscription the way
relays do (kinds, authors, tags, ``until``, ``limit`` newest first), and
taking or refusing what is published to them. For the published list
(nostr/published.py) and its view."""

from __future__ import annotations

from typing import Dict, Iterable, List, Set

import shiboken6

from tests.outbox_fakes import FakePool, HandPool, settle


def matches(event: dict, flt: dict) -> bool:
    if "kinds" in flt and event.get("kind") not in flt["kinds"]:
        return False
    if "authors" in flt and event.get("pubkey") not in flt["authors"]:
        return False
    if "ids" in flt and event.get("id") not in flt["ids"]:
        return False
    if "until" in flt and event.get("created_at", 0) > flt["until"]:
        return False
    if "since" in flt and event.get("created_at", 0) < flt["since"]:
        return False
    for key, values in flt.items():
        if key.startswith("#"):
            name = key[1:]
            if not any(len(tag) >= 2 and tag[0] == name and tag[1] in values
                       for tag in event.get("tags", [])):
                return False
    return True


def answer(stored: Iterable[dict], filters: List[dict]) -> List[dict]:
    """What a relay holding ``stored`` sends for ``filters``."""
    out: Dict[str, dict] = {}
    for flt in filters:
        found = sorted((e for e in stored if matches(e, flt)),
                       key=lambda e: (-e["created_at"], e["id"]))
        if "limit" in flt:
            found = found[:flt["limit"]]
        for event in found:
            out.setdefault(event["id"], event)
    return list(out.values())


class Relays(HandPool):
    """A pool of relays: ``keep`` stores events on them, ``down`` relays
    cannot be reached, ``unreachable_for`` maps a relay to the kinds it
    cannot be reached for, ``refusing_kinds`` closes requests that ask
    for those kinds, ``ignoring_until`` relays answer as if no ``until``
    was asked, ``write_only`` relays take events but close every request
    asking for them sign in first, ``keeps_nothing`` relays answer OK and
    keep nothing, ``silent`` relays never end a request (packets dropped),
    and ``run`` lets every request that is open be
    answered (and whatever it leads to) until nothing is left."""

    def __init__(self, *, refuse_publish: Iterable[str] = ()) -> None:
        super().__init__()
        self.stored: Dict[str, List[dict]] = {}
        self.down: Set[str] = set()
        self.unreachable_for: Dict[str, Set[int]] = {}
        self.refusing_kinds: Set[int] = set()
        self.ignoring_until: Set[str] = set()
        self.write_only: Set[str] = set()
        self.keeps_nothing: Set[str] = set()
        self.silent: Set[str] = set()
        self.publisher = FakePool(refuse=refuse_publish)
        self._answered = 0

    @property
    def published(self) -> list:
        return self.publisher.published

    def keep(self, relay: str, *events: dict) -> None:
        self.stored.setdefault(relay, []).extend(events)

    def publish(self, urls, event):
        # A relay that takes an event gives it back when asked, as relays
        # do (``write_only`` relays take it and refuse every read).
        for url in urls:
            if (url not in self.publisher.refuse and url not in self.keeps_nothing
                    and event not in self.stored.get(url, [])):
                self.keep(url, event)
        return self.publisher.publish(urls, event)

    def run(self, rounds: int = 12) -> None:
        for _ in range(rounds):
            settle()
            while self._answered < len(self.subs):
                sub = self.subs[self._answered]
                self._answered += 1
                if not shiboken6.isValid(sub) or sub.is_closed:
                    continue                    # closed or gone: nobody asks any more
                kinds = {k for flt in sub.filters for k in flt.get("kinds", [])}
                for url in sub.urls:
                    if url in self.silent:
                        continue
                    if url in self.down or kinds & self.unreachable_for.get(url, set()):
                        sub.fail(url)
                    elif url in self.write_only:
                        sub.refuse(url, "auth-required: sign in to read")
                    elif kinds & self.refusing_kinds:
                        sub.refuse(url, "blocked: not here")
                    else:
                        filters = sub.filters if url not in self.ignoring_until else [
                            {k: v for k, v in flt.items() if k != "until"}
                            for flt in sub.filters]
                        sub.answer(url, *answer(self.stored.get(url, []), filters))
        settle()
