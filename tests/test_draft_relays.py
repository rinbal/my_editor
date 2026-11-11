# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the private relay list for drafts (NIP-37, kind 10013).

What must hold: the relays travel encrypted in the content as
``[["relay", url], ...]`` and never as plain tags; reading tells "none"
apart from "could not read or decrypt"; a list that could not be read is
never replaced; an empty list is never published.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.draft_relays import (  # noqa: E402
    KIND_PRIVATE_RELAYS, UNDECRYPTED, UNREACHABLE, PrivateDraftRelays, draft_relay_address,
    parse_private_relays, private_relays_plaintext,
)
from nostr.outbox import writer as outbox_writer  # noqa: E402
from nostr.outbox.directory import RelayDirectory  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    ABSENT, NOW, PK, UNKNOWN, FakeClient, FakePool, FakeQuery, FakeSessionPool, Profile,
    found, settle, signed,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


class SealingClient(FakeClient):
    """Encrypts to self by wrapping the text, so a test can see what was sealed."""

    def __init__(self, *, refuse=False, **kw):
        super().__init__(**kw)
        self.refuse = refuse

    def nip44_encrypt_self(self, plaintext, on_success, on_failure):
        if self.refuse:
            on_failure("user rejected")
        else:
            on_success("sealed:" + plaintext)

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure):
        if self.refuse or not ciphertext.startswith("sealed:"):
            on_failure("cannot decrypt")
        else:
            on_success(ciphertext[len("sealed:"):])


def lists(answer, client=None):
    client = client or SealingClient()
    pool = FakePool()
    query = FakeQuery({KIND_PRIVATE_RELAYS: answer})
    directory = RelayDirectory(pool, query=query, store_path=None)
    return PrivateDraftRelays(profile=Profile(), pool=pool,
                              session_pool=FakeSessionPool(client), directory=directory,
                              query=query, clock=lambda: NOW), client, pool


def test_the_list_is_read_and_decrypted():
    event = signed(KIND_PRIVATE_RELAYS, [],
                   'sealed:[["relay","wss://Mine.example/"],["relay","wss://two.example"]]')
    drafts, _client, _pool = lists(found(event))
    got = []
    drafts.read(got.append)
    assert got == [["wss://mine.example", "wss://two.example"]]


def test_none_and_unreadable_are_told_apart():
    for answer, expected, failure in ((ABSENT, [], None), (UNKNOWN, None, UNREACHABLE)):
        drafts, _client, _pool = lists(answer)
        got = []
        drafts.read(got.append)
        assert got == [expected] and drafts.last_failure == failure
    undecryptable = signed(KIND_PRIVATE_RELAYS, [], "garbage")
    drafts, _client, _pool = lists(found(undecryptable))
    got = []
    drafts.read(got.append)
    assert got == [None] and drafts.last_failure == UNDECRYPTED     # the signer, not the net


def test_publishing_encrypts_the_relays_into_the_content():
    drafts, client, pool = lists(ABSENT)
    outcomes = []
    drafts.publish(["wss://mine.example/", "wss://mine.example"], outcomes.append)
    settle(10)
    assert outcomes[0].status == outbox_writer.WRITTEN
    sent = client.requests[0]
    assert sent["kind"] == KIND_PRIVATE_RELAYS and sent["tags"] == []
    assert sent["content"] == 'sealed:[["relay","wss://mine.example"]]'
    assert pool.published


def test_a_list_that_could_not_be_read_is_never_replaced():
    drafts, client, pool = lists(UNKNOWN)
    outcomes = []
    drafts.publish(["wss://mine.example"], outcomes.append)
    settle(10)
    assert outcomes[0].status == outbox_writer.UNKNOWN_BASE
    assert client.requests == [] and pool.published == []


def test_a_signer_that_will_not_encrypt_changes_nothing():
    drafts, client, pool = lists(ABSENT, SealingClient(refuse=True))
    outcomes = []
    drafts.publish(["wss://mine.example"], outcomes.append)
    assert outcomes[0].status == outbox_writer.FAILED and pool.published == []
    assert outcomes[0].cause == outbox_writer.CAUSE_SIGNER


def test_an_empty_list_is_never_published():
    with pytest.raises(ValueError):
        private_relays_plaintext(["not a relay"])


def test_a_list_that_is_not_json_is_not_guessed_at():
    with pytest.raises(ValueError):
        parse_private_relays('{"relay": "wss://x.example"}')
    assert parse_private_relays('[["r", "wss://x.example"], ["relay"]]') == []


@pytest.mark.parametrize("typed, address", [
    ("wss://Relay.Example/", "wss://relay.example"),
    ("  wss://relay.example:443  ", "wss://relay.example"),
    ("wss://nas.local:4848", "wss://nas.local:4848"),      # the person's own network
    ("wss://10.0.0.5", "wss://10.0.0.5"),
    ("ws://localhost:7777", "ws://localhost:7777"),        # a relay on this computer
    ("ws://127.0.0.1:4869", "ws://127.0.0.1:4869"),
    ("ws://[::1]:4869", "ws://[::1]:4869"),
    ("ws://relay.example", None),                          # readable on the way
    ("ws://192.168.1.20:7777", None),
    ("https://relay.example", None),
    ("relay.example", None),
    ("wss://", None),
    ("wss://user@relay.example", None),
    ("", None),
])
def test_an_address_typed_for_drafts(typed, address):
    assert draft_relay_address(typed) == address


