# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Following submissions (nostr/standup/watcher.py) against the fake
server: through the status keys and never a signature, at launch, when
the view opens and every 15 minutes, per account, and stopped on
sign-out (criterion 19 of the plan). And the rule for a request that got
no answer (contract 12.4): the lookup decides."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from nostr.standup import views
from nostr.standup.client import StandupApi
from nostr.standup.errors import Code
from nostr.standup.lists import SubmissionEntry
from nostr.standup.watcher import (
    REFRESH_INTERVAL_MS,
    Different,
    NotStored,
    Superseded,
    SubmissionWatcher,
    classify,
    read_submission,
    resolve_unconfirmed,
)
from tests.membership_fakes import FakeClock, FakeTimer
from tests.standup_fakes import (
    DROP_AFTER,
    DROP_BEFORE,
    SERVER,
    Caller,
    FakeStandupServer,
    SignAs,
    StandupNam,
    submission_for,
)
from tests.standup_vectors import NOW, sk


def never_sign(unsigned, on_success, on_failure):
    raise AssertionError("following a submission must not ask the signer app")


class World:
    def __init__(self):
        self.clock = FakeClock(NOW)
        self.server = FakeStandupServer(self.clock)
        self.nam = StandupNam(responder=self.server)
        self.writer = StandupApi(SignAs(sk("writer")), server=SERVER, nam=self.nam, clock=self.clock)
        self.reader = StandupApi(never_sign, server=SERVER, nam=self.nam, clock=self.clock)
        self.entries = []

    def submit(self, n=0, **kw):
        prepared = submission_for(sk("writer"), d=f"text-{n}", **kw)
        outcome = []
        self.writer.submit(prepared.body, outcome.append, outcome.append)
        self.nam.settle()
        assert isinstance(outcome[0], views.Confirmed), outcome
        entry = entry_for(prepared)
        self.entries.append(entry)
        return prepared, entry

    def watcher(self, *, account="a" * 64, entries=None):
        timer = FakeTimer()
        watcher = SubmissionWatcher(account, entries or (lambda: list(self.entries)),
                                    lambda server: self.reader, timer=timer)
        updates, failures = [], []
        watcher.updated.connect(updates.append)
        watcher.failed.connect(failures.append)
        return watcher, timer, updates, failures

    def lookups(self):
        return [r for r in self.server.requests if r[1] == "/api/submissions/lookup"]


def entry_for(prepared) -> SubmissionEntry:
    body = prepared.body
    return SubmissionEntry(
        sid=body["submission_id"], publication=body["publication"], server=SERVER,
        role="writer", s_sub=prepared.s_sub.hex(), status_key=prepared.status_key,
        title=body["title"], kind=body["kind"], d="d", round=body["round"],
        revision=body["revision"], revision_hash=body["revision_hash"], updated_at=NOW)


# -- polling ---------------------------------------------------------------------

def test_launch_looks_at_once_without_a_signature_and_again_every_15_minutes():
    world = World()
    _prepared, entry = world.submit()
    watcher, timer, updates, _ = world.watcher()
    watcher.start()
    world.nam.settle()
    assert len(world.lookups()) == 1
    assert world.lookups()[0][3]["Authorization"] is None
    [update] = updates
    followed = update[entry.sid]
    assert followed.item.submission.state == "submitted" and followed.last_seq == 1
    assert timer.started[-1] == REFRESH_INTERVAL_MS
    timer.fire()
    world.nam.settle()
    assert len(world.lookups()) == 2


def test_opening_the_view_looks_at_once():
    world = World()
    world.submit()
    watcher, _timer, _updates, _ = world.watcher()
    watcher.view_opened()          # not started: nothing
    world.nam.settle()
    assert world.lookups() == []
    watcher.start()
    world.nam.settle()
    watcher.view_opened()
    world.nam.settle()
    assert len(world.lookups()) == 2


def test_only_what_is_new_is_asked_for():
    world = World()
    prepared, entry = world.submit()
    watcher, timer, updates, _ = world.watcher()
    watcher.start()
    world.nam.settle()
    world.server.submissions[entry.sid].state = "in_review"
    world.server._event(world.server.submissions[entry.sid], "taken", Caller("ee" * 32))
    timer.fire()
    world.nam.settle()
    sent = json.loads(world.lookups()[-1][2])
    assert sent == {"items": [{"status_key": prepared.status_key, "since": 1}]}
    followed = watcher.followed()[entry.sid]
    assert [i.seq for i in followed.timeline] == [1, 2]
    assert followed.item.submission.state == "in_review"


