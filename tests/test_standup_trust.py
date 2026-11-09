# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which key a submission is encrypted to (nostr/standup/trust.py,
editorial contract 8.1).

Criterion 4 of the plan: a key event with a wrong signature, the wrong
``d``, the wrong signer or an older epoch than this app has seen blocks
submitting, with the contract's words. The fixture's key events are the
ones both apps test."""

from __future__ import annotations

import json

import pytest

import i18n
from nostr import events
from nostr.standup import trust
from nostr.standup.client import StandupApi
from nostr.standup.trust import KeyNotConfirmed, verify_key_event
from tests.membership_fakes import FakeClock
from tests.standup_fakes import SERVER, FakeStandupServer, SignAs, StandupNam
from tests.standup_vectors import NOW, VECTORS, pk, sk

KEYS = VECTORS["key_event"]
ADDRESS = KEYS["address"]
GERMAN = "Der Schlüssel der Redaktion ist nicht bestätigt. Einreichen ist gerade nicht möglich."


class Settings:
    """The app's settings store, in memory."""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.saved = []

    def load(self):
        return json.loads(json.dumps(self.data))

    def save(self, key, value):
        self.data[key] = value
        self.saved.append((key, value))


def store(data=None):
    settings = Settings(data)
    return trust.EpochStore(load=settings.load, save=settings.save), settings


def key_event(epoch, *, created_at=NOW - 50, sk_name="publication", d=None, content=None,
              server="https://standup.example"):
    body = json.loads(KEYS["valid"]["content"])
    body.update(epoch=epoch, server=server)
    if content:
        body.update(content)
    return events.sign_event({
        "kind": 30078, "created_at": created_at, "content": json.dumps(body, sort_keys=True),
        "tags": [["d", d or "standup:redaktion-key:einundzwanzig-magazin"], ["a", ADDRESS]],
    }, sk(sk_name))


# -- the checks of 8.1 -----------------------------------------------------------

def test_the_fixtures_key_event_is_confirmed():
    key = verify_key_event(KEYS["valid"], ADDRESS, highest_seen=KEYS["highest_seen_epoch"])
    content = json.loads(KEYS["valid"]["content"])
    assert (key.epoch, key.pubkey, key.server) == (3, content["pubkey"], "https://standup.example")
    assert key.rules["max_open_per_writer"] == 3 and key.name == "Einundzwanzig Magazin"
    assert key.event_id == KEYS["valid"]["id"]


@pytest.mark.parametrize("name", sorted(KEYS["must_fail"]))
def test_every_broken_key_event_of_the_fixture_is_refused(name):
    with pytest.raises(KeyNotConfirmed):
        verify_key_event(KEYS["must_fail"][name], ADDRESS)


def test_an_older_epoch_than_seen_is_refused():
    with pytest.raises(KeyNotConfirmed, match="older"):
        verify_key_event(KEYS["older_epoch"], ADDRESS, highest_seen=KEYS["highest_seen_epoch"])
    # On a computer that never saw epoch 3, epoch 2 is the newest it knows.
    assert verify_key_event(KEYS["older_epoch"], ADDRESS).epoch == 2


@pytest.mark.parametrize("change, reason", [
    (lambda e: dict(e, kind=1), "kind 30078"),
    (lambda e: dict(e, kind="30078"), "kind 30078"),
    (lambda e: dict(e, content=e["content"].replace("3", "4", 1)), "signature"),
    (lambda e: key_event(3, content={"publication": "30004:" + "ab" * 32 + ":x"}),
     "another publication"),
    (lambda e: key_event(3, content={"pubkey": "ff" * 32}), "x-only"),
    (lambda e: key_event(3, content={"v": 2}), "version"),
    (lambda e: key_event("3"), "whole number"),
    (lambda e: key_event(True), "whole number"),
    (lambda e: key_event(3, content={"rules": None}), "rules"),
    (lambda e: key_event(3, server="http://standup.example"), "server"),
    (lambda e: key_event(3, d="standup:redaktion-key:other"), "d tag"),
    (lambda e: dict(key_event(3), tags=key_event(3)["tags"]
                    + [["d", "standup:redaktion-key:einundzwanzig-magazin"]]), "signature"),
])
def test_anything_else_wrong_is_refused_too(change, reason):
    with pytest.raises(KeyNotConfirmed, match=reason):
        verify_key_event(change(KEYS["valid"]), ADDRESS)