def test_going_back_to_the_usual_relays_publishes_an_empty_list():
    existing = signed(KIND_PRIVATE_RELAYS, [], 'sealed:[["relay","wss://mine.example"]]')
    drafts, client, pool = lists(found(existing))
    outcomes = []
    drafts.clear(outcomes.append)
    settle(10)
    assert outcomes[0].status == outbox_writer.WRITTEN
    assert client.requests[0]["content"] == "sealed:[]" and client.requests[0]["tags"] == []
    assert parse_private_relays("[]") == []                 # read back: none, the usual relays


def test_an_account_without_a_list_has_nothing_to_stop():
    drafts, client, pool = lists(ABSENT)
    outcomes = []
    drafts.clear(outcomes.append)
    settle(10)
    assert outcomes[0].status == outbox_writer.REFUSED
    assert client.requests == [] and pool.published == []


# -- saving moves the drafts (review H1) ------------------------------------------------

MINE = "wss://mine.example"           # the account's draft relay before the change
OTHER = "wss://other.example"         # one it moves to
U1, U2 = "wss://usual-1.example", "wss://usual-2.example"   # its usual relays


def wrap(d_tag, content="sealed draft", *, at=NOW - 500, extra=()):
    return signed(31234, [["d", d_tag], ["k", "1"], *extra], content, created_at=at)


def moving(answer, *, drafts_on=None, refuse_publish=(), reason="blocked"):
    """A PrivateDraftRelays that already read ``answer`` (the account's
    list), on relays a test fills (``drafts_on``: relay -> events) and
    runs: Relays answers reads and takes or refuses what is sent."""
    from tests.published_fakes import Relays
    relays = Relays(refuse_publish=refuse_publish)
    relays.publisher.reason = reason
    for relay, events_ in (drafts_on or {}).items():
        relays.keep(relay, *events_)
    query = FakeQuery({KIND_PRIVATE_RELAYS: answer})
    directory = RelayDirectory(relays, query=query, store_path=None)
    directory.remember(signed(10002, [["r", U1], ["r", U2]], created_at=NOW - 900))
    client = SealingClient()
    drafts = PrivateDraftRelays(profile=Profile(), pool=relays,
                                session_pool=FakeSessionPool(client), directory=directory,
                                query=query, clock=lambda: NOW)
    read = []
    drafts.read(read.append)
    if read and read[0] is not None:
        directory.set_draft_relays(PK, read[0])
    return drafts, relays, directory, client


def save(drafts, relays, chosen):
    outcomes, steps = [], []
    drafts.save(chosen, outcomes.append, on_step=lambda *step: steps.append(step))
    relays.run(rounds=20)
    return outcomes[0] if outcomes else None, steps


def sent(relays, kind=None):
    """(relays, event) of what was published, in order."""
    return [(urls, event) for urls, event in relays.published
            if kind is None or event["kind"] == kind]


LIST_ON_MINE = found(signed(KIND_PRIVATE_RELAYS, [], 'sealed:[["relay","wss://mine.example"]]'))


def test_going_back_to_the_usual_relays_moves_the_drafts_there_first():
    draft, deleted = wrap("draft-1"), wrap("draft-2", content="")
    request = signed(5, [["a", f"31234:{PK}:draft-3"], ["k", "31234"]], created_at=NOW - 400)
    old_version = wrap("draft-1", content="older", at=NOW - 900)
    drafts, relays, directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [draft, deleted, request, old_version]})
    outcome, steps = save(drafts, relays, [])
    assert outcome.ok and outcome.move.moved == 3 and outcome.move.total == 3
    moved = sent(relays)[:3]
    assert {event["id"] for _urls, event in moved} == {draft["id"], deleted["id"], request["id"]}
    assert all(set(urls) == {U1, U2} for urls, _event in moved)     # where drafts go next
    (_urls, listed), = sent(relays, KIND_PRIVATE_RELAYS)
    assert listed["content"] == "sealed:[]" and sent(relays)[-1][1] is listed  # the list last
    assert directory.draft_relays_of(PK) == []
    # Three events moved, one of them a draft with content (the count shown).
    assert ("moving_drafts", 3, 3, 1) in steps and steps[-1][0] == "saving_list"


def test_moving_to_another_relay_sends_the_drafts_there():
    draft = wrap("draft-1")
    drafts, relays, directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: [draft]})
    outcome, _steps = save(drafts, relays, [OTHER])
    assert outcome.ok
    assert sent(relays, 31234) == [([OTHER], draft)]
    assert directory.draft_relays_of(PK) == [OTHER]


def test_a_relay_being_left_that_does_not_answer_changes_nothing():
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    relays.down = {MINE}
    outcome, _steps = save(drafts, relays, [])
    assert not outcome.ok and outcome.move.unread == (MINE,) and outcome.write is None
    assert relays.published == []                       # neither drafts nor the list
    assert directory.draft_relays_of(PK) == [MINE]      # drafts go where they went


def test_a_draft_no_new_relay_took_changes_nothing():
    drafts, relays, directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [wrap("draft-1")]}, refuse_publish={U1, U2},
        reason="publish timeout")
    outcome, _steps = save(drafts, relays, [])
    assert not outcome.ok and len(outcome.move.not_moved) == 1
    assert not outcome.move.refused                     # silence: try again later
    assert sent(relays, KIND_PRIVATE_RELAYS) == []
    assert directory.draft_relays_of(PK) == [MINE]


def test_a_refusal_is_told_from_silence():
    drafts, relays, _directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [wrap("draft-1")]}, refuse_publish={U1, U2},
        reason="blocked: not on this relay")
    outcome, _steps = save(drafts, relays, [])
    assert not outcome.ok and outcome.move.refused      # choose other relays


def test_a_draft_one_new_relay_has_already_is_moved_whatever_the_other_says():
    draft = wrap("draft-1")
    drafts, relays, _directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [draft], U1: [draft]}, refuse_publish={U2},
        reason="blocked: no")
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok and sent(relays, 31234) == [([U2], draft)]


