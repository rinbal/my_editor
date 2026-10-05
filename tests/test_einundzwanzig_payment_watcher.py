# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the wait for a settled membership payment.

After paying, the user waits while the association learns of the
payment. Every check is a signed request, and with a phone signer every
signature may be a prompt, so the watcher's job is as much about when
NOT to ask as about asking: one request at a time, nothing after it was
stopped, nothing once an answer makes further checks pointless.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from nostr.einundzwanzig_api import (
    POLL_ATTEMPTS,
    POLL_INTERVAL_MS,
    ApiError,
    ErrorCode,
    MembershipApi,
    PaymentWatcher,
    parse_invoice,
)
from tests.membership_fakes import (
    SERVICE,
    PREFIX,
    FakeClock,
    FakeMembershipServer,
    FakeNam,
    FakeSigner,
    FakeTimer,
    data_reply,
    invoice_data,
    tag,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def production_association(monkeypatch):
    # A developer's shell may point the app at a test association.
    monkeypatch.delenv("MYEDITOR_MEMBERSHIP_UPSTREAM", raising=False)


UNPAID = parse_invoice(invoice_data(created=False))
PAID = parse_invoice(invoice_data(paid=True, created=False))
EXPIRED = parse_invoice(invoice_data(checkout_url=None, created=False))


class ScriptedApi:
    """Holds each refresh until the test answers it."""

    def __init__(self):
        self.pending = []
        self.years = []

    def refresh_payment(self, year, on_success, on_failure):
        self.years.append(year)
        self.pending.append((on_success, on_failure))

    def succeed(self, invoice):
        on_success, _ = self.pending.pop(0)
        on_success(invoice)

    def fail(self, error):
        _, on_failure = self.pending.pop(0)
        on_failure(error)


class Recorder:
    def __init__(self, watcher):
        self.events = []
        watcher.paid.connect(lambda inv: self.events.append(("paid", inv)))
        watcher.still_waiting.connect(lambda n: self.events.append(("waiting", n)))
        watcher.expired.connect(lambda inv: self.events.append(("expired", inv)))
        watcher.gave_up.connect(lambda: self.events.append(("gave_up",)))
        watcher.failed.connect(lambda err: self.events.append(("failed", err)))

    def names(self):
        return [e[0] for e in self.events]


def watcher(max_attempts=POLL_ATTEMPTS):
    api, timer = ScriptedApi(), FakeTimer()
    w = PaymentWatcher(api, 2026, timer=timer, max_attempts=max_attempts)
    return w, api, timer, Recorder(w)


def test_the_defaults_mirror_the_reference_client():
    assert POLL_INTERVAL_MS == 5_000
    assert POLL_ATTEMPTS == 24


def test_the_first_check_waits_one_interval():
    w, api, timer, _ = watcher()
    w.start()
    assert timer.single_shot
    assert timer.started == [POLL_INTERVAL_MS]
    assert api.pending == []
    timer.fire()
    assert len(api.pending) == 1 and api.years == [2026]


def test_poll_now_checks_immediately():
    w, api, timer, _ = watcher()
    w.start(poll_now=True)
    assert len(api.pending) == 1
    assert not timer.isActive()


def test_a_settled_payment_is_reported_and_polling_stops():
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    api.succeed(UNPAID)
    assert rec.events == [("waiting", 1)]
    assert timer.isActive() and timer.started[-1] == POLL_INTERVAL_MS
    timer.fire()
    api.succeed(PAID)
    assert rec.names() == ["waiting", "paid"]
    assert rec.events[-1][1] is PAID
    assert not w.running and not timer.isActive()
    assert api.pending == []


def test_it_gives_up_after_the_last_attempt():
    w, api, timer, rec = watcher(max_attempts=3)
    w.start(poll_now=True)
    api.succeed(UNPAID)
    timer.fire()
    api.succeed(UNPAID)
    timer.fire()
    api.succeed(UNPAID)
    assert rec.events == [("waiting", 1), ("waiting", 2), ("gave_up",)]
    assert not w.running and not timer.isActive()
    assert len(api.years) == 3


def test_the_full_default_run_is_twenty_four_checks():
    w, api, timer, rec = watcher()
    w.start()
    for _ in range(POLL_ATTEMPTS):
        timer.fire()
        api.succeed(UNPAID)
    assert len(api.years) == POLL_ATTEMPTS
    assert rec.names().count("waiting") == POLL_ATTEMPTS - 1
    assert rec.names()[-1] == "gave_up"


def test_only_one_check_is_in_flight_at_a_time():
    w, api, timer, _ = watcher()
    w.start(poll_now=True)
    assert not timer.isActive()  # the next check waits for this answer
    w.check_now()
    assert len(api.pending) == 1


def test_check_now_skips_the_wait():
    w, api, timer, _ = watcher()
    w.start()
    w.check_now()
    assert len(api.pending) == 1 and not timer.isActive()


def test_an_expired_invoice_ends_the_wait():
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    api.succeed(EXPIRED)
    assert rec.names() == ["expired"]
    assert not w.running and not timer.isActive()


def test_stop_ignores_an_answer_still_in_flight():
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    w.stop()
    api.succeed(PAID)
    assert rec.events == []
    assert not timer.isActive() and not w.running


def test_stop_while_waiting_cancels_the_next_check():
    w, api, timer, _ = watcher()
    w.start()
    w.stop()
    assert not timer.isActive()
    w.check_now()
    assert api.pending == []


def test_a_restart_ignores_the_previous_run():
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    w.start(poll_now=True)
    api.succeed(PAID)  # the answer to the first run
    assert rec.events == []
    api.succeed(UNPAID)
    assert rec.events == [("waiting", 1)]


def test_a_slot_that_stops_the_watcher_is_respected():
    w, api, timer, _ = watcher()
    w.still_waiting.connect(lambda _n: w.stop())
    w.start(poll_now=True)
    api.succeed(UNPAID)
    assert not timer.isActive() and not w.running


@pytest.mark.parametrize("code", [
    ErrorCode.OFFLINE, ErrorCode.TIMEOUT, ErrorCode.SERVER, ErrorCode.BAD_RESPONSE,
])
def test_a_passing_failure_keeps_waiting(code):
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    error = ApiError(code)
    api.fail(error)
    assert rec.events == [("waiting", 1)]
    assert w.last_error is error
    assert timer.isActive()
    timer.fire()
    api.succeed(PAID)
    assert rec.names()[-1] == "paid"
    assert w.last_error is None


@pytest.mark.parametrize("code", [
    ErrorCode.SIGNER_DECLINED, ErrorCode.SIGNER_UNREACHABLE, ErrorCode.UNAUTHORIZED,
    ErrorCode.UNAVAILABLE, ErrorCode.NOT_FOUND, ErrorCode.VALIDATION, ErrorCode.CONFLICT,
])
def test_a_failure_asking_again_cannot_cure_stops_the_wait(code):
    # Above all a signer that said no or is not answering: asking again
    # would only put another prompt on the phone.
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    error = ApiError(code)
    api.fail(error)
    assert rec.events == [("failed", error)]
    assert not w.running and not timer.isActive()


def test_a_short_quota_wait_is_honoured():
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    api.fail(ApiError(ErrorCode.RATE_LIMITED, status=429, retry_after=30))
    assert rec.names() == ["waiting"]
    assert timer.started[-1] == 30_000


def test_a_long_quota_wait_ends_the_watch():
    w, api, timer, rec = watcher()
    w.start(poll_now=True)
    error = ApiError(ErrorCode.RATE_LIMITED, status=429, retry_after=3600)
    api.fail(error)
    assert rec.events == [("failed", error)]


def test_polling_uses_refresh_with_one_fresh_signature_per_check():
    clock = FakeClock()
    server = FakeMembershipServer(clock)
    answers = iter([invoice_data(created=False), invoice_data(paid=True, created=False)])
    server.routes["POST /payments/2026/refresh"] = lambda b, e: data_reply(next(answers))
    nam = FakeNam(responder=server)
    signer = FakeSigner(clock=clock)
    api = MembershipApi(signer, service_url=SERVICE, nam=nam, clock=clock)
    timer = FakeTimer()
    w = PaymentWatcher(api, 2026, timer=timer)
    rec = Recorder(w)

    w.start()
    timer.fire()
    nam.settle()
    clock.advance(5)
    timer.fire()
    nam.settle()

    assert rec.names() == ["waiting", "paid"]
    assert len(signer.requests) == 2  # one per check, not one per check per endpoint
    assert [tag(e, "u") for e in server.accepted] == [PREFIX + "/payments/2026/refresh"] * 2
    assert len({e["id"] for e in server.accepted}) == 2
    assert server.refusals == []
