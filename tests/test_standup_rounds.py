# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round step of contract 2 (feat/redaktion bde1274): every entry of
the work asked about, each of its own server, at most 100 rounds and
32 MiB of answers together; the entries found named stored (72d5ddc),
and a lookup's limit said with its wait (contract 15). A
``stale_revision`` no longer chooses a round again (72d5ddc): see
test_standup_submitting."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nostr import failures
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
NOW = 1_800_000_000.0


def _sid(round_: int) -> str:
    return formats.submission_id(WRITER, PUBLICATION, formats.coordinate_hash(WRITER, D), round_)


def _entry(round_: int, server: str = SERVER) -> SubmissionEntry:
    return SubmissionEntry(sid=_sid(round_), publication=PUBLICATION, server=server,
                           role="writer", s_sub="e5" * 32, status_key=f"{round_:x}".rjust(64, "0"),
                           title="T", kind="article", d=D, round=round_, revision=1,
                           revision_hash="f6" * 32, envelope_sha256="a7" * 32,
                           wraps_sha256={"redaktion": "b8" * 32, "writer": "c9" * 32},
                           updated_at=1)


def _view(round_: int, state: str, later: bool = False, sid: str = ""):
    return SimpleNamespace(submission_id=sid or _sid(round_), round=round_, state=state,
                           later_round=later)


class Client:
    """One server: ``states`` by round for the lookup (the entry's own
    status key; a round not named is not found). Every answer takes
    ``size`` bytes (a callable: by the call's number); ``refusal`` is
    the error every lookup answers, as StandupApi gives it (a 429 with
    its Retry-After parsed)."""

    def __init__(self, server: str = SERVER, *, states=None, size=100,
                 shifted: bool = False, other_view: bool = False, refusal=None,
                 refusals: int = -1, read_first: bool = False) -> None:
        self.server = server
        self.states = states or {}
        self.size = size
        self.shifted = shifted          # the lookup's View names the next round
        self.other_view = other_view    # its View is of another sid, its receipt not
        self.refusal = refusal
        self.refusals = refusals        # how many lookups it refuses (-1: every one)
        self.read_first = read_first    # the refusal comes after the answer's bytes
        self.lookups = []

    def _bytes(self, on_bytes, limit) -> bool:
        if limit is not None and limit < 1:
            raise ValueError("an answer limit is a positive number of bytes")  # as StandupApi
        count = self.size(len(self.lookups)) if callable(self.size) else self.size
        on_bytes(count)
        return count <= limit

    def lookup(self, items, on_success, on_failure, *, limit=None, on_bytes=None) -> None:
        self.lookups.append(SimpleNamespace(keys=[k for k, _ in items], limit=limit,
                                            since={s for _, s in items}))
        if self.refusal is not None and self.refusals != 0:
            self.refusals -= 1
            if self.read_first:
                self._bytes(on_bytes, limit)
            on_failure(self.refusal)
            return
        if not self._bytes(on_bytes, limit):
            on_failure(StandupError(Code.BAD_RESPONSE, message="larger than allowed"))
            return
        found = {}
        for key, _since in items:
            round_ = int(key, 16)
            if round_ in self.states:
                view = _view(round_ + self.shifted, self.states[round_],
                             sid=_sid(round_ + 1 if self.other_view else round_))
                found[key] = SimpleNamespace(submission=view,
                                             receipt=SimpleNamespace(submission_id=_sid(round_)))
        on_success(LookupAnswer(found=found, unreadable=frozenset()))


class Later:
    """``later``: each wait held, run by the test (lesson 34)."""

    def __init__(self) -> None:
        self.waits = []                 # the delays asked for, in order
        self._calls = []

    def __call__(self, ms, call) -> None:
        self.waits.append(ms)
        self._calls.append(call)

    def run(self) -> None:
        while self._calls:
            self._calls.pop(0)()


def _ask(entries, clients, later=None) -> RoundAnswer:
    by_server = {client.server: client for client in clients}
    later = later or Later()
    said = []
    # A server this app may not send to gives a client without one, as
    # StandupApi does (servers.usable_server_url).
    ask_round(lambda server: by_server.get(server) or Client(""),
              entries, said.append, later=later, clock=lambda: NOW)
    later.run()
    assert len(said) == 1
    return said[0]


# -- every entry, each of its own server -----------------------------------------

def test_every_entry_is_asked_of_its_own_server_with_its_timeline_left_out():
    # RG-M2r 9: an entry asked of another server read as never stored and
    # was sent again. Here round 1 is declined at the server it went to.
    here, there = Client(states={2: "withdrawn"}), Client(ELSEWHERE, states={1: "declined"})
    answer = _ask([_entry(1, ELSEWHERE), _entry(2)], [here, there])
    assert answer == RoundAnswer(3, stored=(_entry(1, ELSEWHERE), _entry(2)))
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
    assert (answer.round, answer.words) == (MAX_ROUNDS_ASKED + 1, "")
    assert [len(lookup.keys) for lookup in client.lookups] == [50, 50]