def test_a_draft_a_request_deleted_is_left_and_the_request_moves():
    deleted = wrap("draft-1", at=NOW - 500)
    request = signed(5, [["a", f"31234:{PK}:draft-1"], ["k", "31234"]], created_at=NOW - 400)
    saved_again = wrap("draft-2", at=NOW - 300)
    request_2 = signed(5, [["a", f"31234:{PK}:draft-2"], ["k", "31234"]],
                       created_at=NOW - 350)       # older than the draft saved after it
    drafts, relays, _directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [deleted, request, saved_again, request_2]})
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok
    assert {event["id"] for _urls, event in sent(relays) if event["kind"] != KIND_PRIVATE_RELAYS} \
        == {request["id"], saved_again["id"], request_2["id"]}


def test_save_anyway_leaves_the_drafts_of_a_relay_that_is_gone():
    drafts, relays, directory, _client = moving(
        LIST_ON_MINE, drafts_on={U1: [wrap("draft-1")]})
    relays.down = {MINE}
    outcomes = []
    drafts.save([], outcomes.append, leave_unread=[MINE])
    relays.run(rounds=20)
    assert outcomes[0].ok and directory.draft_relays_of(PK) == []
    (_urls, listed), = sent(relays, KIND_PRIVATE_RELAYS)
    assert listed["content"] == "sealed:[]"


def test_save_anyway_leaves_only_the_relays_it_named():
    # Agreeing to leave MINE's drafts is not agreeing to leave SECOND's:
    # SECOND going quiet too stops the change, and says so.
    second = "wss://second.example"
    list_on_both = found(signed(
        KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{MINE}"],["relay","{second}"]]'))
    drafts, relays, directory, _client = moving(list_on_both,
                                                drafts_on={second: [wrap("draft-1")]})
    relays.down = {MINE, second}
    outcomes = []
    drafts.save([], outcomes.append, leave_unread=[MINE])
    relays.run(rounds=20)
    assert not outcomes[0].ok and outcomes[0].move.unread == (second,)
    assert relays.published == [] and directory.draft_relays_of(PK) == [MINE, second]


class _Every:
    """Every relay (as ``Relays.down``)."""

    def __contains__(self, _url):
        return True


def test_nothing_read_at_all_changes_nothing_even_when_leaving_them():
    drafts, relays, directory, _client = moving(LIST_ON_MINE)
    relays.down = _Every()
    outcomes = []
    drafts.save([], outcomes.append, leave_unread=[MINE])
    relays.run(rounds=20)
    assert not outcomes[0].ok and relays.published == []
    assert directory.draft_relays_of(PK) == [MINE]


def test_drafts_saved_by_routing_older_than_the_list_move_too():
    # Another device moved the drafts to MINE since this one started, and
    # this one still saved a draft by its older routing, on OLD.
    old = "wss://old.example"
    late_one = wrap("draft-9")
    drafts, relays, directory, _client = moving(LIST_ON_MINE, drafts_on={old: [late_one]})
    directory.set_draft_relays(PK, [old])
    outcome, _steps = save(drafts, relays, [OTHER])
    assert outcome.ok and sent(relays, 31234) == [([OTHER], late_one)]


def test_a_draft_saved_while_the_list_is_signed_is_caught_up():
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    late = wrap("draft-2", at=NOW - 5)
    outcomes = []

    def step(name, *_counts):
        if name == "saving_list":                 # another device saves now, by the old list
            relays.keep(MINE, late)

    drafts.save([OTHER], outcomes.append, on_step=step)
    relays.run(rounds=30)
    assert outcomes[0].ok
    assert ([OTHER], late) in sent(relays, 31234)
    assert directory.draft_relays_of(PK) == [OTHER]


def test_a_list_changed_elsewhere_since_it_was_read_is_not_written_over():
    drafts, relays, directory, _client = moving(LIST_ON_MINE)
    newer = signed(KIND_PRIVATE_RELAYS, [], 'sealed:[["relay","wss://else.example"]]',
                   created_at=NOW - 50)
    drafts._query.answers[KIND_PRIVATE_RELAYS] = found(newer)    # changed on another device
    outcome, _steps = save(drafts, relays, [OTHER])
    assert not outcome.ok and outcome.write.cause == outbox_writer.CAUSE_CHANGED
    assert sent(relays, KIND_PRIVATE_RELAYS) == [] and directory.draft_relays_of(PK) == [MINE]


def test_a_relay_that_has_a_newer_version_has_the_draft():
    drafts, relays, _directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [wrap("draft-1")]}, refuse_publish={U1, U2},
        reason="replaced: have newer event")
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok and outcome.move.moved == 1


def test_drafts_already_where_they_go_next_are_not_sent_again():
    draft = wrap("draft-1")
    drafts, relays, _directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [draft], U1: [draft], U2: [draft]})
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok and outcome.move.moved == 1 and sent(relays, 31234) == []


def test_an_expired_draft_is_left_behind():
    expired = wrap("old-import", extra=[["expiration", str(NOW - 10)]])
    drafts, relays, _directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: [expired]})
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok and outcome.move.total == 0 and sent(relays, 31234) == []


def test_every_draft_is_read_a_page_at_a_time(monkeypatch):
    from nostr import draft_relays
    monkeypatch.setattr(draft_relays, "MOVE_PAGE", 2)
    many = [wrap(f"draft-{n}", at=NOW - 100 * n) for n in range(5)]
    drafts, relays, _directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: many})
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok and outcome.move.moved == 5


