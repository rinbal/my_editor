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

After a 409 ``stale_revision`` for a revision 1 the server stores that
sid already, under secrets the list does not hold for it (another device
handed the work in meanwhile, or the list lost its entry), so a lookup
by the list's status key cannot find it. ``stored`` names the entry of
that try: from its round on, the rounds are asked about one by one,
``GET .../submissions/<sid>`` with both timelines left out, until one
the server stores nothing for, one still open, or a closed one with no
later round (``later_round``).

Together the step asks about at most :data:`MAX_ROUNDS_ASKED` rounds and
reads at most :data:`MAX_ROUND_ANSWER_BYTES` of answers. At either
bound, and at any answer that is neither a stored submission nor the
server's own ``not_found`` (a proxy's page says nothing), nothing is
chosen and :class:`RoundAnswer` says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from i18n import _
from . import drafting, formats
from .entries import SubmissionEntry
from .errors import Code, StandupError
from .watcher import AT_THE_END, look_up_all, lookup_batches

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
    discarded (Verwerfen)."""

    round: Optional[int]
    words: str = ""
    unsent: Optional[SubmissionEntry] = None
    from_list: bool = False
    final: bool = False


def unknown() -> RoundAnswer:
    """A server could not be asked, or its answer not used: not told, so
    not guessed (a guess could take the place of the entry, and with it
    the keys, of a submission sent before, RG-M2b 1)."""
    return RoundAnswer(None, _(drafting.ROUND_UNKNOWN))


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


def ask_round(client_for: Callable[[str], Any], entries: Sequence[SubmissionEntry],
              on_done: Callable[[RoundAnswer], None], *, writer: str,
              stored: Optional[SubmissionEntry] = None) -> None:
    """``on_done(RoundAnswer)`` for the work of ``entries``: the writer's
    entries of one work at one publication, each with a status key, from
    a complete read of the list. ``client_for(server)`` gives the client
    of a server (lookups need no signature; the GET of a round after
    ``stored`` needs the session of ``stored``'s server). ``stored``: the
    entry a revision 1 was refused for as ``stale_revision``."""
    asked: List[SubmissionEntry] = sorted(entries, key=lambda e: e.round)
    if stored is not None and all(entry.sid != stored.sid for entry in asked):
        asked.append(stored)
    budget = _Budget()
    if not budget.take(len(asked)):
        on_done(bounded())
        return
    batches = lookup_batches(asked)
    states: Dict[str, Optional[str]] = {}

    def failed(_error: StandupError) -> None:
        on_done(bounded() if budget.spent else unknown())

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
            look_up_all(api, batch, lambda items: found(index, items), failed,
                        limit=budget.bytes, on_bytes=budget.read)

    def found(index: int, items) -> None:
        states.update({sid: item.submission.state if item else None
                       for sid, item in items.items()})
        look_up(index + 1)

    def choose() -> None:
        choice = drafting.choose_round(asked, states)
        if choice.round is None or stored is None:
            on_done(RoundAnswer(choice.round, unsent=choice.unsent))
            return
        # The server stores the round refused, so each before it too
        # (it takes a round only after the last one closed).
        probe(max(choice.round, stored.round))

    def free(round_: int) -> None:
        unsent = next((e for e in asked if e.round == round_ and states.get(e.sid) is None
                       and e.sid != stored.sid), None)
        on_done(RoundAnswer(round_, unsent=unsent))

    def probe(round_: int) -> None:
        if not budget.take(1):
            on_done(bounded())
            return
        coordinate = formats.coordinate_hash(writer, stored.d)
        sid = formats.submission_id(writer, stored.publication, coordinate, round_)

        def held(detail) -> None:
            view = detail.submission
            if view.submission_id != sid or view.round != round_:
                on_done(unknown())
            elif view.state not in drafting.CLOSED_STATES:
                on_done(RoundAnswer(None))          # handed in there, still open
            elif view.later_round:
                probe(round_ + 1)
            else:
                free(round_ + 1)

        def not_held(error: StandupError) -> None:
            if budget.spent:
                on_done(bounded())
            elif error.code == Code.NOT_FOUND and error.own:
                free(round_)
            else:
                on_done(unknown())

        # ``stored`` was looked up of this same client first (its server
        # checked there).
        client_for(stored.server).submission(
            sid, held, not_held, since=AT_THE_END, since_internal=AT_THE_END,
            limit=budget.bytes, on_bytes=budget.read)

    look_up(0)


__all__ = ["MAX_ROUNDS_ASKED", "MAX_ROUND_ANSWER_BYTES", "RoundAnswer", "ask_round", "bounded",
           "unknown"]
