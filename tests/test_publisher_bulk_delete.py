# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deleting a pile of drafts, one signature at a time.

Every deletion is separately signed, so a bulk run is a row of approval
prompts on the user's phone. That shapes all of this: the requests go out
one at a time and in the order the user is looking at, the run can be
stopped between them, and what is reported at the end is what actually
happened rather than what was asked for.

The property worth the most here is the last one. A partial result
reported as a whole one is how somebody comes to believe a draft is gone
when it is still sitting on their relays.
"""

from __future__ import annotations

import sys
from typing import List, Tuple
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication

from nostr.publisher import DraftBulkDeleteJob

PK = "a" * 64
OK = [("wss://r/", True, "ok")]
REFUSED = [("wss://r/", False, "blocked: pubkey not allowed")]


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def profile():
    p = MagicMock()
    p.user_pubkey = PK
    p.bunker_pubkey = "b" * 64
    p.bunker_relays = ["wss://bunker.test/"]
    p.local_secret_hex = "0" * 64
    return p


class Signer:
    """A stand-in signer whose answers the test decides, in order.

    ``script`` is one entry per draft: "ok", "refused" (relays reject),
    or a string to fail with. ``defer`` holds each request open instead
    of answering, which is how the one-at-a-time property is observed.
    """

    def __init__(self, script=(), *, defer=False):
        self.script = list(script)
        self.defer = defer
        self.signed: List[str] = []
        self.pending: List[Tuple[str, callable, callable]] = []
        self.relays_seen: List[List[str]] = []

    # -- the seams DraftDeleteJob uses ---------------------------------
    def relay_list_cache(self):
        cache = MagicMock()
        cache.fetch.side_effect = (
            lambda pubkey, relays, on_done: on_done(MagicMock(write=[], read=[]))
        )
        return cache

    def session_pool(self):
        pool = MagicMock()
        pool.get.side_effect = (
            lambda profile, on_ready, on_error: on_ready(self.client())
        )
        return pool

    def client(self):
        client = MagicMock()

        def sign(unsigned, on_success, on_failure):
            identifier = next(
                (t[1] for t in unsigned.get("tags", []) if t and t[0] == "d"), "")
            self.signed.append(identifier)
            if self.defer:
                self.pending.append((identifier, on_success, on_failure))
                return
            self._answer(identifier, on_success, on_failure)

        client.sign_event.side_effect = sign
        return client

    def _answer(self, identifier, on_success, on_failure):
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome in ("ok", "refused"):
            on_success({"id": f"evt-{identifier}", "kind": 31234,
                        "created_at": 1, "pubkey": PK, "tags": [], "content": ""})
        else:
            on_failure(outcome)

    def release_next(self):
        identifier, on_success, on_failure = self.pending.pop(0)
        self._answer(identifier, on_success, on_failure)

    def relay_pool(self):
        pool = MagicMock()

        def publish(relays, event, **kw):
            self.relays_seen.append(list(relays))
            job = MagicMock()
            # The relay verdict pairs with the signature that produced it.
            results = REFUSED if self._next_publish_refused() else OK
            job.all_done.connect.side_effect = lambda cb: cb(results)
            return job

        pool.publish.side_effect = publish
        return pool

    def _next_publish_refused(self):
        return self._refused_flag

    _refused_flag = False


def run(targets, *, script=(), entitled=(), defer=False):
    signer = Signer(script, defer=defer)
    job = DraftBulkDeleteJob(
        relay_pool=signer.relay_pool(),
        relay_list_cache=signer.relay_list_cache(),
        session_pool=signer.session_pool(),
        profile=profile(),
        targets=targets,
        entitled_relays=entitled,
    )
    seen = {"finished": [], "progress": [], "tombstoned": []}
    job.finished.connect(lambda d, f: seen["finished"].append((d, list(f))))
    job.progress.connect(lambda d, t: seen["progress"].append((d, t)))
    job.tombstoned.connect(lambda d, e: seen["tombstoned"].append(d))
    return job, signer, seen


def targets(n=3, kind=1):
    return [(f"d{i}", kind) for i in range(n)]


# --------------------------------------------------------------------- #
# One at a time, in order                                               #
# --------------------------------------------------------------------- #

def test_only_one_signature_is_requested_at_a_time():
    # The whole reason this is sequential: N prompts fired at once land
    # on the user's phone in an order nobody chose.
    job, signer, _ = run(targets(4), defer=True)
    job.start()
    assert signer.signed == ["d0"]

    signer.release_next()
    assert signer.signed == ["d0", "d1"]


def test_they_go_in_the_order_they_were_given():
    job, signer, _ = run(targets(4))
    job.start()
    assert signer.signed == ["d0", "d1", "d2", "d3"]


def test_a_synchronous_signer_does_not_recurse_per_draft():
    # A cached session answers in the same call stack. Two hundred drafts
    # deep is a stack overflow, not a slow deletion.
    job, signer, seen = run(targets(200))
    job.start()
    assert len(signer.signed) == 200
    assert seen["finished"] == [(200, [])]


def test_every_draft_is_reported_as_it_settles():
    job, _signer, seen = run(targets(3))
    job.start()
    assert seen["progress"] == [(1, 3), (2, 3), (3, 3)]


def test_each_deletion_is_announced_so_the_row_can_go():
    job, _signer, seen = run(targets(3))
    job.start()
    assert seen["tombstoned"] == ["d0", "d1", "d2"]


# --------------------------------------------------------------------- #
# A partial result is reported as one                                   #
# --------------------------------------------------------------------- #

def test_one_failure_does_not_end_the_run():
    # The user asked for all of them to go. The ones that can, should.
    job, signer, seen = run(targets(4), script=["ok", "signer refused", "ok", "ok"])
    job.start()
    assert len(signer.signed) == 4
    deleted, failures = seen["finished"][0]
    assert deleted == 3 and len(failures) == 1
    assert failures[0][0] == "d1"


def test_a_failure_does_not_strand_the_drafts_behind_it():
    # Deferred on purpose. With a signer that answers in the same call
    # stack the pump loop carries on by itself, so a bug that stops the
    # queue after a failure is invisible; every real signer answers
    # later, which is the path this covers.
    job, signer, _seen = run(targets(3), script=["denied", "ok", "ok"], defer=True)
    job.start()
    assert signer.signed == ["d0"]

    signer.release_next()
    assert signer.signed == ["d0", "d1"], "the run stopped at the first failure"
    signer.release_next()
    assert signer.signed == ["d0", "d1", "d2"]


def test_a_deferred_run_reports_once_everything_has_settled():
    job, signer, seen = run(targets(3), script=["denied", "ok", "denied"], defer=True)
    job.start()
    for _ in range(3):
        assert not seen["finished"], "reported before the run was over"
        signer.release_next()

    assert len(seen["finished"]) == 1
    deleted, failures = seen["finished"][0]
    assert deleted == 1 and len(failures) == 2


def test_the_failures_name_the_drafts_that_did_not_go():
    job, _signer, seen = run(
        targets(3), script=["denied by user", "ok", "denied by user"])
    job.start()
    _deleted, failures = seen["finished"][0]
    assert [ident for ident, _reason in failures] == ["d0", "d2"]
    assert all("denied" in reason for _ident, reason in failures)


def test_a_deletion_no_relay_accepted_is_not_counted_as_deleted():
    # ``completed`` fires even when every relay refused. Counting that as
    # a deletion tells the user a draft is gone from a place it is on.
    signer = Signer(["ok"])
    signer._refused_flag = True
    job = DraftBulkDeleteJob(
        relay_pool=signer.relay_pool(),
        relay_list_cache=signer.relay_list_cache(),
        session_pool=signer.session_pool(),
        profile=profile(), targets=targets(1),
    )
    out = []
    job.finished.connect(lambda d, f: out.append((d, list(f))))
    job.start()

    deleted, failures = out[0]
    assert deleted == 0 and len(failures) == 1
    assert "no relay accepted" in failures[0][1]


def test_finishing_is_announced_exactly_once():
    job, _signer, seen = run(targets(5), script=["ok", "no", "ok", "no", "ok"])
    job.start()
    assert len(seen["finished"]) == 1


def test_an_empty_run_finishes_rather_than_hanging():
    job, _signer, seen = run([])
    job.start()
    assert seen["finished"] == [(0, [])]


# --------------------------------------------------------------------- #
# Stopping                                                              #
# --------------------------------------------------------------------- #

def test_cancelling_stops_before_the_next_signature():
    job, signer, seen = run(targets(5), defer=True)
    job.start()
    signer.release_next()          # d0 completes
    assert signer.signed == ["d0", "d1"]

    job.cancel()
    assert seen["finished"], "a cancelled run still has to report"
    before = len(signer.signed)
    if signer.pending:
        signer.release_next()
    assert len(signer.signed) == before, "no further signature after cancel"


def test_a_cancelled_run_reports_what_actually_happened():
    # It cannot recall a deletion already signed and published, so the
    # count is what went, not what was asked for.
    job, signer, seen = run(targets(5), defer=True)
    job.start()
    signer.release_next()
    signer.release_next()
    job.cancel()

    deleted, _failures = seen["finished"][0]
    assert deleted == 2


def test_cancelling_twice_reports_once():
    job, _signer, seen = run(targets(3), defer=True)
    job.start()
    job.cancel()
    job.cancel()
    assert len(seen["finished"]) == 1


def test_cancelling_a_finished_run_changes_nothing():
    job, _signer, seen = run(targets(2))
    job.start()
    job.cancel()
    assert len(seen["finished"]) == 1


# --------------------------------------------------------------------- #
# Refusing what it cannot do, before it costs an approval               #
# --------------------------------------------------------------------- #

def test_a_foreign_inner_kind_is_refused_at_construction():
    # Kind 30024 is Habla's long-form draft. Discovering this on the
    # ninth draft has already cost the user eight approvals.
    with pytest.raises(ValueError, match="unsupported inner kind"):
        DraftBulkDeleteJob(
            relay_pool=MagicMock(), relay_list_cache=MagicMock(),
            session_pool=MagicMock(), profile=profile(),
            targets=[("d0", 1), ("d1", 30024)],
        )


def test_an_empty_identifier_is_refused_at_construction():
    with pytest.raises(ValueError, match="identifier"):
        DraftBulkDeleteJob(
            relay_pool=MagicMock(), relay_list_cache=MagicMock(),
            session_pool=MagicMock(), profile=profile(),
            targets=[("", 1)],
        )


def test_nothing_is_signed_when_construction_refuses():
    signer = Signer()
    with pytest.raises(ValueError):
        DraftBulkDeleteJob(
            relay_pool=signer.relay_pool(),
            relay_list_cache=signer.relay_list_cache(),
            session_pool=signer.session_pool(),
            profile=profile(), targets=[("d0", 1), ("d1", 30024)],
        )
    assert signer.signed == []


# --------------------------------------------------------------------- #
# Standing on entitled relays is not lost in the batch                  #
# --------------------------------------------------------------------- #

def test_entitled_relays_reach_every_deletion():
    entitled = ["wss://nostr.einundzwanzig.space"]
    job, signer, _ = run(targets(3), entitled=entitled)
    job.start()
    assert len(signer.relays_seen) == 3
    for relays in signer.relays_seen:
        assert entitled[0] in relays


def test_the_total_is_known_before_the_run_starts():
    # The progress dialog needs its maximum before the first prompt.
    job, _signer, _ = run(targets(7))
    assert job.total == 7
