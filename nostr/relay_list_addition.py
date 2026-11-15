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

A list not changed or not published is said by what did not happen and
the one sentence of its cause (outbox/writer.failure_words, lessons 36).
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from i18n import N_, _
from nostr import failures
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

# One sentence each; a list not changed or not published says its cause.
_MESSAGES = {
    ADDED: N_("Added to your relay list."),
    ALREADY: N_("It’s already on your relay list."),
    NO_LIST: N_("You don’t have a relay list yet: MyEditor can publish a recommended one "
                "with the members’ relay on it."),
    PUBLISHED: N_("Your relay list is published, with the members’ relay on it."),
    LIST_EXISTS: N_("Your account has a relay list after all: add the members’ relay to it "
                    "instead."),
}
# What did not happen, by the job that did not do it.
NOT_CHANGED = N_("Your relay list wasn’t changed")
NOT_PUBLISHED = N_("Your relay list wasn’t published")

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


def outcome_message(outcome: str, write: Optional[outbox_writer.WriteOutcome] = None, *,
                    not_done: str = NOT_CHANGED) -> str:
    """Plain words for an outcome, for the window to show: for a list not
    changed (``not_done``), that and the one sentence of ``write``'s
    cause; never a relay's name or a count."""
    if outcome in _MESSAGES:
        return _(_MESSAGES[outcome])
    why = (outbox_writer.failure_words(write) if write is not None
           else _(failures.CHECK_CONNECTION))
    return failures.spoken(_(not_done), why)


class RelayListAddition(QObject):
    """One attempt to add ``relay_url`` to ``profile``'s relay list."""

    finished = Signal(str)   # ADDED, ALREADY, NO_LIST, UNREADABLE or FAILED; once
    NOT_DONE = NOT_CHANGED

    def __init__(self, pool, session_pool, profile, relay_url: str, *, directory,
                 query=fetch_replaceable, clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # The writer's own outcome, once it finished (its cause, for words).
        self.write: Optional[outbox_writer.WriteOutcome] = None
        self._writer = outbox_writer.add_relay(
            url=relay_url, pool=pool, directory=directory, session_pool=session_pool,
            profile=profile, query=query, clock=clock, parent=self)
        self._writer.finished.connect(self._finished)

    def _finished(self, outcome: outbox_writer.WriteOutcome) -> None:
        self.write = outcome
        self.finished.emit(_FROM_ADDING.get(outcome.status, FAILED))

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
    NOT_DONE = NOT_PUBLISHED

    def __init__(self, pool, session_pool, profile, relay_url: str, *, directory,
                 query=fetch_replaceable, clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.write: Optional[outbox_writer.WriteOutcome] = None
        self._writer = outbox_writer.create_relay_list(
            extra_write=[relay_url], pool=pool, directory=directory,
            session_pool=session_pool, profile=profile, query=query, clock=clock,
            parent=self)
        self._writer.finished.connect(self._finished)

    def _finished(self, outcome: outbox_writer.WriteOutcome) -> None:
        self.write = outcome
        self.finished.emit(_FROM_CREATING.get(outcome.status, FAILED))

    def start(self) -> None:
        self._writer.start()
