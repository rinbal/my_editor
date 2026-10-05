# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add one relay to the user's published relay list, and change nothing else.

The membership window offers this for the EINUNDZWANZIG members' relay.
The safe read-modify-write itself (read the current list fresh, never
build one from nothing, check what the signer returns, require two
relays, read it back) lives in nostr/outbox/writer.py; this module only
turns its outcome into the ones the window shows:

    added        the new list reached the relays
    already      the relay is already listed (under any marker); nothing sent
    no_list      the account has no relay list; nothing was published
    unreadable   the current list couldn't be read; nothing was published
    failed       the signer said no, or too few relays took the new list

"No list" and "couldn't read it" are different situations and are told
apart. An account with no list can be offered MyEditor's recommended
one with the relay on it (RecommendedRelayList), but only when the
person asks for it; a list that merely could not be read must never be
replaced by one built from nothing, so that case only says to try again
later. Publishing the recommended list adds two more outcomes:

    published    the recommended list, with the relay, reached the relays
    list_exists  a list turned up after all; nothing was published
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from nostr.outbox import writer as outbox_writer
from nostr.outbox.lookup import fetch_replaceable

ADDED = "added"
ALREADY = "already"
NO_LIST = "no_list"
UNREADABLE = "unreadable"
FAILED = "failed"
PUBLISHED = "published"
LIST_EXISTS = "list_exists"

# Outcomes after which there is nothing left to do.
DONE = frozenset({ADDED, ALREADY, PUBLISHED})

_MESSAGES = {
    ADDED: "Added to your relay list.",
    ALREADY: "It’s already on your relay list.",
    NO_LIST: ("You don’t have a relay list yet, so nothing was changed. MyEditor "
              "can publish a recommended one with the members’ relay on it."),
    UNREADABLE: ("MyEditor couldn’t read your relay list right now, so nothing was "
                 "changed. Try again later."),
    FAILED: "Your relay list wasn’t changed. Try again in a moment.",
    PUBLISHED: "Your relay list is published, with the members’ relay on it.",
    LIST_EXISTS: ("Your account has a relay list after all, so nothing was "
                  "published. Add the members’ relay to it instead."),
}

_FROM_ADDING = {
    outbox_writer.WRITTEN: ADDED,
    outbox_writer.UNCHANGED: ALREADY,
    outbox_writer.REFUSED: NO_LIST,
    outbox_writer.UNKNOWN_BASE: UNREADABLE,
    outbox_writer.FAILED: FAILED,
}

_FROM_CREATING = {
    outbox_writer.WRITTEN: PUBLISHED,
    outbox_writer.EXISTS: LIST_EXISTS,
    outbox_writer.UNKNOWN_BASE: UNREADABLE,
    outbox_writer.FAILED: FAILED,
}


def outcome_message(outcome: str) -> str:
    """Plain words for an outcome, for the window to show."""
    return _MESSAGES.get(outcome, _MESSAGES[FAILED])


class RelayListAddition(QObject):
    """One attempt to add ``relay_url`` to ``profile``'s relay list."""

    finished = Signal(str)   # ADDED, ALREADY, NO_LIST, UNREADABLE or FAILED; once

    def __init__(self, pool, session_pool, profile, relay_url: str, *, directory,
                 query=fetch_replaceable, clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._writer = outbox_writer.add_relay(
            url=relay_url, pool=pool, directory=directory, session_pool=session_pool,
            profile=profile, query=query, clock=clock, parent=self)
        self._writer.finished.connect(
            lambda outcome: self.finished.emit(_FROM_ADDING.get(outcome.status, FAILED)))

    def start(self) -> None:
        self._writer.start()


class RecommendedRelayList(QObject):
    """Publish MyEditor's recommended relay list with ``relay_url`` on it,
    for an account that has none.

    Only ever started by an explicit click. The writer reads the current
    list first and publishes nothing when one exists or cannot be read,
    so a real list is never replaced by this one.
    """

    finished = Signal(str)   # PUBLISHED, LIST_EXISTS, UNREADABLE or FAILED; once

    def __init__(self, pool, session_pool, profile, relay_url: str, *, directory,
                 query=fetch_replaceable, clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._writer = outbox_writer.create_relay_list(
            extra_write=[relay_url], pool=pool, directory=directory,
            session_pool=session_pool, profile=profile, query=query, clock=clock,
            parent=self)
        self._writer.finished.connect(
            lambda outcome: self.finished.emit(_FROM_CREATING.get(outcome.status, FAILED)))

    def start(self) -> None:
        self._writer.start()
