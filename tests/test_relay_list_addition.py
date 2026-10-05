# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins adding one relay to the user's published relay list.

What must hold (the membership window relies on these outcomes):

  No list found (no_list), or none could be read (unreadable): nothing is
  published, because a list built from nothing would replace the
  person's real one. The two are told apart: only an account that has
  no list is offered the recommended one, and only on request.

  The new list keeps every relay already on it, adds the one relay, is
  newer than the old list, and goes to the relays the list names (added).

  A relay already listed is not added again and nothing is signed
  (already).

  A signer that says no, or relays that will not take it, change nothing
  (failed).

The safe read-modify-write underneath is pinned in test_outbox_package.py.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import relay_list_addition as rla  # noqa: E402
from nostr.outbox import defaults  # noqa: E402
from nostr.outbox.directory import RelayDirectory  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    ABSENT, NOW, UNKNOWN, FakeClient, FakePool, FakeQuery, FakeSessionPool,
    Profile, found, settle, signed,
)

E21 = "wss://nostr.einundzwanzig.space"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def run(answer, *, client=None, pool=None):
    client = client or FakeClient()
    pool = pool or FakePool()
    query = FakeQuery({10002: answer})
    directory = RelayDirectory(pool, query=query, store_path=None)
    job = rla.RelayListAddition(pool, FakeSessionPool(client), Profile(), E21,
                                directory=directory, query=query, clock=lambda: NOW)
    outcomes = []
    job.finished.connect(outcomes.append)
    job.start()
    settle(10)
    return outcomes, client, pool


def test_no_list_found_publishes_nothing():
    outcomes, client, pool = run(ABSENT)
    assert outcomes == [rla.NO_LIST]
    assert client.requests == [] and pool.published == []


def test_a_list_that_could_not_be_read_publishes_nothing():
    outcomes, client, pool = run(UNKNOWN)
    assert outcomes == [rla.UNREADABLE] and client.requests == []
    assert pool.published == []


def test_no_list_and_could_not_read_say_different_things():
    none = rla.outcome_message(rla.NO_LIST)
    unread = rla.outcome_message(rla.UNREADABLE)
    assert none != unread
    assert "recommended" in none
    assert "try again later" in unread.lower()


def test_the_new_list_keeps_everything_and_adds_the_relay():
    existing = signed(10002, [["r", "wss://a.example"], ["r", "wss://b.example", "read"]])
    outcomes, client, pool = run(found(existing))
    assert outcomes == [rla.ADDED]
    tags = client.requests[0]["tags"]
    assert tags == [["r", "wss://a.example"], ["r", "wss://b.example", "read"],
                    ["r", E21, "write"]]
    assert client.requests[0]["created_at"] > existing["created_at"]
    targets, _event = pool.published[0]
    for relay in ("wss://a.example", "wss://b.example", E21, *defaults.INDEXER_RELAYS):
        assert relay in targets


def test_a_relay_already_listed_is_left_alone():
    outcomes, client, pool = run(found(signed(10002, [["r", E21 + "/"]])))
    assert outcomes == [rla.ALREADY]
    assert client.requests == [] and pool.published == []


def test_a_signer_that_says_no_changes_nothing():
    outcomes, _client, pool = run(found(signed(10002, [["r", "wss://a.example"]])),
                                  client=FakeClient(fail="user rejected"))
    assert outcomes == [rla.FAILED] and pool.published == []


def test_relays_that_will_not_take_it_are_a_failure():
    existing = signed(10002, [["r", "wss://a.example"]])
    everyone = {"wss://a.example", E21, *defaults.INDEXER_RELAYS}
    outcomes, _client, _pool = run(found(existing), pool=FakePool(refuse=everyone))
    assert outcomes == [rla.FAILED]


def test_outcome_messages_are_plain_and_have_no_em_dashes():
    for outcome in (rla.ADDED, rla.ALREADY, rla.NO_LIST, rla.FAILED):
        text = rla.outcome_message(outcome)
        assert text and "\u2014" not in text and "10002" not in text


# -- the recommended list, for an account that has none ----------------------------------

def publish(answer, *, client=None, pool=None):
    client = client or FakeClient()
    pool = pool or FakePool()
    query = FakeQuery({10002: answer})
    directory = RelayDirectory(pool, query=query, store_path=None)
    job = rla.RecommendedRelayList(pool, FakeSessionPool(client), Profile(), E21,
                                   directory=directory, query=query, clock=lambda: NOW)
    outcomes = []
    job.finished.connect(outcomes.append)
    job.start()
    settle(10)
    return outcomes, client, pool


def test_the_recommended_list_carries_the_members_relay():
    outcomes, client, pool = publish(ABSENT)
    assert outcomes == [rla.PUBLISHED]
    tags = client.requests[0]["tags"]
    assert ["r", E21, "write"] in tags
    for url, _marker in defaults.STARTER_LIST:
        assert any(tag[1] == url for tag in tags)
    assert pool.published


def test_the_recommended_list_never_replaces_a_list_that_exists():
    existing = signed(10002, [["r", "wss://a.example"]])
    outcomes, client, pool = publish(found(existing))
    assert outcomes == [rla.LIST_EXISTS]
    assert client.requests == [] and pool.published == []


def test_the_recommended_list_is_not_published_when_the_list_could_not_be_read():
    outcomes, client, pool = publish(UNKNOWN)
    assert outcomes == [rla.UNREADABLE]
    assert client.requests == [] and pool.published == []


def test_every_outcome_has_plain_words():
    for outcome in (rla.ADDED, rla.ALREADY, rla.NO_LIST, rla.UNREADABLE, rla.FAILED,
                    rla.PUBLISHED, rla.LIST_EXISTS):
        text = rla.outcome_message(outcome)
        assert text and "\u2014" not in text and "10002" not in text
    assert rla.DONE == {rla.ADDED, rla.ALREADY, rla.PUBLISHED}
