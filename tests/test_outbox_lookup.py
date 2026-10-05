# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lookup itself (nostr/outbox/lookup.py), relay by relay.

A lookup ends when every relay asked has ended: answered (EOSE),
refused (CLOSED) or could not be reached, and at the latest on time.
Only answers count toward ABSENT, and only validly signed events toward
FOUND.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.outbox import lookup  # noqa: E402
from nostr.outbox.policy import LookupState  # noqa: E402
from nostr.relay import Subscription  # noqa: E402
from tests.outbox_fakes import NOW, OTHER_SK, PK, HandPool, settle, signed  # noqa: E402

INDEXER = "wss://purplepag.es"
A = "wss://nos.lol"
B = "wss://relay.damus.io"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def ask(relays, *, timeout_ms=60_000, author=PK):
    pool = HandPool()
    results = []
    # Kept on the pool: without a parent, the query lives as long as a
    # reference to it does, as any QObject wrapper would.
    pool.query = lookup.fetch_replaceable(pool, relays, kind=10002, author=author,
                                          on_done=results.append, timeout_ms=timeout_ms)
    return pool, results


def test_it_waits_for_every_relay_then_counts_the_answers():
    pool, results = ask([INDEXER, A, B])
    sub = pool.subs[0]
    assert sub.filters == [{"kinds": [10002], "authors": [PK], "limit": 2}]
    sub.answer(INDEXER)
    sub.answer(A)
    assert results == []                     # B has not ended yet
    sub.answer(B)
    assert results[0].state is LookupState.ABSENT
    assert results[0].answered == (INDEXER, A, B)
    assert sub.is_closed


def test_a_refusal_ends_a_relay_but_is_no_answer():
    pool, results = ask([INDEXER, A])
    pool.subs[0].refuse(INDEXER, "auth-required: sign in first")
    pool.subs[0].answer(A)
    assert results[0].state is LookupState.UNKNOWN      # one answer is no quorum
    assert results[0].refused == (INDEXER,)


def test_unreachable_relays_end_the_lookup_early():
    pool, results = ask([INDEXER, A, B])
    pool.subs[0].answer(INDEXER)
    pool.subs[0].answer(A)
    pool.subs[0].fail(B)
    assert results[0].state is LookupState.ABSENT       # two answers, one an indexer
    assert results[0].refused == (B,)


def test_a_relay_that_answers_after_refusing_still_refused():
    pool, results = ask([INDEXER, A])
    sub = pool.subs[0]
    sub.refuse(A)
    sub.relay_eose.emit(A)                   # a confused relay, after its CLOSED
    sub.answer(INDEXER)
    assert results[0].answered == (INDEXER,)
    assert results[0].state is LookupState.UNKNOWN


def test_silence_ends_on_time_as_unknown():
    pool, results = ask([INDEXER, A], timeout_ms=20)
    pool.subs[0].answer(INDEXER)
    assert results == []
    deadline = time.monotonic() + 2
    while not results and time.monotonic() < deadline:
        QApplication.processEvents()
    assert results[0].state is LookupState.UNKNOWN
    assert pool.subs[0].is_closed


def test_no_relays_is_unknown_without_a_request():
    pool, results = ask([])
    assert results == []                     # answered on the next turn, not inside the call
    settle()
    assert results[0].state is LookupState.UNKNOWN and pool.subs == []


def test_forged_candidates_never_win():
    real = signed(10002, [["r", "wss://real.com"]], created_at=NOW - 500)
    newer_forgery = dict(signed(10002, [["r", "wss://evil.com"]], created_at=NOW), sig="00" * 64)
    same_id_forgery = dict(real, tags=[["r", "wss://evil.com"]])   # real id and signature
    someone_else = signed(10002, [["r", "wss://other.com"]], sk=OTHER_SK, created_at=NOW)
    pool, results = ask([A, B])
    pool.subs[0].answer(A, newer_forgery, same_id_forgery, someone_else)
    pool.subs[0].answer(B, real)
    assert results[0].state is LookupState.FOUND
    assert results[0].event == real


def test_a_string_timestamp_does_not_compete():
    real = signed(10002, [["r", "wss://real.com"]], created_at=NOW - 500)
    newer = signed(10002, [["r", "wss://newer.com"]], created_at=NOW)
    pool, results = ask([A])
    pool.subs[0].answer(A, dict(newer, created_at=str(NOW)), real)
    assert results[0].event == real


def test_a_flood_of_forgeries_makes_it_unknown_not_absent():
    pool, results = ask([INDEXER, A])
    sub = pool.subs[0]
    for i in range(lookup.VERIFY_CAP + 5):
        forged = dict(signed(10002, [["r", f"wss://x{i}.com"]], created_at=NOW + i),
                      sig="00" * 64)
        sub.event.emit(forged)
    sub.answer(INDEXER)
    sub.answer(A)
    assert results[0].state is LookupState.UNKNOWN


def test_the_subscription_is_let_go_of():
    pool, results = ask([A])
    sub = pool.subs[0]
    destroyed = []
    sub.destroyed.connect(lambda *_: destroyed.append(True))
    sub.answer(A)
    settle()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert destroyed == [True]



def test_a_finished_query_is_let_go_of_by_its_parent_and_goes_once():
    owner = QObject()
    pool, results = HandPool(), []
    query = lookup.fetch_replaceable(pool, [A], kind=10002, author=PK,
                                     on_done=results.append, parent=owner)
    gone = []
    query.destroyed.connect(lambda *_: gone.append(True))
    pool.subs[0].answer(A)
    settle()
    assert len(results) == 1
    assert query.parent() is None              # its delete is the only way it goes
    del owner                                  # the parent goes before the event loop runs
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert gone == [True]

# -- the real Subscription reports each relay's end once ------------------------------------

class StubRelay(QObject):
    connected = Signal()
    disconnected = Signal()
    message = Signal(list)
    error = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.is_connected = False
        self.sent = []

    def open(self):
        pass

    def send(self, message):
        self.sent.append(message)
        return self.is_connected


class StubPool:
    def __init__(self):
        self.relays = {}

    def get_or_create(self, url):
        return self.relays.setdefault(url, StubRelay(url))


def test_the_subscription_reports_how_each_relay_ended():
    pool = StubPool()
    sub = Subscription(pool, ["wss://a.com", "wss://b.com", "wss://c.com"],
                       [{"kinds": [10002]}])
    ended = []
    sub.relay_eose.connect(lambda url: ended.append(("eose", url)))
    sub.relay_closed.connect(lambda url, reason: ended.append(("closed", url)))
    sub.relay_failed.connect(lambda url, reason: ended.append(("failed", url)))
    a, b, c = (pool.relays[u] for u in ("wss://a.com", "wss://b.com", "wss://c.com"))

    a.error.emit("host not found")
    a.disconnected.emit()                    # follows the error: reported once
    b.is_connected = True
    b.connected.emit()
    assert b.sent == [["REQ", sub.sub_id, {"kinds": [10002]}]]
    b.message.emit(["EOSE", sub.sub_id])
    b.disconnected.emit()                    # after answering: nothing to report
    c.is_connected = True
    c.connected.emit()
    c.message.emit(["CLOSED", sub.sub_id, "auth-required: sign in"])
    assert ended == [("failed", "wss://a.com"), ("eose", "wss://b.com"),
                     ("closed", "wss://c.com")]
    sub.close()
