# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How every Nostr screen says that something failed (lessons 36): one
helper for all of them, so none says it its own way.

Partial failure is silent. Some relays, servers or pages of a list
failing is normal: what still can work is tried again quietly, with
backoff (:func:`retry_quietly`), and nothing is said meanwhile, never a
relay's name, a count of them, a code, or a message per relay or try.

Total failure speaks once: a short title that says what did not happen,
one sentence with the likely cause and what to do (the sentences below
are the common ones), one action (Try Again) and Cancel; a label shows
the two with :func:`markup`. Technical details only behind Details or
the help tooltip. Pure: no Qt.
"""

from __future__ import annotations

import html
from typing import Callable, Sequence, TypeVar

from i18n import N_

T = TypeVar("T")

# The quiet tries after the first: soon, then less often. Three, so a
# total failure is said within half a minute.
QUIET_RETRY_MS = (2_000, 6_000, 15_000)

# The likely cause and what to do, one sentence each. Nothing that was
# never reached is blamed on a server, nor a server's answer on the
# connection.
CHECK_CONNECTION = N_("Check your internet connection and try again.")
# A server that answered with trouble of its own (a 5xx, an answer that
# could not be read).
SERVER_TROUBLE = N_("The server couldn’t take it just now: try again in a moment.")
# The servers an account publishes to (its relays) answered, and turned
# it down: the connection is not the cause.
TURNED_DOWN = N_("The servers your posts go to turned it down: try again later.")
SIGNER_SILENT = N_("Your signer app didn’t answer: open it and try again.")
SIGNER_DECLINED = N_("Your signer app declined the request: allow it there and try again.")
# The signer app answered with an error of its own.
SIGNER_FAILED = N_("Your signer app ran into a problem: open it and try again.")


def retry_quietly(attempt: Callable[[Callable[[T], None]], None], *,
                  settled: Callable[[T], bool], on_done: Callable[[T], None],
                  later: Callable[[int, Callable[[], None]], None],
                  delays: Sequence[int] = QUIET_RETRY_MS) -> None:
    """``attempt(answer)`` until an answer is ``settled``, again after each
    of ``delays`` (milliseconds, through ``later``); ``on_done`` hears the
    first settled answer, or the last one once the tries are spent. The
    answers before are heard by nobody: nothing is said for them."""
    left = list(delays)

    def answered(result: T) -> None:
        if settled(result) or not left:
            on_done(result)
        else:
            later(left.pop(0), lambda: attempt(answered))

    attempt(answered)


def markup(title: str, sentence: str) -> str:
    """A total failure for a rich-text label: its title in bold above its
    sentence, both translated (and escaped here)."""
    return f"<b>{html.escape(title)}</b><br>{html.escape(sentence)}"


def spoken(*parts: str) -> str:
    """The lines shown, as one announcement or one status bar line: a
    title, which has no end mark, gets a period, so it reads (and a
    screen reader pauses) as a sentence of its own."""
    return " ".join(part if part.endswith((".", "…", "!", "?")) else f"{part}."
                    for part in parts if part)


__all__ = ["CHECK_CONNECTION", "QUIET_RETRY_MS", "SERVER_TROUBLE", "SIGNER_DECLINED",
           "SIGNER_FAILED", "SIGNER_SILENT", "TURNED_DOWN", "markup", "retry_quietly", "spoken"]