def test_a_relay_that_ignores_the_page_asked_for_is_not_read_forever(monkeypatch):
    # It sends its newest drafts whatever ``until`` says: its older ones
    # can never be reached, so it counts as a relay that did not answer.
    from nostr import draft_relays
    monkeypatch.setattr(draft_relays, "MOVE_PAGE", 3)
    many = [wrap(f"draft-{n}", at=NOW - 100 * n) for n in range(5)]
    drafts, relays, directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: many})
    relays.ignoring_until = {MINE}
    outcome, _steps = save(drafts, relays, [])
    assert outcome is not None and not outcome.ok and outcome.move.unpaged == (MINE,)
    assert outcome.move.unread == ()                   # it answered: not a connection matter
    assert len(relays.subs) <= 3                       # two pages, not hundreds
    assert relays.published == [] and directory.draft_relays_of(PK) == [MINE]


def test_save_anyway_passes_a_relay_that_ignores_the_page_asked_for(monkeypatch):
    # Review M2: Save Anyway failed the same way every time.
    from nostr import draft_relays
    monkeypatch.setattr(draft_relays, "MOVE_PAGE", 3)
    many = [wrap(f"draft-{n}", at=NOW - 100 * n) for n in range(5)]
    drafts, relays, directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: many})
    relays.ignoring_until = {MINE}
    outcomes = []
    drafts.save([], outcomes.append, leave_unread=[MINE])
    relays.run(rounds=20)
    assert outcomes[0].ok and directory.draft_relays_of(PK) == []


def test_a_kept_relay_that_ignores_the_page_asked_for_blocks_nothing(monkeypatch):
    from nostr import draft_relays
    monkeypatch.setattr(draft_relays, "MOVE_PAGE", 3)
    many = [wrap(f"draft-{n}", at=NOW - 100 * n) for n in range(5)]
    drafts, relays, directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: many})
    relays.ignoring_until = {MINE}
    outcome, _steps = save(drafts, relays, [MINE, OTHER])
    assert outcome.ok and directory.draft_relays_of(PK) == [MINE, OTHER]


def test_a_reading_that_does_not_end_is_given_up(monkeypatch):
    from nostr import draft_relays
    monkeypatch.setattr(draft_relays, "MOVE_PAGE", 2)
    monkeypatch.setattr(draft_relays, "MOVE_PAGES_AT_MOST", 2)
    many = [wrap(f"draft-{n}", at=NOW - 100 * n) for n in range(6)]
    drafts, relays, _directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: many})
    outcome, _steps = save(drafts, relays, [])
    assert not outcome.ok and outcome.move.unread == (MINE,) and relays.published == []


def test_a_save_stopped_while_the_drafts_are_read_changes_nothing():
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    outcomes, answers = [], []
    drafts.save([OTHER], outcomes.append)
    assert drafts.cancel()
    relays.run(rounds=20)
    assert outcomes == [] and relays.published == []
    from nostr.outbox import ask_draft_relays
    ask_draft_relays(directory, Profile(), answers.append)   # saving a draft goes on
    settle()
    assert answers == [[MINE]] and directory.draft_relays_of(PK) == [MINE]


def test_sends_on_their_way_when_a_save_is_stopped_are_let_go_quietly():
    # Review L6: answers to sends made before Cancel reached a mover the
    # sheet had deleted, and every one raised in the log.
    import sys
    from PySide6.QtCore import QCoreApplication, QEvent
    from tests.outbox_fakes import FakeJob
    drafts, relays, _directory, _client = moving(
        LIST_ON_MINE, drafts_on={MINE: [wrap(f"d{n}") for n in range(3)]})
    held = []

    def hold(urls, event):
        job = FakeJob()
        held.append((job, [(url, True, "") for url in urls]))
        return job

    relays.publisher.publish = hold
    drafts.save([], lambda _outcome: None)
    relays.run(rounds=10)
    assert held and drafts.cancel()
    drafts.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    errors, real_hook = [], sys.excepthook
    sys.excepthook = lambda kind, value, _tb: errors.append(f"{kind.__name__}: {value}")
    try:
        for job, results in held:
            job.report(results)
        settle(5)
    finally:
        sys.excepthook = real_hook
    assert errors == []


def test_a_list_on_its_way_can_not_be_stopped():
    drafts, relays, _directory, _client = moving(LIST_ON_MINE,
                                                 drafts_on={MINE: [wrap("draft-1")]})
    outcomes = []

    def step(name, *_counts):
        if name == "saving_list":
            assert not drafts.cancel()

    drafts.save([OTHER], outcomes.append, on_step=step)
    relays.run(rounds=20)
    assert outcomes[0].ok


LOCKED = "wss://locked.example"       # takes drafts, shows them only after a sign-in


def test_a_relay_that_takes_drafts_but_does_not_give_them_back_is_not_kept():
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    relays.write_only = {LOCKED}
    outcome, _steps = save(drafts, relays, [LOCKED])
    assert not outcome.ok and outcome.move.unreadable == (LOCKED,) and outcome.write is None
    assert sent(relays, KIND_PRIVATE_RELAYS) == [] and directory.draft_relays_of(PK) == [MINE]


def test_a_relay_that_answers_ok_and_keeps_nothing_is_not_kept():
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    relays.keeps_nothing = {OTHER}
    outcome, _steps = save(drafts, relays, [OTHER])
    assert not outcome.ok and outcome.move.unreadable == (OTHER,)
    assert directory.draft_relays_of(PK) == [MINE]


def test_a_relay_that_would_not_show_drafts_is_found_out_with_no_draft_to_move():
    drafts, relays, directory, _client = moving(ABSENT)
    relays.write_only = {LOCKED}
    outcome, _steps = save(drafts, relays, [LOCKED])
    assert not outcome.ok and outcome.move.unreadable == (LOCKED,)
    assert sent(relays, KIND_PRIVATE_RELAYS) == [] and directory.draft_relays_of(PK) == []


