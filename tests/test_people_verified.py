# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who a person is comes only from their own signed profile, and the
newest one wins without letting a far-future date win forever.

Search relays and indexers can answer with anything. A forged kind 0
dated years ahead used to be stored as the person's profile, and with
that date nothing real could ever replace it.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import contacts  # noqa: E402
from nostr.contacts import ContactListFetcher, metadata_time, parse_metadata_event  # noqa: E402
from nostr.known_people import KnownPeople, Person  # noqa: E402
from nostr.search import Nip50SearchClient  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    OTHER_PK, OTHER_SK, PK, FakeRelayDirectory, HandPool, settle, signed,
)

YEAR = 365 * 86_400


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def profile(name, *, created_at, sk=None):
    kwargs = {"sk": sk} if sk else {}
    return signed(0, content=json.dumps({"name": name}), created_at=created_at, **kwargs)


def search(people, *events_):
    pool = HandPool()
    client = Nip50SearchClient(pool, people)
    batches = []
    client.results.connect(lambda _query, batch: batches.append(batch))
    client.search("ali")
    sub = pool.subs[0]
    for event in events_:
        sub.event.emit(event)
    sub.eose.emit()
    return [p.display_name for p in batches[0]]


def test_a_forged_search_result_cannot_shadow_the_real_profile(tmp_path):
    people = KnownPeople(path=tmp_path / "people.json")
    now = int(time.time())
    forged = dict(profile("Mallory", created_at=now + 10 * YEAR), sig="00" * 64)
    real = profile("Alice", created_at=now - 60)
    assert search(people, forged, real) == ["Alice"]
    assert people.get(PK).display_name == "Alice"
    # ... and the next real update replaces it, as it should.
    search(people, profile("Alice B.", created_at=now))
    assert people.get(PK).display_name == "Alice B."


def test_only_profiles_count_in_a_search(tmp_path):
    people = KnownPeople(path=tmp_path / "people.json")
    note = signed(1, content="ali is here", created_at=int(time.time()))
    assert search(people, note) == [] and len(people) == 0


def test_a_profile_dated_far_ahead_is_taken_as_only_a_little_ahead():
    now = 1_000_000
    event = profile("Alice", created_at=now + 10 * YEAR)
    assert metadata_time(event, now=now) == now + contacts.FUTURE_TOLERANCE_S
    assert parse_metadata_event(event, now=now).updated_at == now + contacts.FUTURE_TOLERANCE_S
    assert metadata_time(dict(event, created_at=str(now))) == 0


def test_an_older_record_only_fills_what_the_newer_one_lacks(tmp_path):
    people = KnownPeople(path=tmp_path / "people.json")
    people.upsert(Person(pubkey=PK, display_name="New", updated_at=200))
    people.upsert(Person(pubkey=PK, display_name="Old", picture="https://p/old.png",
                         updated_at=100))
    assert people.get(PK).display_name == "New"
    assert people.get(PK).picture == "https://p/old.png"
    # A contact list's petname (no time) does not rename them either, but
    # its relay hint and source are kept.
    people.upsert(Person(pubkey=PK, display_name="petname", relay_hint="wss://hint.example",
                         source="contact"))
    person = people.get(PK)
    assert (person.display_name, person.relay_hint, person.source) == \
        ("New", "wss://hint.example", "contact")


def test_a_profile_no_newer_than_the_known_one_is_not_even_checked(tmp_path, monkeypatch):
    people = KnownPeople(path=tmp_path / "people.json")
    now = int(time.time())
    people.upsert(Person(pubkey=OTHER_PK, display_name="Bob", updated_at=now))
    checked = []
    real_verify = contacts.events.verify_event
    monkeypatch.setattr(contacts.events, "verify_event",
                        lambda event: checked.append(event["id"]) or real_verify(event))
    fetcher = ContactListFetcher(HandPool(), people, relay_directory=FakeRelayDirectory())
    fetcher._followed_set = {OTHER_PK}
    fetcher._on_metadata_event(profile("Old Bob", created_at=now - 100, sk=OTHER_SK))
    assert checked == [] and people.get(OTHER_PK).display_name == "Bob"
    fetcher._on_metadata_event(profile("Bob Again", created_at=now + 1, sk=OTHER_SK))
    assert len(checked) == 1 and people.get(OTHER_PK).display_name == "Bob Again"
    settle()
