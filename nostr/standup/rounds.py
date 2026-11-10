# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round a work goes to a publication in (editorial contract 2,
"Choosing the round and the secrets", feat/redaktion bde1274), steps 2
to 4. Step 1, a complete read of the writer's submissions list, is the
caller's and comes first.

:func:`ask_round` asks about every entry the list holds for the work at
the publication, each of the server it names
(:func:`~.watcher.lookup_batches`), by its status key and with its
timeline left out, and chooses from what the servers say
(:func:`~.drafting.choose_round`).

A lookup that the connection or the server's trouble stopped is asked
again quietly, with backoff (lessons 36: nothing is said while it still
can work); only then is the round said not to be told.

A lookup that finds an entry's submission is a receipt held for its
sid with its status key: :class:`RoundAnswer` names those entries not
yet marked so (``stored``), and the caller marks them on the list (13,
72d5ddc). A lookup refused for a limit (429, contract 15) is said as
one, with when asking again helps (``limited``, ``retry_at``), never as
a round that cannot be told. A round still open whose receipt misses a
picture is said as stored with pictures missing, never as handed in:
the app asking holds none of its files (a run here that does is shown
instead), so the app that sent it uploads them (12.4 at 33d92c9).

Together the step asks about at most :data:`MAX_ROUNDS_ASKED` rounds and
reads at most :data:`MAX_ROUND_ANSWER_BYTES` of answers. At either
bound, and at any answer that is not the lookup's, nothing is chosen and
:class:`RoundAnswer` says why.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from i18n import _
from .. import failures
from . import drafting
from .entries import SubmissionEntry
from .errors import PASSING, Code, StandupError, cause_words
from .watcher import look_up_all, lookup_batches
from .words import (
    ELSEWHERE_NOT_HERE_YET, NOT_CHECKED, PICTURE_NOT_IN, PICTURES_IN_THE_APP_THAT_SENT,
    SUBMITTED_ELSEWHERE,
)

MAX_ROUNDS_ASKED = 100
MAX_ROUND_ANSWER_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class RoundAnswer:
    """The round a text goes to a publication in: ``round``, or None
    while it is still open there (``words`` empty) or when it cannot be
    told (``words`` say why, translated). The sheet offers Try Again then
    (lesson 12) unless ``final``: asking again would meet the same bound.
    ``from_list``: the words are about the list of submissions, so Try
    Again reads it again (``StandupWriter.reload``) before asking.
    ``unsent`` is the list's entry of an earlier try the server has no
    receipt for: it is sent again, with its own secrets (Erneut), or
    discarded (Verwerfen). ``limited``: a lookup met a limit of the
    server (no round told, ``words`` empty: the sheet names the
    publication), and asking again helps from ``retry_at`` on (clock
    time; None without a Retry-After). ``stored``: entries the lookups
    found the server holds, not yet marked so on the list. ``elsewhere``:
    the entry of a round another device stored (its revision 1 refused
    here as ``stale_revision``): no round, the ``words`` say so, and Try
    Again (Erneut laden) reads the list again (contract 2 at 0456312)."""

    round: Optional[int]
    words: str = ""
    unsent: Optional[SubmissionEntry] = None
    from_list: bool = False
    final: bool = False
    limited: bool = False
    retry_at: Optional[float] = None
    stored: Tuple[SubmissionEntry, ...] = ()
    elsewhere: Optional[SubmissionEntry] = None
    # What did not happen, untranslated: with ``words``, said as their
    # title.
    title: str = NOT_CHECKED


def unknown(code: str = "") -> RoundAnswer:
    """A server could not be asked (``code``, one of PASSING: asked again
    quietly first, then said by its cause, the connection or the
    server's trouble), or its answer not used: not told, so not guessed
    (a guess could take the place of the entry, and with it the keys, of
    a submission sent before, RG-M2b 1)."""
    return RoundAnswer(None, _(cause_words(code) if code in PASSING else drafting.ROUND_UNKNOWN))


def bounded() -> RoundAnswer:
    return RoundAnswer(None, _(drafting.ROUND_LIMIT), final=True)