def test_more_than_50_submissions_are_asked_for_in_batches():
    world = World()
    entries = []
    for n in range(120):
        prepared = submission_for(sk("writer"), d=f"t-{n}")
        entries.append(entry_for(prepared))
    watcher, _timer, _updates, _ = world.watcher(entries=lambda: entries)
    watcher.start()
    world.nam.settle()
    sizes = [len(json.loads(r[2])["items"]) for r in world.lookups()]
    assert sizes == [50, 50, 20]


def test_contributors_entries_without_a_status_key_are_not_looked_up():
    world = World()
    _prepared, entry = world.submit()
    contributor = replace(entry, role="contributor", status_key=None)
    watcher, timer, updates, _ = world.watcher(entries=lambda: [contributor])
    watcher.start()
    world.nam.settle()
    assert world.lookups() == [] and updates == [] and timer.active


def test_a_round_asked_for_while_one_runs_follows_it():
    world = World()
    world.submit()
    watcher, _timer, _updates, _ = world.watcher()
    watcher.start()
    watcher.view_opened()
    world.nam.settle()
    assert len(world.lookups()) == 2


def test_a_server_that_cannot_be_asked_is_tried_again_next_round():
    world = World()
    world.submit()
    world.server.inject("lookup", DROP_BEFORE)
    watcher, timer, updates, failures = world.watcher()
    watcher.start()
    world.nam.settle()
    assert updates == [] and len(failures) == 1 and timer.active
    timer.fire()
    world.nam.settle()
    assert len(updates) == 1


# -- account scope (criterion 19) ----------------------------------------------------

def test_stop_ends_the_timer_ignores_answers_on_the_way_and_forgets():
    world = World()
    world.submit()
    watcher, timer, updates, _ = world.watcher()
    watcher.start()
    world.nam.settle()
    assert len(updates) == 1 and watcher.followed()
    timer.fire()                   # a round is on its way...
    watcher.stop()                 # ...when the person signs out
    world.nam.settle()
    assert len(updates) == 1 and watcher.followed() == {}
    assert not timer.active and not watcher.running
    watcher.view_opened()
    watcher.refresh()
    world.nam.settle()
    assert len(world.lookups()) == 2   # the one before, the one in flight; nothing after


def test_stopping_one_accounts_watcher_leaves_the_others_running():
    world = World()
    world.submit()
    first, first_timer, first_updates, _ = world.watcher(account="a" * 64)
    second, second_timer, second_updates, _ = world.watcher(account="b" * 64)
    first.start()
    second.start()
    world.nam.settle()
    first.stop()
    second_timer.fire()
    world.nam.settle()
    assert len(first_updates) == 1 and len(second_updates) == 2
    assert not first_timer.active and second_timer.active


# -- no answer after sending (12.4) ------------------------------------------------

def _resolve(world, entry):
    outcome = []
    resolve_unconfirmed(world.reader, entry, lambda r: outcome.append(("ok", r)),
                        lambda e: outcome.append(("err", e)))
    world.nam.settle()
    assert len(outcome) == 1
    return outcome[0]


def test_a_request_that_arrived_without_an_answer_is_confirmed_by_the_lookup():
    # Criterion 2: "Unbestätigt" resolves through the status key.
    world = World()
    world.server.inject("submit", DROP_AFTER)
    prepared = submission_for(sk("writer"))
    outcome = []
    world.writer.submit(prepared.body, outcome.append, outcome.append)
    world.nam.settle()
    assert outcome[0].maybe_stored
    kind, result = _resolve(world, entry_for(prepared))
    assert kind == "ok" and isinstance(result, views.Confirmed)
    assert result.receipt.revision_hash == prepared.body["revision_hash"]