def test_two_d_tags_are_refused():
    event = events.sign_event({
        "kind": 30078, "created_at": NOW, "content": KEYS["valid"]["content"],
        "tags": [["d", "standup:redaktion-key:einundzwanzig-magazin"], ["d", "other"]],
    }, sk("publication"))
    with pytest.raises(KeyNotConfirmed, match="d tag"):
        verify_key_event(event, ADDRESS)


def test_a_content_with_a_key_twice_is_refused():
    content = KEYS["valid"]["content"][:-1] + ',"epoch":9}'
    event = events.sign_event({"kind": 30078, "created_at": NOW, "content": content,
                               "tags": KEYS["valid"]["tags"]}, sk("publication"))
    with pytest.raises(KeyNotConfirmed, match="twice"):
        verify_key_event(event, ADDRESS)


def test_a_local_server_counts_only_with_the_developer_setting():
    event = key_event(3, server="http://127.0.0.1:5000")
    with pytest.raises(KeyNotConfirmed):
        verify_key_event(event, ADDRESS, local_http=False)
    assert verify_key_event(event, ADDRESS, local_http=True).server == "http://127.0.0.1:5000"


def test_the_refusal_says_the_contracts_words_in_german():
    refusal = KeyNotConfirmed("why")
    assert refusal.words == trust.KEY_NOT_CONFIRMED
    i18n.install("de")
    try:
        text = i18n._(refusal.words)
    finally:
        i18n.install("en")
    assert text == GERMAN


# -- the highest epoch -------------------------------------------------------------

def test_the_highest_epoch_is_kept_in_the_settings_and_only_raised():
    epochs, settings = store()
    assert epochs.highest(ADDRESS) == 0
    epochs.note(ADDRESS, 3)
    epochs.note(ADDRESS, 2)
    assert epochs.highest(ADDRESS) == 3
    assert settings.data[trust.SETTINGS_KEY] == {ADDRESS: 3}
    assert len(settings.saved) == 1


def test_a_broken_settings_entry_counts_as_nothing_seen():
    epochs, _settings = store({trust.SETTINGS_KEY: {ADDRESS: "9", "x": True}})
    assert epochs.highest(ADDRESS) == 0


def test_the_newest_valid_event_wins_and_is_remembered():
    epochs, _ = store()
    candidates = [KEYS["older_epoch"], KEYS["valid"], KEYS["must_fail"]["bad_signature"],
                  key_event(3, created_at=NOW - 10)]
    key = trust.newest_key(candidates, ADDRESS, epochs)
    assert key.epoch == 3 and key.created_at == NOW - 10
    assert epochs.highest(ADDRESS) == 3
    # The server now hands out epoch 2 again: refused, epoch 3 was seen.
    with pytest.raises(KeyNotConfirmed, match="older"):
        trust.newest_key([KEYS["older_epoch"]], ADDRESS, epochs)


def test_with_nothing_valid_there_is_no_key():
    epochs, _ = store()
    with pytest.raises(KeyNotConfirmed):
        trust.newest_key([None, KEYS["must_fail"]["wrong_pubkey"]], ADDRESS, epochs)
    with pytest.raises(KeyNotConfirmed, match="no key event"):
        trust.newest_key([], ADDRESS, epochs)


# -- from the server and from relays -------------------------------------------------

class Relays:
    def __init__(self, found=()):
        self.found = list(found)
        self.asked = []

    def __call__(self, pubkey, d_tag, on_done):
        self.asked.append((pubkey, d_tag))
        on_done(list(self.found))