class _Budget:
    """What the round step may still ask about, and read."""

    def __init__(self) -> None:
        self.rounds = MAX_ROUNDS_ASKED
        self.bytes = MAX_ROUND_ANSWER_BYTES

    def take(self, rounds: int) -> bool:
        """Whether ``rounds`` more may be asked about (counted then)."""
        if rounds > self.rounds or self.bytes <= 0:
            return False
        self.rounds -= rounds
        return True

    def read(self, count: int) -> None:
        self.bytes -= count

    @property
    def spent(self) -> bool:
        """An answer went past the bytes left (and was not read)."""
        return self.bytes < 0


def limited(retry_after: Optional[int], clock: Callable[[], float] = time.time) -> RoundAnswer:
    """A lookup refused for a limit: when asking again helps."""
    return RoundAnswer(None, limited=True,
                       retry_at=clock() + retry_after if retry_after else None)


def ask_round(client_for: Callable[[str], Any], entries: Sequence[SubmissionEntry],
              on_done: Callable[[RoundAnswer], None], *,
              later: Callable[[int, Callable[[], None]], None],
              clock: Callable[[], float] = time.time,
              refused: Callable[[SubmissionEntry], bool] = lambda _entry: False) -> None:
    """``on_done(RoundAnswer)`` for the work of ``entries``: the writer's
    entries of one work at one publication, each with a status key, from
    a complete read of the list. ``client_for(server)`` gives the client
    of a server (lookups need no signature, they carry the status keys);
    ``later(ms, call)`` waits before a quiet try again; ``clock`` is the
    time a limit's wait counts from; ``refused(entry)`` whether a
    ``stale_revision`` for the entry's revision 1 is held."""
    asked: List[SubmissionEntry] = sorted(entries, key=lambda e: e.round)
    budget = _Budget()
    if not budget.take(len(asked)):
        on_done(bounded())
        return
    batches = lookup_batches(asked)
    states: Dict[str, Optional[str]] = {}
    missing_pictures = set()                    # sids whose receipt misses a file

    def failed(error: StandupError) -> None:
        if budget.spent:
            on_done(bounded())
        elif error.code == Code.RATE_LIMITED:
            on_done(limited(error.retry_after, clock))
        else:
            on_done(unknown(error.code))

    def look_up(index: int) -> None:
        if index == len(batches):
            choose()
            return
        batch = batches[index]
        api = client_for(batch[0].server)
        if budget.bytes <= 0:
            on_done(bounded())
        elif api.server != batch[0].server:
            on_done(unknown())                      # a server this app may not ask
        else:
            def attempt(answer) -> None:
                look_up_all(api, batch, answer, answer, limit=budget.bytes,
                            on_bytes=budget.read)

            def settled(result) -> bool:
                return not (isinstance(result, StandupError) and result.code in PASSING
                            and budget.bytes > 0)

            failures.retry_quietly(
                attempt, settled=settled, later=later,
                on_done=lambda result: failed(result) if isinstance(result, StandupError)
                else found(index, result))

    def found(index: int, items) -> None:
        states.update({sid: item.submission.state if item else None
                       for sid, item in items.items()})
        missing_pictures.update(sid for sid, item in items.items()
                                if item is not None and item.receipt.blobs_missing)
        look_up(index + 1)

    def choose() -> None:
        choice = drafting.choose_round(asked, states, refused)
        # A lookup that found it is a receipt held (13, 72d5ddc).
        held = tuple(e for e in asked if states.get(e.sid) is not None and not e.stored)
        if choice.elsewhere is not None:
            on_done(RoundAnswer(None, _(ELSEWHERE_NOT_HERE_YET), from_list=True, stored=held,
                                elsewhere=choice.elsewhere, title=SUBMITTED_ELSEWHERE))
            return
        if choice.open is not None and choice.open.sid in missing_pictures:
            # Nothing to upload from here: no Try Again (final).
            on_done(RoundAnswer(None, _(PICTURES_IN_THE_APP_THAT_SENT), final=True, stored=held,
                                title=PICTURE_NOT_IN))
            return
        on_done(RoundAnswer(choice.round, unsent=choice.unsent, stored=held))

    look_up(0)


__all__ = ["MAX_ROUNDS_ASKED", "MAX_ROUND_ANSWER_BYTES", "RoundAnswer", "ask_round", "bounded",
           "limited", "unknown"]
