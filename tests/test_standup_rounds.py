# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round step of contract 2 (feat/redaktion bde1274): every entry of
the work asked about, each of its own server, at most 100 rounds and
32 MiB of answers together, and after a ``stale_revision`` the rounds
from the refused one asked by GET."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nostr.standup import drafting, formats, rounds
from nostr.standup.answers import LookupAnswer
from nostr.standup.entries import SubmissionEntry
from nostr.standup.errors import Code, StandupError
from nostr.standup.rounds import MAX_ROUND_ANSWER_BYTES, MAX_ROUNDS_ASKED, RoundAnswer, ask_round
from nostr.standup.watcher import AT_THE_END

WRITER = "a1" * 32
PUBLICATION = f"30078:{'b2' * 32}:magazin"
D = "ein-jahr"
SERVER = "https://s.example"
ELSEWHERE = "https://other.example"


def _sid(round_: int) -> str:
    return formats.submission_id(WRITER, PUBLICATION, formats.coordinate_hash(WRITER, D), round_)


def _entry(round_: int, server: str = SERVER) -> SubmissionEntry:
    return SubmissionEntry(sid=_sid(round_), publication=PUBLICATION, server=server,
                           role="writer", s_sub="e5" * 32, status_key=f"{round_:x}".rjust(64, "c"),
                           title="T", kind="article", d=D, round=round_, revision=1,
                           revision_hash="f6" * 32, envelope_sha256="a7" * 32,
                           wraps_sha256={"redaktion": "b8" * 32, "writer": "c9" * 32},
                           updated_at=1)


def _view(round_: int, state: str, later: bool = False, sid: str = ""):
    return SimpleNamespace(submission_id=sid or _sid(round_), round=round_, state=state,
                           later_round=later)


class Client:
    """One server: ``states`` by round for the lookup (the entry's own
    status key), ``held`` by round for the GET (a View, or the error it
    answers; a round not named is the server's own not_found). Every
    answer takes ``size`` bytes (a callable: by the call's number)."""

    def __init__(self, server: str = SERVER, *, states=None, held=None, size=100,
                 shifted: bool = False, other_view: bool = False) -> None:
        self.server = server
        self.states, self.held = states or {}, held or {}
        self.size = size
        self.shifted = shifted          # the lookup's View names the next round
        self.other_view = other_view    # its View is of another sid, its receipt not
        self.lookups, self.gets = [], []

    def _bytes(self, on_bytes, limit) -> bool:
        if limit is not None and limit < 1:
            raise ValueError("an answer limit is a positive number of bytes")  # as StandupApi
        count = self.size(len(self.lookups) + len(self.gets)) if callable(self.size) \
            else self.size
        on_bytes(count)
        return count <= limit

    def lookup(self, items, on_success, on_failure, *, limit=None, on_bytes=None) -> None:
        self.lookups.append(SimpleNamespace(keys=[k for k, _ in items], limit=limit,
                                            since={s for _, s in items}))
        if not self._bytes(on_bytes, limit):
            on_failure(StandupError(Code.BAD_RESPONSE, message="larger than allowed"))
            return
        found = {}
        for key, _since in items:
            round_ = int(key.lstrip("c") or "0", 16)
            if round_ in self.states:
                view = _view(round_ + self.shifted, self.states[round_],
                             sid=_sid(round_ + 1 if self.other_view else round_))
                found[key] = SimpleNamespace(submission=view,
                                             receipt=SimpleNamespace(submission_id=_sid(round_)))
        on_success(LookupAnswer(found=found, unreadable=frozenset()))

    def submission(self, sid, on_success, on_failure, *, since=None, since_internal=None,
                   limit=None, on_bytes=None) -> None:
        round_ = next(r for r in range(1, 1000) if _sid(r) == sid)
        self.gets.append(SimpleNamespace(round=round_, since=since,
                                         since_internal=since_internal, limit=limit))
        if not self._bytes(on_bytes, limit):
            on_failure(StandupError(Code.BAD_RESPONSE, message="larger than allowed"))
            return
        answer = self.held.get(round_)
        if answer is None:
            on_failure(StandupError(Code.NOT_FOUND, status=404, own=True))
        elif isinstance(answer, StandupError):
            on_failure(answer)
        else:
            on_success(SimpleNamespace(submission=answer))


def _ask(entries, clients, *, stored=None) -> RoundAnswer:
    by_server = {client.server: client for client in clients}
    said = []
    # A server this app may not send to gives a client without one, as
    # StandupApi does (servers.usable_server_url).
    ask_round(lambda server: by_server.get(server) or Client(""),
              entries, said.append, writer=WRITER, stored=stored)
    assert len(said) == 1
    return said[0]


# -- every entry, each of its own server -----------------------------------------

def test_every_entry_is_asked_of_its_own_server_with_its_timeline_left_out():
    # RG-M2r 9: an entry asked of another server read as never stored and
    # was sent again. Here round 1 is declined at the server it went to.
    here, there = Client(states={2: "withdrawn"}), Client(ELSEWHERE, states={1: "declined"})
    answer = _ask([_entry(1, ELSEWHERE), _entry(2)], [here, there])
    assert answer == RoundAnswer(3)
    assert [len(c.lookups) for c in (here, there)] == [1, 1]
    assert there.lookups[0].keys == [_entry(1).status_key]
    assert here.lookups[0].since == there.lookups[0].since == {AT_THE_END}


