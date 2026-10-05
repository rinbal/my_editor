# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The user's own profile and contact list come from where their lists
are, and only a validly signed event counts.

Before, the newest event any relay handed over won, signed or not, so a
relay could rename the user in their own editor or fill the mention
picker with strangers. These tests run the real lookup (newest_valid)
against relays that answer with forgeries next to the real events.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.contacts import ContactListFetcher  # noqa: E402
from nostr.known_people import KnownPeople  # noqa: E402
from nostr.metadata import ProfileMetadataFetcher  # noqa: E402
from nostr.outbox.policy import (  # noqa: E402
    LookupState, RelayList, bulk_profile_relays, lookup_relays,
)
from nostr.profiles import Profile, ProfileStore  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    NOW, OTHER_PK, OTHER_SK, PK, FakeRelayDirectory, FakeSubscription, settle, signed,
)

STRANGER_SK = bytes.fromhex("4c" * 32)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class ScriptedRelays:
    """Relays that answer every REQ with whatever they hold that matches
    its kinds and authors, forged or not, as a hostile relay would."""

    def __init__(self, held):
        self.held = list(held)
        self.asked = []          # (relays, filters) per REQ

    def subscribe(self, urls, filters):
        sub = FakeSubscription()
        self.asked.append((list(urls), filters))

        def answer():
            for flt in filters:
                for event in self.held:
                    if event["kind"] in flt.get("kinds", ()) and \
                            event["pubkey"] in flt.get("authors", ()):
                        sub.event.emit(event)
            sub.eose.emit()
        QTimer.singleShot(0, answer)
        return sub


def forged(event, **changes):
    """``event`` with fields changed after signing: its signature no
    longer matches, whatever it claims."""
    return {**event, **changes}


def profile_event(name, *, sk=None, created_at=NOW - 500):
    kwargs = {"sk": sk} if sk else {}
    return signed(0, content=json.dumps({"name": name}), created_at=created_at, **kwargs)


def my_directory():
    return FakeRelayDirectory({PK: RelayList(write=["wss://mine.example"],
                                             state=LookupState.FOUND)})


# -- the user's own profile (kind 0) ---------------------------------------------------

def metadata_fetcher(tmp_path, relays, directory=None):
    directory = directory or FakeRelayDirectory()
    store = ProfileStore(path=tmp_path / "profiles.json")
    store.upsert(Profile(user_pubkey=PK, bunker_pubkey="b" * 64,
                         bunker_relays=["wss://bunker.example"],
                         local_secret_hex="0" * 64))
    fetcher = ProfileMetadataFetcher(relays, store, relay_directory=directory)
    outcome = {"updated": [], "failed": []}
    fetcher.updated.connect(outcome["updated"].append)
    fetcher.failed.connect(outcome["failed"].append)
    return fetcher, store, outcome


def test_a_forged_newer_profile_does_not_rename_the_user(tmp_path):
    real = profile_event("Alice")
    relays = ScriptedRelays([real, forged(real, content=json.dumps({"name": "Mallory"}),
                                          created_at=NOW)])
    fetcher, store, outcome = metadata_fetcher(tmp_path, relays)
    fetcher.fetch(store.get(PK))
    settle()
    assert store.get(PK).display_name == "Alice"
    assert len(outcome["updated"]) == 1


def test_a_profile_signed_by_someone_else_is_not_the_users(tmp_path):
    impostor = forged(profile_event("Mallory", sk=OTHER_SK, created_at=NOW), pubkey=PK)
    relays = ScriptedRelays([impostor])
    fetcher, store, outcome = metadata_fetcher(tmp_path, relays)
    fetcher.fetch(store.get(PK))
    settle()
    assert store.get(PK).display_name == ""
    assert outcome["failed"] == ["no metadata event found"]


def test_the_profile_is_asked_for_where_the_users_lists_are(tmp_path):
    relays = ScriptedRelays([profile_event("Alice")])
    directory = my_directory()
    fetcher, store, _outcome = metadata_fetcher(tmp_path, relays, directory)
    fetcher.fetch(store.get(PK))
    settle()
    asked, _filters = relays.asked[0]
    assert asked == lookup_relays(known=directory.cached(PK))
    assert asked[0] == "wss://mine.example"
    assert "wss://bunker.example" not in asked     # a signer's relay is not a home


# -- the user's contact list (kind 3) and the people in it ----------------------------

def contact_fetcher(tmp_path, relays, directory=None):
    directory = directory or FakeRelayDirectory()
    people = KnownPeople(path=tmp_path / "people.json")
    fetcher = ContactListFetcher(relays, people, relay_directory=directory)
    return fetcher, people


def test_a_forged_newer_contact_list_is_ignored(tmp_path):
    real = signed(3, [["p", OTHER_PK]])
    stranger = "e" * 64
    relays = ScriptedRelays([real, forged(real, tags=[["p", stranger]], created_at=NOW)])
    fetcher, people = contact_fetcher(tmp_path, relays, my_directory())
    fetcher.fetch(PK)
    settle()
    assert OTHER_PK in people and stranger not in people
    asked, _filters = relays.asked[0]
    assert asked[0] == "wss://mine.example"


def test_follows_profiles_come_from_the_indexers_and_must_be_signed(tmp_path):
    bob = profile_event("Bob", sk=OTHER_SK)
    stranger = profile_event("Stranger", sk=STRANGER_SK, created_at=NOW)
    relays = ScriptedRelays([
        signed(3, [["p", OTHER_PK]]),
        bob,
        forged(bob, content=json.dumps({"name": "Eve"}), created_at=NOW),
        stranger,
    ])
    fetcher, people = contact_fetcher(tmp_path, relays)
    fetcher.fetch(PK)
    settle()
    assert people.get(OTHER_PK).display_name == "Bob"
    assert stranger["pubkey"] not in people
    metadata_relays, filters = relays.asked[1]
    assert metadata_relays == bulk_profile_relays()
    assert filters == [{"kinds": [0], "authors": [OTHER_PK]}]


def test_a_late_answer_for_another_account_is_dropped(tmp_path):
    relays = ScriptedRelays([signed(3, [["p", OTHER_PK]])])
    fetcher, people = contact_fetcher(tmp_path, relays)
    fetcher.fetch(PK)
    fetcher.fetch(OTHER_PK)          # the account switched before PK's answer
    settle()
    assert OTHER_PK not in people


def test_the_users_own_lookups_cannot_be_built_without_the_directory(tmp_path):
    # Without it, the user's own relays would silently go unasked.
    from nostr.blossom.server_list import UserServerList
    with pytest.raises(TypeError):
        ProfileMetadataFetcher(ScriptedRelays([]), ProfileStore(path=tmp_path / "p.json"))
    with pytest.raises(TypeError):
        ContactListFetcher(ScriptedRelays([]), KnownPeople(path=tmp_path / "k.json"))
    with pytest.raises(TypeError):
        UserServerList(ScriptedRelays([]))
