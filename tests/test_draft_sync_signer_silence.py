# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the drafts do while the signer app is away.

Measured against a real pairing: the requests reached five relays and
nothing answered on any of them, on several days. The phone was asleep,
or the signer app closed. That is the ordinary case for a signer that
lives on a phone, so it is designed for, not reported as an error:

  1. Check before asking: one cheap question (``ping``, then
     ``get_public_key``) finds out whether anyone is there, where each
     draft used to spend its own 30 seconds finding out.
  2. Waiting is a state, not a failure: drafts that cannot be opened yet
     wait, drafts that arrive meanwhile join them, and only the signer's
     own "no" (or a damaged draft) fails a draft.
  3. It resumes by itself: asked again every minute, at once on Try Again
     or when the person comes back to the app, and decrypting goes on the
     moment the signer answers.
"""

from __future__ import annotations

import sys
from typing import List
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication

from nostr.bunker import BunkerClient, is_signer_silent, signer_failure_words
from nostr.draft_store import DraftState, DraftStore
from nostr.draft_sync import SIGNER_RECHECK_MS, DraftSync
from nostr.drafts import DraftWrapMeta, build_inner_event, serialize_inner_event

PK = "a" * 64
SILENT = "timed out waiting for signer"
UNDELIVERED = "could not deliver request to any relay (wss://x: no)"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def _make_profile():
    p = MagicMock()
    p.user_pubkey = PK
    p.bunker_pubkey = "b" * 64
    p.bunker_relays = ["wss://bunker.test/"]
    p.local_secret_hex = "0" * 64
    return p


class Phone:
    """The signer app on the person's phone. Every question waits until
    the test answers it, or lets it go unanswered."""

    def __init__(self):
        self.client = MagicMock(spec=BunkerClient)
        self.checks: List[tuple] = []
        self.decrypts: List[tuple] = []
        self.client.check_alive.side_effect = (
            lambda on_answer, on_silence, **_kw: self.checks.append((on_answer, on_silence)))
        self.client.nip44_decrypt_self.side_effect = (
            lambda ct, on_success, on_failure, **_kw: self.decrypts.append(
                (ct, on_success, on_failure)))

    def answers(self):
        on_answer, _on_silence = self.checks[-1]
        on_answer()

    def is_silent(self, reason: str = SILENT):
        _on_answer, on_silence = self.checks[-1]
        on_silence(reason)

    def asked_for(self) -> List[str]:
        return [ct for ct, _ok, _fail in self.decrypts]

    def opens(self, ident: str):
        ct, on_success, _fail = self.decrypts[-1]
        assert ct == f"CT-{ident}"
        on_success(serialize_inner_event(build_inner_event(kind=1, content=ident,
                                                           pubkey_hex=PK)))

    def does_not_answer_the_draft(self, reason: str = SILENT):
        _ct, _ok, on_failure = self.decrypts[-1]
        on_failure(reason)

    def refuses_the_draft(self, reason: str = "user rejected the request"):
        _ct, _ok, on_failure = self.decrypts[-1]
        on_failure(reason)


def no_draft_list(_profile, on_done):
    on_done([])


def _sync(phone: Phone | None = None) -> DraftSync:
    sync = DraftSync(
        relay_pool=MagicMock(), relay_directory=MagicMock(),
        session_pool=MagicMock(), store=DraftStore(), read_draft_list=no_draft_list,
    )
    sync._profile = _make_profile()
    sync._store.bind_profile(PK)
    sync._read_relays = ["wss://r.test/"]
    if phone is not None:
        sync._bunker = phone.client
    return sync


def _wrap(ident: str, created_at: int = 100) -> DraftWrapMeta:
    return DraftWrapMeta(
        identifier=ident, inner_kind=1, event_id=f"e-{ident}",
        pubkey=PK, created_at=created_at, expiration=None, ciphertext=f"CT-{ident}",
    )


def _arrive(sync: DraftSync, *idents: str) -> None:
    for ident in idents:
        meta = _wrap(ident)
        sync._store.upsert_skeleton(meta)
        sync._enqueue_decrypt(meta)


def _many(n: int) -> List[str]:
    return [f"d{i:02d}" for i in range(n)]


def _waiting(sync: DraftSync, phone: Phone, *idents: str) -> None:
    """Drafts have arrived and the signer app did not answer the check."""
    _arrive(sync, *idents)
    phone.is_silent()


# --------------------------------------------------------------------- #
# 1. Check before asking                                                #
# --------------------------------------------------------------------- #

def test_the_signer_is_asked_whether_it_is_there_before_any_draft():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a", "b")
    assert len(phone.checks) == 1
    assert phone.decrypts == []

    phone.answers()
    # Then one draft at a time, as always.
    assert phone.asked_for() == ["CT-a"]


def test_no_answer_and_the_drafts_wait_without_a_single_decrypt_timeout():
    # The defect this is about: two drafts spent 30 seconds each finding
    # out what one cheap question finds out.
    phone = Phone()
    sync = _sync(phone)
    _waiting(sync, phone, *_many(48))
    assert phone.decrypts == []
    assert all(r.state is DraftState.LOADING for r in sync._store)
    assert not any(r.failure_reason for r in sync._store)
    assert sync._signer_unreachable


def test_one_question_for_a_burst_of_drafts():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, *_many(48))
    assert len(phone.checks) == 1


def test_a_key_kept_on_this_computer_is_never_asked_whether_it_is_there():
    # Only a signer app on another device can be away.
    sync = _sync()
    local = MagicMock()
    sent: List[str] = []
    local.nip44_decrypt_self.side_effect = (
        lambda ct, on_success, on_failure, **_kw: sent.append(ct))
    sync._bunker = local
    _arrive(sync, "a")
    assert sent == ["CT-a"]
    local.check_alive.assert_not_called()


def test_a_refresh_asks_again_before_the_next_draft():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a")
    phone.answers()
    phone.opens("a")
    sync.refresh()
    _arrive(sync, "b")
    assert len(phone.checks) == 2
    assert phone.asked_for() == ["CT-a"]
    phone.answers()
    assert phone.asked_for() == ["CT-a", "CT-b"]


def test_retrying_one_draft_asks_first():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a")
    phone.answers()
    phone.refuses_the_draft()
    assert sync._store.get("a").state is DraftState.FAILED

    sync.retry_decrypt("a")
    assert len(phone.checks) == 2
    phone.answers()
    assert phone.asked_for() == ["CT-a", "CT-a"]


# --------------------------------------------------------------------- #
# 2. Waiting is a state, not a failure                                  #
# --------------------------------------------------------------------- #

def test_drafts_that_arrive_while_others_wait_join_them():
    phone = Phone()
    sync = _sync(phone)
    _waiting(sync, phone, "a")
    _arrive(sync, "late")
    assert sync._store.get("late").state is DraftState.LOADING
    assert "late" in sync._pending
    assert phone.decrypts == []


def test_a_draft_nobody_answered_for_waits_first_in_line():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a", "b", "c")
    phone.answers()
    phone.does_not_answer_the_draft()

    record = sync._store.get("a")
    assert record.state is DraftState.LOADING and not record.failure_reason
    assert list(sync._decrypt_queue)[0] == "a"
    assert sync._signer_unreachable
    # Nobody is there, so nobody else is asked.
    assert phone.asked_for() == ["CT-a"]


def test_an_undelivered_request_is_silence_too():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a")
    phone.is_silent(UNDELIVERED)
    assert sync._signer_unreachable
    assert sync._store.get("a").state is DraftState.LOADING


def test_only_the_signers_own_no_fails_a_draft():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a", "b")
    phone.answers()
    phone.refuses_the_draft()
    assert sync._store.get("a").state is DraftState.FAILED
    assert not sync._signer_unreachable
    assert phone.asked_for() == ["CT-a", "CT-b"]


def test_a_damaged_draft_still_fails_on_its_own():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a", "b")
    phone.answers()
    _ct, on_success, _fail = phone.decrypts[-1]
    on_success("not an event")
    assert sync._store.get("a").state is DraftState.FAILED
    assert "damaged" in sync._store.get("a").failure_reason
    assert phone.asked_for() == ["CT-a", "CT-b"]


def test_the_newest_version_of_a_waiting_draft_is_the_one_asked_for():
    # Latest write wins, also across a wait.
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a")
    phone.answers()
    newer = DraftWrapMeta(identifier="a", inner_kind=1, event_id="e-a2", pubkey=PK,
                          created_at=200, expiration=None, ciphertext="CT-a2")
    sync._store.upsert_skeleton(newer)
    sync._enqueue_decrypt(newer)
    phone.does_not_answer_the_draft()
    sync._recheck_timer.timeout.emit()
    phone.answers()
    assert phone.asked_for() == ["CT-a", "CT-a2"]


def test_giving_up_is_said_once():
    phone = Phone()
    sync = _sync(phone)
    seen: List[bool] = []
    sync.signer_unreachable.connect(seen.append)
    _waiting(sync, phone, "a")
    for _ in range(3):
        sync._recheck_timer.timeout.emit()
        phone.is_silent()
    assert seen == [True]


# --------------------------------------------------------------------- #
# 3. It resumes by itself                                               #
# --------------------------------------------------------------------- #

def test_while_drafts_wait_the_signer_is_asked_again_every_minute():
    phone = Phone()
    sync = _sync(phone)
    _waiting(sync, phone, "a")
    assert SIGNER_RECHECK_MS == 60_000
    assert sync._recheck_timer.isActive()
    assert sync._recheck_timer.interval() == SIGNER_RECHECK_MS

    sync._recheck_timer.timeout.emit()
    assert len(phone.checks) == 2


def test_the_moment_the_signer_answers_decrypting_resumes():
    phone = Phone()
    sync = _sync(phone)
    seen: List[bool] = []
    sync.signer_unreachable.connect(seen.append)
    _waiting(sync, phone, "a", "b")

    sync._recheck_timer.timeout.emit()
    phone.answers()
    assert phone.asked_for() == ["CT-a"]
    phone.opens("a")
    assert seen == [True, False]
    assert not sync._recheck_timer.isActive()
    assert phone.asked_for() == ["CT-a", "CT-b"]


def test_an_answer_to_the_question_but_not_to_the_draft_keeps_them_waiting():
    # The signer app is open but nobody approves the draft on the phone:
    # the drafts stay one quiet line, rather than coming back as rows for
    # thirty seconds and leaving again every minute.
    phone = Phone()
    sync = _sync(phone)
    seen: List[bool] = []
    sync.signer_unreachable.connect(seen.append)
    _waiting(sync, phone, "a", "b")

    sync._recheck_timer.timeout.emit()
    phone.answers()
    phone.does_not_answer_the_draft()
    assert seen == [True]
    assert sync._signer_unreachable and sync._recheck_timer.isActive()


def test_try_again_asks_now():
    phone = Phone()
    sync = _sync(phone)
    _waiting(sync, phone, "a")
    sync.retry_signer()
    assert len(phone.checks) == 2
    # Asked once at a time: a second click while it is asked adds nothing.
    sync.retry_signer()
    assert len(phone.checks) == 2


def test_coming_back_to_the_app_asks_at_once_only_while_drafts_wait():
    phone = Phone()
    sync = _sync(phone)
    sync.ask_signer_again()
    assert phone.checks == []

    _waiting(sync, phone, "a")
    sync.ask_signer_again()
    assert len(phone.checks) == 2


def test_showing_the_panel_asks_at_once_while_drafts_wait():
    # Showing the Drafts panel refreshes it.
    phone = Phone()
    sync = _sync(phone)
    _waiting(sync, phone, "a")
    sync.refresh()
    assert len(phone.checks) == 2


def test_a_handshake_nobody_answered_is_asked_again_through_the_session_pool():
    sync = _sync()
    gets = sync._session_pool.get
    sync._on_bunker_unavailable(sync._generation, SILENT)
    assert sync._signer_unreachable
    assert sync._relay_pool.subscribe.call_count == 1      # the drafts are read anyway

    sync._recheck_timer.timeout.emit()
    assert gets.call_count == 1
    gets.call_args.kwargs["on_error"](SILENT)
    # A silent minute does not read the relays again.
    assert sync._relay_pool.subscribe.call_count == 1
    assert sync._recheck_timer.isActive()


def test_drafts_that_waited_for_the_handshake_are_decrypted_once_it_answers():
    # They used to stay "Decrypting" for good: nothing queued them while
    # there was no signer, and the relays do not send them twice.
    sync = _sync()
    sync._on_bunker_unavailable(sync._generation, SILENT)
    _arrive(sync, "a", "b")
    sync.retry_signer()
    phone = Phone()
    sync._session_pool.get.call_args.kwargs["on_ready"](phone.client)
    phone.answers()
    assert phone.asked_for() == ["CT-a"]


def test_a_signer_that_cannot_decrypt_at_all_still_ends_the_asking():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a", "b")
    phone.answers()
    phone.refuses_the_draft("method not found")
    assert sync._bunker_unsupported
    sync.retry_signer()
    sync.ask_signer_again()
    assert len(phone.checks) == 1


def test_leaving_the_account_stops_the_asking():
    phone = Phone()
    sync = _sync(phone)
    _waiting(sync, phone, "a")
    sync._recheck_timer.timeout.emit()
    sync.stop()
    assert not sync._recheck_timer.isActive()
    assert not sync._signer_unreachable

    # The question asked before leaving comes back: it changes nothing.
    phone.answers()
    assert phone.decrypts == []
    assert not sync._recheck_timer.isActive()


def test_a_stale_silence_cannot_make_the_next_account_wait():
    phone = Phone()
    sync = _sync(phone)
    _arrive(sync, "a")
    sync.stop()
    sync._profile = _make_profile()
    phone.is_silent()
    assert not sync._signer_unreachable
    assert not sync._recheck_timer.isActive()


# --------------------------------------------------------------------- #
# Words                                                                 #
# --------------------------------------------------------------------- #

def test_the_publish_error_says_where_the_problem_is():
    # "timed out waiting for signer" is accurate and useless: it does not
    # say that the phone in the user's hand is the thing to fix.
    assert is_signer_silent(SILENT) and is_signer_silent(UNDELIVERED)
    for reason in (SILENT, UNDELIVERED):
        message = signer_failure_words(reason)
        assert "signer" in message.lower()
        assert "try again" in message.lower()


def test_a_failure_we_have_no_better_words_for_is_said_plainly_never_passed_through():
    # Lessons 26 and 36: the reason is for Details and the log, never the
    # sentence shown.
    message = signer_failure_words("signer returned malformed user pubkey")
    assert message == "Your signer app ran into a problem: open it and try again."
    assert not is_signer_silent("user rejected the request")