def test_a_request_that_never_arrived_may_be_sent_again():
    world = World()
    world.server.inject("submit", DROP_BEFORE)
    prepared = submission_for(sk("writer"))
    world.writer.submit(prepared.body, lambda r: None, lambda e: None)
    world.nam.settle()
    kind, result = _resolve(world, entry_for(prepared))
    assert kind == "ok" and isinstance(result, NotStored) and result.receipt is None


def test_a_revision_that_did_not_arrive_while_the_one_before_did():
    world = World()
    first, _entry = world.submit()
    second = submission_for(sk("writer"), d="text-0", revision=2, content="Neu.",
                            s_sub=first.s_sub, status_key=first.status_key)
    kind, result = _resolve(world, entry_for(second))
    assert kind == "ok" and isinstance(result, NotStored) and result.receipt.revision == 1


def test_a_later_or_a_different_revision_on_the_server_is_told_apart():
    world = World()
    first, entry = world.submit()
    second = submission_for(sk("writer"), d="text-0", revision=2, content="Neu.",
                            s_sub=first.s_sub, status_key=first.status_key)
    world.writer.submit(second.body, lambda r: None, lambda e: None)
    world.nam.settle()
    kind, result = _resolve(world, entry)
    assert kind == "ok" and isinstance(result, Superseded)
    other = replace(entry_for(second), revision_hash="99" * 32)
    kind, result = _resolve(world, other)
    assert kind == "ok" and isinstance(result, Different)


def test_files_missing_after_no_answer_are_named():
    world = World()
    blob = {"sha256": "cd" * 32, "size": 3, "servers": ["https://blossom.example"]}
    world.server.inject("submit", DROP_AFTER)
    prepared = submission_for(sk("writer"), blobs=(blob,))
    world.writer.submit(prepared.body, lambda r: None, lambda e: None)
    world.nam.settle()
    kind, result = _resolve(world, entry_for(prepared))
    assert kind == "ok" and isinstance(result, views.BlobsMissing)
    assert result.missing == ("cd" * 32,)


def test_when_the_server_cannot_be_asked_the_entry_stays_unconfirmed():
    world = World()
    _prepared, entry = world.submit()
    world.server.inject("lookup", DROP_BEFORE)
    kind, error = _resolve(world, entry)
    assert kind == "err" and error.code == "offline"


def test_only_a_writers_entry_can_be_resolved():
    world = World()
    _prepared, mine = world.submit()
    with pytest.raises(ValueError):
        resolve_unconfirmed(world.reader, replace(mine, role="contributor", status_key=None),
                            print, print)
    assert world.lookups() == []


def test_an_answer_that_cannot_be_read_leaves_the_entry_unconfirmed():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    prepared = submission_for(sk("writer"))
    world.writer.submit(prepared.body, lambda r: None, lambda e: None)
    world.nam.settle()
    original = world.server._lookup

    def broken(who, body, params, query):
        reply = original(who, body, params, query)
        data = json.loads(reply._body)
        data["items"][0]["receipt"]["state"] = "lost"
        reply._body = json.dumps(data).encode("utf-8")
        return reply

    world.server._lookup = broken
    kind, error = _resolve(world, entry_for(prepared))
    assert kind == "err" and error.code == Code.BAD_RESPONSE and error.maybe_stored


# -- what a receipt says about an entry (RG-M1 5) ----------------------------------

def test_an_entry_recorded_for_a_revision_that_never_arrived_is_not_shown_as_submitted():
    world = World()
    prepared, entry = world.submit()
    unsent = replace(entry, revision=2, revision_hash="ab" * 32)
    watcher, _timer, updates, _ = world.watcher(entries=lambda: [unsent])
    watcher.start()
    world.nam.settle()
    followed = updates[-1][entry.sid]
    assert isinstance(followed.status, NotStored)
    assert followed.status.receipt.revision == 1


def test_the_watcher_and_the_no_answer_rule_read_a_receipt_alike():
    world = World()
    _prepared, entry = world.submit()
    watcher, _timer, updates, _ = world.watcher()
    watcher.start()
    world.nam.settle()
    followed = updates[-1][entry.sid]
    assert isinstance(followed.status, views.Confirmed)
    for recorded, kind in ((entry, views.Confirmed),
                           (replace(entry, revision_hash="ab" * 32), Different),
                           (replace(entry, revision=2), NotStored),
                           (replace(entry, revision=0), Superseded)):
        assert isinstance(classify(recorded, followed.item.receipt), kind)
        _ok, resolved = _resolve(world, recorded)
        assert isinstance(resolved, kind)


