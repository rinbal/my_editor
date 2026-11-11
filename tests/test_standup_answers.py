# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The STANDUP answers, parsed once (nostr/standup/answers.py): a
malformed answer is refused whole, except the lookup, where one item
that cannot be read must never read as "not stored" (contract 12.4)."""

from __future__ import annotations

import json

import pytest

from nostr.standup import answers, formats
from nostr.standup.client import StandupApi
from nostr.standup.formats import FormatError
from tests.membership_fakes import FakeClock
from tests.standup_fakes import SERVER, FakeStandupServer, SignAs, StandupNam, submission_for
from tests.standup_vectors import ADDRESS, NOW, pk, sk


def lookup_answer(prepared) -> dict:
    """The fake server's lookup answer for ``prepared``, as JSON."""
    clock = FakeClock(NOW)
    server = FakeStandupServer(clock)
    nam = StandupNam(responder=server)
    api = StandupApi(SignAs(sk("writer")), server=SERVER, nam=nam, clock=clock)
    api.submit(prepared.body, lambda r: None, lambda e: None)
    nam.settle()
    reply = server._lookup(None, {"items": [{"status_key": prepared.status_key, "since": 0}]},
                           {}, {})
    return json.loads(reply._body)


def asked(*keys):
    return {formats.status_key_hash(k): k for k in keys}


def test_a_lookup_answers_by_status_key_and_leaves_out_what_the_server_does_not_know():
    prepared = submission_for(sk("writer"))
    other = "ee" * 32
    answer = answers.parse_lookup(lookup_answer(prepared), asked=asked(prepared.status_key, other))
    assert list(answer.found) == [prepared.status_key] and answer.unreadable == frozenset()


def test_an_item_that_cannot_be_read_marks_its_own_key_only():
    prepared = submission_for(sk("writer"))
    data = lookup_answer(prepared)
    data["items"][0]["receipt"]["seq"] = "eins"
    answer = answers.parse_lookup(data, asked=asked(prepared.status_key, "ee" * 32))
    assert answer.found == {} and answer.unreadable == {prepared.status_key}


def test_an_item_that_does_not_say_whose_it_is_marks_every_key_without_an_answer():
    prepared = submission_for(sk("writer"))
    data = lookup_answer(prepared)
    data["items"].append({"status_key_hash": "nonsense"})
    answer = answers.parse_lookup(data, asked=asked(prepared.status_key, "ee" * 32))
    assert list(answer.found) == [prepared.status_key]
    assert answer.unreadable == {"ee" * 32}


def test_a_session_must_be_for_the_person_who_signed_and_still_valid():
    data = {"token": "ab" * 32, "pubkey": pk("writer"), "expires_at": NOW + 3600}
    assert answers.parse_session(data, signed_by=pk("writer"), now=NOW).expires_at == NOW + 3600
    assert "ab" * 32 not in repr(answers.parse_session(data, signed_by=pk("writer"), now=NOW))
    with pytest.raises(FormatError, match="another person"):
        answers.parse_session(data, signed_by=pk("editor"), now=NOW)
    with pytest.raises(FormatError, match="expired"):
        answers.parse_session(data, signed_by=pk("writer"), now=NOW + 3600)
    with pytest.raises(FormatError):
        answers.parse_session(dict(data, token="xyz"), signed_by=pk("writer"), now=NOW)


def member(**changes) -> dict:
    return dict({"pubkey": pk("editor"), "role": "owner", "status": "active", "sees": "all",
                 "invited_by": None, "accepted_at": None, "away_until": None,
                 "releaser": True}, **changes)


def test_members_are_read_strictly():
    assert answers.parse_members({"members": [member()]})[0].releaser is True
    for broken in ({"role": "boss"}, {"status": "maybe"}, {"releaser": "yes"},
                   {"pubkey": "x"}, {"accepted_at": "gestern"}):
        with pytest.raises(FormatError):
            answers.parse_members({"members": [member(**broken)]})


def test_the_editorial_answer_names_invitations():
    data = {"turns": [], "counts": {"mine": 0},
            "publications": [{"address": ADDRESS, "role": "owner", "name": "Magazin"}],
            "invitations": [{"address": ADDRESS, "name": "Magazin", "role": "curator",
                             "sees": "since_join", "invited_by": pk("editor"),
                             "invited_at": NOW}]}
    editorial = answers.parse_editorial(data)
    assert editorial.publications[0].role == "owner"
    assert editorial.invitations[0] == answers.Invitation(ADDRESS, "Magazin", "curator",
                                                          "since_join", pk("editor"), NOW)
    with pytest.raises(FormatError):
        answers.parse_editorial(dict(data, invitations=[{"address": ADDRESS, "role": "owner"}]))


def test_a_listing_without_a_publication_address_or_key_event_is_left_out():
    event = {"kind": 30078}
    listed = answers.parse_listings({"publications": [
        {"address": ADDRESS, "name": "Magazin", "picture": "http://x", "key_event": event},
        {"address": "30004:xyz:d", "key_event": event},
        {"address": ADDRESS},
    ]})
    assert listed == (answers.Listing(ADDRESS, "Magazin", None, event),)


def test_the_instance_never_guesses_an_address():
    data = {"operator": "Einundzwanzig e.V.", "server": "http://standup.example",
            "web_app_by": "x", "bote": None, "inbox_relays": []}
    assert answers.parse_instance(data).server is None
    with pytest.raises(FormatError):
        answers.parse_instance(dict(data, bote={"pubkey": "x"}))
