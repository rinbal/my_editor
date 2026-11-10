# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Show a Nostr address (NIP-05) on the user's profile, and change nothing else.

The membership window offers this once a member has chosen their
EINUNDZWANZIG address, and again whenever the profile does not show it,
so a person who said Not Now can change their mind later. It only ever
runs after a click.

The safe read-modify-write (read the profile fresh, keep every other
field, check what the signer returns, require two relays) lives in
nostr/outbox/writer.py; this module turns its outcome into the ones the
window shows:

    shown        the profile with the address reached the relays
    already      the profile already shows this address; nothing sent
    no_profile   no profile was found; nothing was published
    unreadable   the profile couldn't be read; nothing was published
    failed       the signer said no, or too few relays took it

A profile that could not be found is not created from nothing here: a
profile holding only an address would replace the person's name and
picture in every app, should the relays asked simply have missed it.

A profile not changed is said by that and the one sentence of its cause
(outbox/writer.failure_words, lessons 36).
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from i18n import N_, _
from nostr import failures
from nostr.outbox import writer as outbox_writer
from nostr.outbox.lookup import fetch_replaceable

SHOWN = "shown"
ALREADY = "already"
NO_PROFILE = "no_profile"
UNREADABLE = "unreadable"
FAILED = "failed"

# Outcomes after which there is nothing left to do.
DONE = frozenset({SHOWN, ALREADY})

# One sentence each; a profile not changed says its cause.
_MESSAGES = {
    SHOWN: N_("Your profile shows it now."),
    ALREADY: N_("Your profile already shows it."),
    NO_PROFILE: N_("MyEditor couldn’t find your profile: set it up in Edit Profile first, "
                   "then try again."),
}
NOT_CHANGED = N_("Your profile wasn’t changed")
# The window spoke for an account that is no longer the active one.
NOT_ACTIVE = N_("This window is for an account that isn’t active any more: close it and "
                "open it again.")

_FROM_WRITER = {
    outbox_writer.WRITTEN: SHOWN,
    outbox_writer.UNCHANGED: ALREADY,
    outbox_writer.REFUSED: NO_PROFILE,
    outbox_writer.UNKNOWN_BASE: UNREADABLE,
    outbox_writer.FAILED: FAILED,
}


def outcome_message(outcome: str, write: Optional[outbox_writer.WriteOutcome] = None) -> str:
    """Plain words for an outcome, for the window to show: for a profile
    not changed, that and the one sentence of ``write``'s cause."""
    if outcome in _MESSAGES:
        return _(_MESSAGES[outcome])
    why = (outbox_writer.failure_words(write) if write is not None
           else _(failures.CHECK_CONNECTION))
    return failures.spoken(_(NOT_CHANGED), why)


def inactive_message() -> str:
    """For a window that spoke for an account no longer active."""
    return failures.spoken(_(NOT_CHANGED), _(NOT_ACTIVE))


class ProfileAddress(QObject):
    """One attempt to show ``address`` as the Nostr address on ``profile``."""

    finished = Signal(str)   # SHOWN, ALREADY, NO_PROFILE, UNREADABLE or FAILED; once

    def __init__(self, pool, session_pool, profile, address: str, *, directory,
                 query=fetch_replaceable, clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # The writer's own outcome, once it finished (its cause, for words).
        self.write: Optional[outbox_writer.WriteOutcome] = None
        self._writer = outbox_writer.update_profile(
            changes={"nip05": address}, on_absent="refuse", pool=pool,
            directory=directory, session_pool=session_pool, profile=profile,
            query=query, clock=clock, parent=self)
        self._writer.finished.connect(self._finished)

    def _finished(self, outcome: outbox_writer.WriteOutcome) -> None:
        self.write = outcome
        self.finished.emit(_FROM_WRITER.get(outcome.status, FAILED))

    def start(self) -> None:
        self._writer.start()