def test_a_relay_that_cannot_be_reached_is_not_judged_by_the_read_back():
    # Down now is not "keeps nothing": as when drafts are sent there, a
    # relay that does not answer says nothing either way.
    gone = "wss://gone.example"
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    relays.down = {gone}
    outcome, _steps = save(drafts, relays, [MINE, gone])
    assert outcome.ok and directory.draft_relays_of(PK) == [MINE, gone]


def test_a_relay_holding_a_newer_version_of_a_draft_gives_it_back():
    # Review L1: nostr-rs-relay, obelisk and khatru answer OK to an older
    # version of a draft they hold a newer one of, and keep only the newer.
    older, newer = wrap("draft-1", at=NOW - 500), wrap("draft-1", "newer", at=NOW - 50)
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [older], OTHER: [newer]})
    relays.keeps_nothing = {OTHER}
    outcome, _steps = save(drafts, relays, [OTHER])
    assert outcome.ok and outcome.move.unreadable == ()
    assert directory.draft_relays_of(PK) == [OTHER]


def test_only_the_relay_that_does_not_give_them_back_is_named():
    drafts, relays, _directory, _client = moving(LIST_ON_MINE,
                                                 drafts_on={MINE: [wrap("draft-1")]})
    relays.write_only = {LOCKED}
    outcome, _steps = save(drafts, relays, [OTHER, LOCKED])
    assert not outcome.ok and outcome.move.unreadable == (LOCKED,)


def test_drafts_saved_while_they_move_wait_for_where_they_go_next():
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    outcomes, answers = [], []
    drafts.save([OTHER], outcomes.append)
    # A draft being saved now (DraftPublishJob asks this) waits ...
    from nostr.outbox import ask_draft_relays
    ask_draft_relays(directory, Profile(), answers.append)
    settle()
    assert answers == []
    relays.run(rounds=20)
    assert outcomes[0].ok and answers == [[OTHER]]      # ... and goes to the new relay


def test_saving_the_same_list_moves_nothing():
    drafts, relays, _directory, _client = moving(LIST_ON_MINE,
                                                 drafts_on={MINE: [wrap("draft-1")]})
    outcome, _steps = save(drafts, relays, [MINE])
    assert outcome.ok and outcome.move.total == 0 and sent(relays, 31234) == []


def test_saving_none_without_a_list_is_already_done():
    drafts, relays, directory, _client = moving(ABSENT)
    outcome, _steps = save(drafts, relays, [])
    assert outcome.ok and outcome.write.status == outbox_writer.UNCHANGED
    assert sent(relays, KIND_PRIVATE_RELAYS) == [] and directory.draft_relays_of(PK) == []


def test_a_list_that_did_not_go_out_leaves_the_drafts_where_they_were():
    drafts, relays, directory, _client = moving(LIST_ON_MINE)
    drafts._query.answers[KIND_PRIVATE_RELAYS] = UNKNOWN    # the list can't be read now
    outcome, _steps = save(drafts, relays, [OTHER])
    assert outcome.move.ok and outcome.write.status == outbox_writer.UNKNOWN_BASE
    assert sent(relays, KIND_PRIVATE_RELAYS) == [] and directory.draft_relays_of(PK) == [MINE]


def test_saving_only_unusable_addresses_is_refused_before_anything_is_asked():
    drafts, client, _pool = lists(ABSENT)
    with pytest.raises(ValueError):
        drafts.save(["not a relay"], lambda _outcome: None)
    assert client.requests == []


# -- drafts left behind (review N4, N5) -------------------------------------------------

def reading_relays(directory):
    from nostr.outbox import ask_draft_relays
    answers = []
    ask_draft_relays(directory, Profile(), answers.append, reading=True)
    settle()
    return answers[0]


def test_a_relay_left_that_goes_quiet_after_the_list_went_out_is_read_on():
    # A draft saved on MINE while the list is signed, and MINE stops
    # answering then: the change stands, MINE is read on, and the sheet
    # is told which relay may still hold drafts.
    drafts, relays, directory, _client = moving(LIST_ON_MINE,
                                                drafts_on={MINE: [wrap("draft-1")]})
    late = wrap("draft-2", at=NOW - 5)
    outcomes = []

    def step(name, *_counts):
        if name == "saving_list":
            relays.keep(MINE, late)
            relays.down = {MINE}

    drafts.save([OTHER], outcomes.append, on_step=step)
    relays.run(rounds=30)
    assert outcomes[0].ok and outcomes[0].left_behind == (MINE,)
    assert ([OTHER], late) not in sent(relays, 31234)
    assert directory.draft_relays_of(PK) == [OTHER]
    left = directory.drafts_left_behind(PK)
    assert list(left) == [MINE] and left[MINE] is not None and left[MINE] < NOW
    assert MINE in reading_relays(directory)


def test_a_list_changed_on_another_device_leaves_the_old_draft_relays_read():
    drafts, relays, directory, client = moving(LIST_ON_MINE)
    elsewhere = signed(KIND_PRIVATE_RELAYS, [], 'sealed:[["relay","wss://else.example"]]',
                       created_at=NOW - 50)
    drafts._query.answers[KIND_PRIVATE_RELAYS] = found(elsewhere)
    read = []
    drafts.read(read.append)
    assert read == [["wss://else.example"]]
    assert directory.draft_relays_of(PK) == ["wss://else.example"]   # followed at once
    assert directory.drafts_left_behind(PK) == {MINE: None}
    assert MINE in reading_relays(directory)