@pytest.mark.parametrize("wrong", ["shifted", "other_view"])
def test_a_lookup_answer_for_another_round_or_submission_is_not_guessed_from(wrong):
    answer = _ask([_entry(1)], [Client(states={1: "declined"}, **{wrong: True})])
    assert answer == rounds.unknown()


def test_an_entry_of_a_server_this_app_may_not_ask_is_not_guessed():
    answer = _ask([_entry(1, "http://nowhere.example")], [Client()])
    assert (answer.round, answer.words, answer.final) == (None, drafting.ROUND_UNKNOWN, False)


# -- the bounds ------------------------------------------------------------------

def test_more_rounds_than_the_bound_are_not_asked_about_and_say_why():
    client = Client()
    answer = _ask([_entry(r) for r in range(1, MAX_ROUNDS_ASKED + 2)], [client])
    assert (answer.round, answer.words, answer.final) == (None, drafting.ROUND_LIMIT, True)
    assert client.lookups == []
    client = Client(states={r: "declined" for r in range(1, MAX_ROUNDS_ASKED + 1)})
    answer = _ask([_entry(r) for r in range(1, MAX_ROUNDS_ASKED + 1)], [client])
    assert answer == RoundAnswer(MAX_ROUNDS_ASKED + 1)
    assert [len(lookup.keys) for lookup in client.lookups] == [50, 50]


@pytest.mark.parametrize("first, second, expected, limits", [
    (MAX_ROUND_ANSWER_BYTES - 10, 10, RoundAnswer(101), [MAX_ROUND_ANSWER_BYTES, 10]),
    (MAX_ROUND_ANSWER_BYTES - 10, 11, rounds.bounded(), [MAX_ROUND_ANSWER_BYTES, 10]),
    (MAX_ROUND_ANSWER_BYTES, 1, rounds.bounded(), [MAX_ROUND_ANSWER_BYTES]),  # none left
], ids=["within", "past the bound", "nothing left"])
def test_answers_together_are_read_up_to_their_bound(first, second, expected, limits):
    # The second lookup may only read what the first left of 32 MiB.
    client = Client(states={r: "declined" for r in range(1, 101)},
                    size=lambda n: first if n == 1 else second)
    assert _ask([_entry(r) for r in range(1, 101)], [client]) == expected
    assert [lookup.limit for lookup in client.lookups] == limits


# -- after stale_revision: the rounds from the refused one, by GET -------------------

@pytest.mark.parametrize("states, held, expected, asked", [
    ({}, {1: _view(1, "declined")}, RoundAnswer(2), [1]),
    ({}, {1: _view(1, "declined", later=True), 2: _view(2, "withdrawn")}, RoundAnswer(3),
     [1, 2]),
    ({}, {1: _view(1, "in_review")}, RoundAnswer(None), [1]),
    ({}, {}, RoundAnswer(1), [1]),                          # the server's own not_found
    ({1: "declined"}, {2: _view(2, "declined")}, RoundAnswer(3), [2]),  # its key found it
    ({1: "submitted"}, {}, RoundAnswer(None), []),          # open: nothing more asked
    ({}, {1: StandupError(Code.NOT_FOUND, status=404)}, rounds.unknown(), [1]),   # a proxy's
    ({}, {1: StandupError(Code.OFFLINE)}, rounds.unknown(), [1]),
    ({}, {1: _view(2, "declined", sid=_sid(1))}, rounds.unknown(), [1]),     # another round
    ({}, {1: _view(1, "declined", sid=_sid(2))}, rounds.unknown(), [1]),     # another sid
], ids=["declined", "a later one too", "open", "not found", "found by its key", "open by key",
        "a proxy's 404", "offline", "another round", "another sid"])
def test_after_stale_the_rounds_from_the_refused_one_are_asked_by_get(states, held, expected,
                                                                       asked):
    client = Client(states=states, held=held)
    assert _ask([], [client], stored=_entry(1)) == expected
    assert [get.round for get in client.gets] == asked
    assert all((get.since, get.since_internal) == (AT_THE_END, AT_THE_END)
               for get in client.gets)


def test_an_earlier_try_never_comes_before_the_refused_round():
    # The server stores the refused round, so each round before it too.
    client = Client(held={2: _view(2, "declined")})
    assert _ask([_entry(1)], [client], stored=_entry(2)) == RoundAnswer(3)
    assert [get.round for get in client.gets] == [2]


def test_an_entry_of_a_round_after_the_refused_one_is_sent_again_with_its_secrets():
    client = Client(held={1: _view(1, "declined")})
    answer = _ask([_entry(2)], [client], stored=_entry(1))
    assert answer.round == 2 and answer.unsent == _entry(2)


@pytest.mark.parametrize("held_bytes, rounds_held, expected_gets", [
    (100, MAX_ROUNDS_ASKED, MAX_ROUNDS_ASKED - 1),          # the round bound
    (MAX_ROUND_ANSWER_BYTES // 3, 10, 3),                   # the byte bound
    ((MAX_ROUND_ANSWER_BYTES - 100) // 2, 10, 2),           # exactly used up: no third
], ids=["rounds", "bytes", "nothing left"])
def test_asking_round_by_round_stops_at_the_bounds(held_bytes, rounds_held, expected_gets):
    # The refused entry is one round asked about; each GET is another.
    client = Client(held={r: _view(r, "declined", later=True) for r in range(1, rounds_held + 1)},
                    size=lambda n: 100 if n == 1 else held_bytes)
    assert _ask([], [client], stored=_entry(1)) == rounds.bounded()
    assert len(client.gets) == expected_gets