# -- more than one answer holds (contract 15, 5c11475) ----------------------------

def test_a_round_continues_while_the_server_says_more():
    world = World()
    prepared, entry = world.submit()
    record = world.server.submissions[entry.sid]
    for seq in range(2, 8):
        record.timeline.append(dict(record.timeline[0], seq=seq))
    world.server.timeline_per_answer["lookup"] = 3
    watcher, _timer, updates, failures = world.watcher()
    watcher.start()
    world.nam.settle()
    assert [i.seq for i in watcher.followed()[entry.sid].timeline] == list(range(1, 8))
    assert len(world.lookups()) == 3 and len(updates) == 1 and failures == []


def test_reading_a_submission_continues_while_the_server_says_more():
    world = World()
    prepared, entry = world.submit()
    record = world.server.submissions[entry.sid]
    for seq in range(2, 6):
        record.timeline.append(dict(record.timeline[0], seq=seq))
    world.server.timeline_per_answer["get"] = 2
    outcome = []
    read_submission(world.writer, entry.sid, outcome.append, outcome.append)
    world.nam.settle()
    [detail] = outcome
    assert [i.seq for i in detail.timeline] == [1, 2, 3, 4, 5] and not detail.more
    assert [r.revision for r in detail.revisions] == [1]


# -- one byte budget for every item of a lookup (RG-M2a 1, backend fde04a3) -----------

def _grow(world, entry, last):
    record = world.server.submissions[entry.sid]
    for seq in range(len(record.timeline) + 1, last + 1):
        record.timeline.append(dict(record.timeline[0], seq=seq))
    record.seq = last


def _item_bytes(world, entry):
    item = world.server.submissions[entry.sid].timeline[0]
    return len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode())


def test_a_submission_behind_one_that_took_the_budget_is_asked_again_not_failed():
    world = World()
    _p, first = world.submit(0)
    _p, second = world.submit(1)
    _grow(world, first, 5)
    _grow(world, second, 3)
    # Room for three items an answer: the first takes it all, the second waits.
    world.server.timeline_bytes = 3 * max(_item_bytes(world, first), _item_bytes(world, second))
    watcher, _timer, updates, failures = world.watcher()
    watcher.start()
    world.nam.settle()
    assert failures == []
    followed = watcher.followed()
    assert [i.seq for i in followed[first.sid].timeline] == [1, 2, 3, 4, 5]
    assert [i.seq for i in followed[second.sid].timeline] == [1, 2, 3]
    asked = [[item["status_key"] == first.status_key for item in json.loads(r[2])["items"]]
             for r in world.lookups()]
    assert asked[1][0] is False                   # the one that waited is served first
    assert len(updates) == 1


def test_a_submission_that_gets_nothing_with_nothing_ahead_of_it_fails():
    world = World()
    world.submit(0)
    world.submit(1)
    world.server.timeline_bytes = 1               # not one item fits: a server holding it
    watcher, _timer, _updates, failures = world.watcher()
    watcher.start()
    world.nam.settle()
    assert [f.message for f in failures] == ["more was promised and nothing came"] * 2
    assert len(world.lookups()) == 1


def test_answers_that_keep_saying_more_end_a_round_and_a_reading_alike():
    # One rule (RG-M2a 8): the watcher no longer ends silently at the cap.
    from nostr.standup.watcher import MAX_CONTINUATIONS
    world = World()
    _p, entry = world.submit()
    _grow(world, entry, MAX_CONTINUATIONS + 5)
    world.server.timeline_per_answer = {"lookup": 1, "get": 1}
    watcher, _timer, _updates, failures = world.watcher()
    watcher.start()
    world.nam.settle()
    too_long = f"more was still promised after {MAX_CONTINUATIONS} answers"
    assert [f.message for f in failures] == [too_long]
    assert len(world.lookups()) == MAX_CONTINUATIONS + 1
    outcome = []
    read_submission(world.writer, entry.sid, outcome.append, outcome.append)
    world.nam.settle()
    assert [o.message for o in outcome] == [too_long]