def test_the_same_list_read_again_is_not_decrypted_again():
    class Counting(SealingClient):
        decrypted = 0

        def nip44_decrypt_self(self, ciphertext, on_success, on_failure):
            Counting.decrypted += 1
            super().nip44_decrypt_self(ciphertext, on_success, on_failure)

    drafts, _client, _pool = lists(LIST_ON_MINE, Counting())
    got = []
    drafts.read(got.append)
    drafts.read(got.append)
    assert got == [[MINE], [MINE]] and Counting.decrypted == 1


def test_drafts_left_behind_are_moved_and_then_no_longer_read():
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    draft = wrap("draft-1")
    drafts, relays, directory, client = moving(LIST_ON_MINE, drafts_on={MINE: [draft]})
    directory.follow_draft_relays(                          # learned from another device
        PK, [OTHER], event=signed(KIND_PRIVATE_RELAYS, [], "sealed:", created_at=NOW - 10))
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(client), store=DraftStore(),
                     read_draft_list=lambda _profile, on_done: on_done([OTHER]))
    sync._profile = Profile()
    relays.down = {MINE}
    sync._move_drafts_left_behind()                        # MINE is quiet: tried again later
    relays.run(rounds=10)
    assert sent(relays, 31234) == [] and directory.drafts_left_behind(PK) == {MINE: None}
    relays.down = set()
    sync._move_drafts_left_behind()
    relays.run(rounds=10)
    assert sent(relays, 31234) == [([OTHER], draft)]
    assert directory.drafts_left_behind(PK) == {} and MINE not in reading_relays(directory)
    sync.stop()
    sync.deleteLater()
    settle()


def test_drafts_go_to_the_usual_relays_while_every_draft_relay_asks_for_a_sign_in():
    # Set by another app: a draft relay that shows drafts only after a
    # sign-in that did not work (the signer app declined it, say). A draft
    # saved there would be read back by no device.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    from nostr.outbox import ask_draft_relays
    on_locked = found(signed(KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{LOCKED}"]]'))
    _drafts, relays, directory, client = moving(on_locked)
    relays.write_only = {LOCKED}
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(client), store=DraftStore(),
                     read_draft_list=lambda _profile, on_done: on_done([LOCKED]))
    said = []
    sync.status_changed.connect(said.append)
    sync._profile = Profile()

    def read_drafts():
        sync._read_relays = reading_relays(directory)
        sync._store.set_loading(True)
        sync._open_subscription()
        relays.run(rounds=5)

    def writing():
        answers = []
        ask_draft_relays(directory, Profile(), answers.append)
        settle()
        return answers[0]

    read_drafts()
    assert LOCKED not in writing() and set(writing()) >= {U1, U2}
    assert said[-1] == ("Drafts kept where you chose stay hidden until signing in there works: "
                        "new drafts are saved with your posts meanwhile.")
    relays.write_only = set()                              # it shows drafts now
    read_drafts()
    assert writing() == [LOCKED]
    sync.stop()
    sync.deleteLater()
    settle()


# -- the newest list known (review L5) ---------------------------------------------------

class CountingClient(SealingClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.decrypted = 0

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure):
        self.decrypted += 1
        super().nip44_decrypt_self(ciphertext, on_success, on_failure)


def another_reader(sheet_lists, relays, directory, client):
    """A second PrivateDraftRelays for the account (as DraftSync keeps)."""
    return PrivateDraftRelays(profile=Profile(), pool=relays,
                              session_pool=FakeSessionPool(client), directory=directory,
                              query=sheet_lists._query, clock=lambda: NOW)


def test_a_list_this_device_wrote_is_not_decrypted_again_by_another_reader():
    lists, relays, directory, _client = moving(LIST_ON_MINE)
    client = CountingClient()
    reader = another_reader(lists, relays, directory, client)
    reader.read(lambda _relays: None)
    out = []
    lists.save([OTHER], out.append)
    relays.run(rounds=20)
    written = [event for _urls, event in relays.published
               if event["kind"] == KIND_PRIVATE_RELAYS][-1]
    lists._query.answers[KIND_PRIVATE_RELAYS] = found(written)
    before = client.decrypted
    got = []
    reader.read(got.append)
    assert out[0].ok and got == [[OTHER]] and client.decrypted == before   # no signer prompt


def test_a_late_answer_with_an_older_list_does_not_undo_a_save():
    lists, relays, directory, _client = moving(LIST_ON_MINE)
    reader = another_reader(lists, relays, directory, CountingClient())
    held = []
    reader._query = lambda _pool, _relays, **kw: held.append(kw["on_done"])
    reader.read(lambda _relays: None)                       # asked before the save ...
    out = []
    lists.save([OTHER], out.append)
    relays.run(rounds=20)
    held[0](LIST_ON_MINE)                                   # ... answered after it
    settle()
    assert out[0].ok and directory.draft_relays_of(PK) == [OTHER]
    assert directory.drafts_left_behind(PK) == {}


def test_a_list_the_signer_declined_is_not_asked_about_on_every_refresh():
    # Review L7: every five minutes the signer app was asked again.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    _lists, relays, directory, _client = moving(ABSENT)
    client = CountingClient(refuse=True)
    query = FakeQuery({KIND_PRIVATE_RELAYS: LIST_ON_MINE})
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(client), store=DraftStore())
    sync._profile = Profile()
    sync._draft_list_reader = PrivateDraftRelays(
        profile=Profile(), pool=relays, session_pool=FakeSessionPool(client),
        directory=directory, query=query, parent=sync)
    for _refresh in range(3):
        sync._follow_draft_list()
        settle()
    assert client.decrypted == 1
    sync.refresh_by_hand()                                  # the person asks: ask again
    settle()
    assert client.decrypted == 2
    sync.stop()
    sync.deleteLater()
    settle()


