# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins showing a member's Nostr address on their profile.

What must hold (the membership window relies on these outcomes):

  The new profile keeps every field it had (name, picture, lightning
  address, fields MyEditor does not know) and only sets the address
  (shown).

  A profile that already shows the address is left alone and nothing is
  signed (already).

  No profile found (no_profile), or none could be read (unreadable):
  nothing is published, because a profile holding only an address would
  replace the person's name and picture everywhere.

  A signer that says no, or relays that will not take it, change nothing
  (failed).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import profile_address as pa  # noqa: E402
from nostr.outbox.directory import RelayDirectory  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    ABSENT, NOW, UNKNOWN, FakeClient, FakePool, FakeQuery, FakeSessionPool,
    Profile, found, settle, signed,
)

ADDRESS = "alice@einundzwanzig.space"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def run(answer, *, client=None, pool=None, jobs=None):
    client = client or FakeClient()
    pool = pool or FakePool()
    query = FakeQuery({0: answer})
    directory = RelayDirectory(pool, query=query, store_path=None)
    job = pa.ProfileAddress(pool, FakeSessionPool(client), Profile(), ADDRESS,
                            directory=directory, query=query, clock=lambda: NOW)
    outcomes = []
    job.finished.connect(outcomes.append)
    job.start()
    settle(10)
    if jobs is not None:
        jobs.append(job)
    return outcomes, client, pool


def profile(**fields):
    return signed(0, [["alt", "profile"]], json.dumps(fields))


def test_the_address_is_added_and_every_other_field_is_kept():
    existing = profile(name="Alice", picture="https://a.example/p.png",
                       lud16="alice@wallet.example", banner="https://a.example/b.png")
    outcomes, client, pool = run(found(existing))
    assert outcomes == [pa.SHOWN]
    sent = client.requests[0]
    assert json.loads(sent["content"]) == {
        "name": "Alice", "picture": "https://a.example/p.png",
        "lud16": "alice@wallet.example", "banner": "https://a.example/b.png",
        "nip05": ADDRESS}
    assert sent["tags"] == [["alt", "profile"]]
    assert sent["created_at"] > existing["created_at"]
    assert pool.published


def test_another_address_is_replaced():
    outcomes, client, _pool = run(found(profile(name="Alice", nip05="alice@example.com")))
    assert outcomes == [pa.SHOWN]
    assert json.loads(client.requests[0]["content"])["nip05"] == ADDRESS


def test_a_profile_that_already_shows_it_is_left_alone():
    outcomes, client, pool = run(found(profile(name="Alice", nip05=ADDRESS)))
    assert outcomes == [pa.ALREADY]
    assert client.requests == [] and pool.published == []


def test_no_profile_found_publishes_nothing():
    outcomes, client, pool = run(ABSENT)
    assert outcomes == [pa.NO_PROFILE]
    assert client.requests == [] and pool.published == []


def test_a_profile_that_could_not_be_read_publishes_nothing():
    outcomes, client, pool = run(UNKNOWN)
    assert outcomes == [pa.UNREADABLE]
    assert client.requests == [] and pool.published == []


def test_a_profile_that_is_not_an_object_is_never_guessed_at():
    outcomes, client, pool = run(found(signed(0, [], "[1, 2]")))
    assert outcomes == [pa.FAILED]
    assert client.requests == [] and pool.published == []


def test_a_signer_that_says_no_changes_nothing():
    jobs = []
    outcomes, _client, pool = run(found(profile(name="Alice")), jobs=jobs,
                                  client=FakeClient(fail="user rejected"))
    assert outcomes == [pa.FAILED] and pool.published == []
    # Lessons 36: what did not happen, then the one sentence of its cause.
    assert pa.outcome_message(pa.FAILED, jobs[0].write) == (
        "Your profile wasn’t changed. Your signer app declined the request: allow it there "
        "and try again.")


def test_a_profile_no_relay_answered_for_is_said_as_the_connection():
    jobs = []
    outcomes, _client, _pool = run(UNKNOWN, jobs=jobs)
    assert outcomes == [pa.UNREADABLE]
    assert pa.outcome_message(pa.UNREADABLE, jobs[0].write) == (
        "Your profile wasn’t changed. Check your internet connection and try again.")


def test_a_window_for_another_account_says_so():
    assert pa.inactive_message() == (
        "Your profile wasn’t changed. This window is for an account that isn’t active any "
        "more: close it and open it again.")


def test_every_outcome_has_plain_words():
    for outcome in (pa.SHOWN, pa.ALREADY, pa.NO_PROFILE, pa.UNREADABLE, pa.FAILED):
        text = pa.outcome_message(outcome)
        assert text and "\u2014" not in text and "nip05" not in text.lower()
    assert pa.outcome_message(pa.NO_PROFILE) != pa.outcome_message(pa.UNREADABLE)
    assert pa.DONE == {pa.SHOWN, pa.ALREADY}