def _world(key=None):
    clock = FakeClock(NOW)
    server = FakeStandupServer(clock, key_event=key)
    nam = StandupNam(responder=server)
    api = StandupApi(SignAs(sk("writer")), server=SERVER, nam=nam, clock=clock)
    return server, nam, api


def _confirm(api, nam, relays, epochs):
    outcome = []
    trust.confirm_key(api, ADDRESS, relays, epochs, lambda k: outcome.append(("ok", k)),
                      lambda r: outcome.append(("refused", r)))
    nam.settle()
    assert len(outcome) == 1
    return outcome[0]


def test_the_key_to_submit_to_comes_from_the_server_and_the_relays():
    server, nam, api = _world()
    relays = Relays([key_event(4)])
    epochs, _ = store()
    kind, key = _confirm(api, nam, relays, epochs)
    assert kind == "ok" and key.epoch == 4
    assert relays.asked == [(pk("publication"), "standup:redaktion-key:einundzwanzig-magazin")]
    assert epochs.highest(ADDRESS) == 4
    # The server still serves epoch 3: from now on that is refused.
    kind, refusal = _confirm(api, nam, Relays(), epochs)
    assert kind == "refused" and "older" in refusal.reason


def test_a_substituted_key_on_the_server_blocks_submitting():
    # Criterion 4: the server's event is signed by someone else.
    server, nam, api = _world(key=KEYS["must_fail"]["wrong_pubkey"])
    epochs, _ = store()
    kind, refusal = _confirm(api, nam, Relays(), epochs)
    assert kind == "refused" and refusal.words == trust.KEY_NOT_CONFIRMED


def test_relays_carry_the_key_when_the_server_cannot_be_asked():
    server, nam, api = _world()
    server.inject("settings_get", "drop before")
    epochs, _ = store()
    kind, key = _confirm(api, nam, Relays([KEYS["valid"]]), epochs)
    assert kind == "ok" and key.epoch == 3


def test_destinations_are_listed_with_their_verified_keys():
    server, nam, api = _world()
    epochs, _ = store()
    finder = trust.Destinations(api, Relays([key_event(4)]), epochs)
    found = []
    finder.find(found.append, lambda e: found.append(("err", e)))
    nam.settle()
    [listed] = found
    [destination] = listed
    assert destination.usable and destination.key.epoch == 4
    assert destination.name == "Einundzwanzig Magazin"


def test_a_destination_without_a_valid_key_is_listed_as_not_usable():
    server, nam, api = _world(key=KEYS["must_fail"]["bad_signature"])
    epochs, _ = store()
    finder = trust.Destinations(api, Relays(), epochs)
    found = []
    finder.find(found.append, lambda e: found.append(("err", e)))
    nam.settle()
    [destination] = found[0]
    assert not destination.usable and destination.key is None and destination.problem


def test_stopping_the_finder_drops_the_answer():
    server, nam, api = _world()
    epochs, _ = store()
    finder = trust.Destinations(api, Relays(), epochs)
    found = []
    finder.find(found.append, found.append)
    finder.stop()
    nam.settle()
    assert found == []


def test_the_relay_lookup_asks_the_publications_outbox_for_one_event():
    asked = {}

    class Directory:
        def outbox_of(self, pubkey, on_done):
            asked["pubkey"] = pubkey
            on_done(["wss://relay.example"])

    import nostr.queries as queries

    def fake_fetch(pool, relays, filters, on_done, *, accept=None, timeout_ms=0, parent=None):
        asked.update(relays=relays, filters=filters)
        on_done(queries.Fetched(events=(KEYS["valid"],)))

    original = queries.fetch_events
    queries.fetch_events = fake_fetch
    try:
        lookup = trust.relay_key_lookup(object(), Directory())
        got = []
        lookup(pk("publication"), "standup:redaktion-key:einundzwanzig-magazin", got.append)
    finally:
        queries.fetch_events = original
    assert got == [[KEYS["valid"]]]
    assert asked["filters"] == [{"kinds": [30078], "authors": [pk("publication")],
                                 "#d": ["standup:redaktion-key:einundzwanzig-magazin"]}]