@pytest.mark.parametrize("first, second, expected, limits", [
    (MAX_ROUND_ANSWER_BYTES - 10, 10, RoundAnswer(101, stored=tuple(_entry(r) for r in
                                                                     range(1, 101))),
     [MAX_ROUND_ANSWER_BYTES, 10]),
    (MAX_ROUND_ANSWER_BYTES - 10, 11, rounds.bounded(), [MAX_ROUND_ANSWER_BYTES, 10]),
    (MAX_ROUND_ANSWER_BYTES, 1, rounds.bounded(), [MAX_ROUND_ANSWER_BYTES]),  # none left
], ids=["within", "past the bound", "nothing left"])
def test_answers_together_are_read_up_to_their_bound(first, second, expected, limits):
    # The second lookup may only read what the first left of 32 MiB.
    client = Client(states={r: "declined" for r in range(1, 101)},
                    size=lambda n: first if n == 1 else second)
    assert _ask([_entry(r) for r in range(1, 101)], [client]) == expected
    assert [lookup.limit for lookup in client.lookups] == limits


# -- what a lookup tells besides the round (72d5ddc, contract 15) -------------------

def test_an_entry_a_lookup_finds_is_named_stored_once():
    # A lookup that finds it is a receipt held for its sid with its status
    # key (13 at 72d5ddc): the caller marks it stored. One the server has
    # no receipt for, or one already marked, is not named.
    already = replace(_entry(2), stored=True)
    client = Client(states={1: "declined", 2: "withdrawn"})
    answer = _ask([_entry(1), already, _entry(3)], [client])
    assert answer.round == 3 and answer.unsent == _entry(3)
    assert answer.stored == (_entry(1),)
    assert _ask([_entry(1)], [Client()]).stored == ()


@pytest.mark.parametrize("retry_after, retry_at", [(90, NOW + 90), (None, None)],
                         ids=["with Retry-After", "without"])
def test_a_lookup_refused_for_a_limit_says_when_asking_again_helps(retry_after, retry_at):
    # RG-M2r2a 5: a 429 read as a round that cannot be told, and Try
    # Again asked again at once, one more counted request each time.
    client = Client(refusal=StandupError(Code.RATE_LIMITED, status=429,
                                         retry_after=retry_after))
    answer = _ask([_entry(1)], [client])
    assert answer == RoundAnswer(None, limited=True, retry_at=retry_at)
    assert not answer.words and not answer.final


def test_another_refusal_is_still_a_round_that_cannot_be_told():
    client = Client(refusal=StandupError(Code.BAD_RESPONSE))
    assert _ask([_entry(1)], [client]) == rounds.unknown()
    assert len(client.lookups) == 1                 # not a passing trouble: not again


# -- partial failure is silent (lessons 36) ---------------------------------------

@pytest.mark.parametrize("code", [Code.OFFLINE, Code.TIMEOUT, Code.SERVER])
def test_a_lookup_the_connection_stopped_once_is_asked_again_quietly(code):
    # One server of two did not answer the first time: asked again after
    # a wait, nothing said, and the round is told.
    later = Later()
    here = Client(states={2: "withdrawn"})
    there = Client(ELSEWHERE, states={1: "declined"}, refusal=StandupError(code), refusals=1)
    answer = _ask([_entry(1, ELSEWHERE), _entry(2)], [here, there], later)
    assert answer == RoundAnswer(3, stored=(_entry(1, ELSEWHERE), _entry(2)))
    assert later.waits == [failures.QUIET_RETRY_MS[0]] and len(there.lookups) == 2


def test_a_lookup_cut_off_after_every_byte_left_is_not_asked_again():
    # Asking again could read nothing more: the bounds are spent.
    later = Later()
    client = Client(refusal=StandupError(Code.TIMEOUT), read_first=True,
                    size=MAX_ROUND_ANSWER_BYTES)
    answer = _ask([_entry(1)], [client], later)
    assert answer.round is None and later.waits == [] and len(client.lookups) == 1


def test_a_lookup_that_never_gets_through_says_so_once_when_the_tries_are_spent():
    later = Later()
    client = Client(refusal=StandupError(Code.OFFLINE))
    answer = _ask([_entry(1)], [client], later)
    assert answer == rounds.unknown(Code.OFFLINE)
    assert answer.words == failures.CHECK_CONNECTION and not answer.final
    assert later.waits == list(failures.QUIET_RETRY_MS)
    assert len(client.lookups) == 1 + len(failures.QUIET_RETRY_MS)


@pytest.mark.parametrize("code, words", [
    (Code.OFFLINE, failures.CHECK_CONNECTION),
    (Code.TIMEOUT, failures.CHECK_CONNECTION),
    (Code.SERVER, failures.SERVER_TROUBLE),
])
def test_a_lookup_that_never_gets_through_is_said_by_its_true_cause(code, words):
    # RG-M2r3 6: a server in trouble (5xx) is never the connection.
    client = Client(refusal=StandupError(code))
    answer = _ask([_entry(1)], [client], Later())
    assert (answer.round, answer.words) == (None, words)