def test_a_relay_left_behind_that_is_gone_does_not_hold_back_the_others():
    # Review M1 (a): the list [K1, K2] was changed elsewhere to [K3]; K1 is
    # gone for good, K2 holds a draft this device saved there meanwhile.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    k1, k2, k3 = "wss://k1.example", "wss://k2.example", "wss://k3.example"
    late = wrap("draft-9")
    on_both = found(signed(KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{k1}"],["relay","{k2}"]]'))
    _lists, relays, directory, client = moving(on_both, drafts_on={k2: [late]})
    directory.follow_draft_relays(
        PK, [k3], event=signed(KIND_PRIVATE_RELAYS, [], "sealed:", created_at=NOW - 10))
    relays.down = {k1}
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(client), store=DraftStore(),
                     read_draft_list=lambda _profile, on_done: on_done([k3]))
    sync._profile = Profile()
    sync._move_drafts_left_behind()
    relays.run(rounds=10)
    assert ([k3], late) in sent(relays, 31234)
    assert directory.drafts_left_behind(PK) == {k1: None}        # still read, still asked
    assert k1 in reading_relays(directory) and k2 not in reading_relays(directory)
    sync.stop()
    sync.deleteLater()
    settle()


def test_a_relay_left_behind_is_read_until_it_answers_however_long(monkeypatch):
    # Review M1 (b): after 30 days it was dropped without a word, drafts and all.
    import time
    _lists, relays, directory, _client = moving(LIST_ON_MINE)
    directory.leave_drafts_behind(PK, [MINE], since=None)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 400 * 86_400)
    assert directory.drafts_left_behind(PK) == {MINE: None} and MINE in reading_relays(directory)


def test_each_relay_left_behind_keeps_its_own_date():
    # Review M1 (c): one shared record gave a relay left on day 29 the date
    # of one left on day 0.
    k1, k2 = "wss://k1.example", "wss://k2.example"
    _lists, _relays, directory, _client = moving(LIST_ON_MINE)
    directory.leave_drafts_behind(PK, [k1], since=NOW - 900)
    directory.leave_drafts_behind(PK, [k2], since=NOW - 100)
    directory.leave_drafts_behind(PK, [k1], since=NOW - 50)        # the earlier date stays
    assert directory.drafts_left_behind(PK) == {k1: NOW - 900, k2: NOW - 100}


def test_a_slow_decrypt_answered_after_a_save_here_does_not_undo_it():
    # Review L9: the read refused the older list, and DraftSync set it anyway.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    from nostr.outbox import ask_draft_relays
    elsewhere_relay = "wss://else.example"
    held = []

    class Holding(SealingClient):
        def nip44_decrypt_self(self, ciphertext, on_success, on_failure):
            held.append(lambda: SealingClient.nip44_decrypt_self(
                self, ciphertext, on_success, on_failure))

    lists, relays, directory, _client = moving(LIST_ON_MINE)
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(Holding()), store=DraftStore())
    sync._profile = Profile()
    sync._draft_list_reader = PrivateDraftRelays(
        profile=Profile(), pool=relays, session_pool=FakeSessionPool(Holding()),
        directory=directory, query=lists._query, parent=sync)
    elsewhere = signed(KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{elsewhere_relay}"]]',
                       created_at=NOW - 60)
    lists._query.answers[KIND_PRIVATE_RELAYS] = found(elsewhere)
    sync._follow_draft_list()                               # the decrypt waits ...
    settle()
    lists._known, lists._read_id = [elsewhere_relay], elsewhere["id"]
    directory.follow_draft_relays(PK, [elsewhere_relay], event=elsewhere)
    out = []
    lists.save([OTHER], out.append)                         # ... while the sheet saves here
    relays.run(rounds=20)
    held[0]()
    settle()
    answers = []
    ask_draft_relays(directory, Profile(), answers.append)
    settle()
    assert out[0].ok and directory.draft_relays_of(PK) == [OTHER] and answers == [[OTHER]]
    sync.stop()
    sync.deleteLater()
    settle()


def test_a_signer_app_that_could_not_be_reached_is_asked_again_at_the_next_refresh():
    # Review L10: "unreachable" was remembered as "declined", so a list
    # changed while this device was off was not followed until a refresh
    # by hand. A real decline is still remembered.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync

    class Waking(FakeSessionPool):
        up = False

        def get(self, profile, on_ready, on_error):
            if self.up:
                on_ready(self.client)
            else:
                on_error("signer app did not answer")

    lists, relays, directory, _client = moving(LIST_ON_MINE)
    elsewhere = signed(KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{OTHER}"]]',
                       created_at=NOW - 60)
    lists._query.answers[KIND_PRIVATE_RELAYS] = found(elsewhere)
    sessions = Waking(SealingClient())
    sync = DraftSync(relay_pool=relays, relay_directory=directory, session_pool=sessions,
                     store=DraftStore())
    sync._profile = Profile()
    sync._draft_list_reader = PrivateDraftRelays(
        profile=Profile(), pool=relays, session_pool=sessions, directory=directory,
        query=lists._query, parent=sync)
    sync._follow_draft_list()                               # the phone is asleep
    settle()
    assert directory.draft_relays_of(PK) == [MINE]
    sessions.up = True
    sync._follow_draft_list()                               # the next automatic refresh
    settle()
    assert directory.draft_relays_of(PK) == [OTHER]
    sync.stop()
    sync.deleteLater()
    settle()


def test_a_signer_app_that_comes_back_reads_the_list_at_once():
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync

    class Waking(FakeSessionPool):
        up = False

        def get(self, profile, on_ready, on_error):
            if self.up:
                on_ready(self.client)
            else:
                on_error("signer app did not answer")

    lists, relays, directory, client = moving(LIST_ON_MINE)
    elsewhere = signed(KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{OTHER}"]]',
                       created_at=NOW - 60)
    lists._query.answers[KIND_PRIVATE_RELAYS] = found(elsewhere)
    sessions = Waking(client)
    sync = DraftSync(relay_pool=relays, relay_directory=directory, session_pool=sessions,
                     store=DraftStore())
    sync._profile = Profile()
    sync._draft_list_reader = PrivateDraftRelays(
        profile=Profile(), pool=relays, session_pool=sessions, directory=directory,
        query=lists._query, parent=sync)
    sync._follow_draft_list()                               # the phone is asleep
    settle()
    sessions.up = True
    sync._on_bunker_ready(sync._generation, client)         # the signer app answers now
    settle()
    assert directory.draft_relays_of(PK) == [OTHER]
    sync.stop()
    sync.deleteLater()
    settle()


def test_a_signer_app_that_comes_back_is_not_asked_twice_for_a_list_being_read():
    # Review round 3, L15: start-up asked the person to approve the same
    # list twice when the first approval took longer than the session.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    held = []

    class Holding(SealingClient):
        def nip44_decrypt_self(self, ciphertext, on_success, on_failure):
            held.append(lambda: SealingClient.nip44_decrypt_self(
                self, ciphertext, on_success, on_failure))

    lists, relays, directory, _client = moving(LIST_ON_MINE)
    elsewhere = signed(KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{OTHER}"]]',
                       created_at=NOW - 60)
    lists._query.answers[KIND_PRIVATE_RELAYS] = found(elsewhere)
    client = Holding()
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(client), store=DraftStore())
    sync._profile = Profile()
    sync._draft_list_reader = PrivateDraftRelays(
        profile=Profile(), pool=relays, session_pool=FakeSessionPool(client),
        directory=directory, query=lists._query, parent=sync)
    sync._follow_draft_list()                               # start-up: the request waits
    settle()
    sync._on_bunker_ready(sync._generation, client)
    settle()
    assert len(held) == 1
    held[0]()
    settle()
    assert directory.draft_relays_of(PK) == [OTHER]
    sync.stop()
    sync.deleteLater()
    settle()


def test_a_relay_left_behind_that_never_answers_does_not_hold_the_loading_line():
    # Review L11: a relay that drops packets kept "Refreshing drafts…" up
    # for a minute at every refresh.
    from nostr.draft_store import DraftStore
    from nostr.draft_sync import DraftSync
    _lists, relays, directory, client = moving(LIST_ON_MINE)
    directory.leave_drafts_behind(PK, ["wss://dropping.example"], since=None)
    relays.silent = {"wss://dropping.example"}
    store = DraftStore()
    sync = DraftSync(relay_pool=relays, relay_directory=directory,
                     session_pool=FakeSessionPool(client), store=store)
    said = []
    sync.status_changed.connect(said.append)
    sync._profile = Profile()
    store.bind_profile(PK)
    sync._read_relays = reading_relays(directory) + ["wss://dropping.example"]
    store.set_loading(True)
    sync._open_subscription()
    relays.run(rounds=5)
    assert not store.is_loading and said[-1].startswith("Loaded")
    sync.stop()
    sync.deleteLater()
    settle()


@pytest.mark.parametrize("first_failure, declined", [
    ("user rejected", True),                       # the signer app said no
    ("not connected", False),                      # this client's own session errors ...
    ("reopening channel", False),
    ("closed by client", False),
    ("dropped from session pool", False),
    ("application shutdown", False),
    ("user pubkey not yet known", False),
    ("timed out waiting for signer", False),       # ... and silence: asked again
])
def test_only_the_signer_apps_own_refusal_counts_as_declined(first_failure, declined):
    # Review round 3, L10: a session that reopened by itself was taken for
    # a refusal, and a list changed elsewhere was not followed.
    class OnceFailing(SealingClient):
        def __init__(self):
            super().__init__()
            self.tries = 0

        def nip44_decrypt_self(self, ciphertext, on_success, on_failure):
            self.tries += 1
            if self.tries == 1:
                on_failure(first_failure)
            else:
                super().nip44_decrypt_self(ciphertext, on_success, on_failure)

    client = OnceFailing()
    reader, _client, _pool = lists(found(signed(
        KIND_PRIVATE_RELAYS, [], f'sealed:[["relay","{OTHER}"]]')), client)
    for _refresh in range(3):
        reader.read(lambda _relays: None)
    assert (client.tries == 1) is declined


def test_asking_again_lets_the_accounts_relays_ask_for_a_sign_in_again():
    # Review round 2, R1: after the person said no to a sign-in, Try Again,
    # Save and Refresh Drafts may ask again; what MyEditor reads by itself
    # does not.
    drafts, relays, _directory, _client = moving(ABSENT)
    drafts._entitled = ["wss://members.example"]
    drafts.ask_again()
    assert relays.forgotten and {U1, U2, "wss://members.example"} <= set(relays.forgotten[-1])


def test_the_usual_relays_are_where_drafts_go_without_a_list_of_their_own():
    """Asked for the sheet, which shows them while no draft relays are
    chosen: the account's own relays, even when it has a list of draft
    relays (they are where drafts would go without one)."""
    drafts, _relays, _directory, _client = moving(LIST_ON_MINE)
    answers = []
    drafts.usual_relays(answers.append)
    settle()
    assert len(answers) == 1 and set(answers[0]) == {U1, U2}
