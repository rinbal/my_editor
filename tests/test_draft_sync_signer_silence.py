# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What happens when the signer stops answering.

Observed against a real Amber pairing: the phone's background service
was gone, so every request reached the relays (7/7 accepted) and nothing
ever replied. The queue is serialized and each request waits 30 seconds,
so a 54-draft library spent close to half an hour showing "Decrypting 0
of 54" and then, having marked all 54 failed, offered no way back.

The properties pinned here are the ones that failure taught us:

  1. Stop asking once it is clear nobody is listening.
  2. Say so, instead of showing progress that cannot advance.
  3. Leave a door open, because the cure is on the user's phone and they
     are the only one who can tell us they have applied it.
"""

from __future__ import annotations

import sys
from typing import List
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication

from nostr.bunker import humanize_failure, is_signer_silent
from nostr.draft_store import DraftState, DraftStore
from nostr.draft_sync import _SILENCE_LIMIT, DraftSync
from nostr.drafts import DraftWrapMeta

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


def _wrap(ident: str, ct: str = "CT", created_at: int = 100) -> DraftWrapMeta:
    return DraftWrapMeta(
        identifier=ident, inner_kind=1, event_id=f"e-{ident}",
        pubkey=PK, created_at=created_at, expiration=None, ciphertext=ct,
    )


def _sync(sent: List[str] | None = None) -> DraftSync:
    sync = DraftSync(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), store=DraftStore(),
    )
    sync._profile = _make_profile()
    sync._store.bind_profile(PK)
    bunker = MagicMock()
    if sent is not None:
        bunker.nip44_decrypt_self.side_effect = (
            lambda ct, on_success, on_failure: sent.append(ct)
        )
    sync._bunker = bunker
    return sync


def _arrive(sync: DraftSync, *idents: str) -> None:
    for ident in idents:
        meta = _wrap(ident)
        sync._store.upsert_skeleton(meta)
        sync._enqueue_decrypt(meta)


def _time_out(sync: DraftSync, ident: str, reason: str = SILENT) -> None:
    sync._on_decrypt_failure(sync._generation, ident, f"e-{ident}", reason)


# --------------------------------------------------------------------- #
# 1. Stop asking                                                        #
# --------------------------------------------------------------------- #

def test_one_silence_is_not_enough_to_give_up():
    # A single dropped reply is ordinary. Latching on it would send the
    # user to their phone over nothing.
    sync = _sync()
    _arrive(sync, "a")
    _time_out(sync, "a")
    assert not sync._signer_unreachable


def test_a_run_of_silences_stops_the_queue():
    sent: List[str] = []
    sync = _sync(sent)
    _arrive(sync, *[str(i) for i in range(10)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))

    assert sync._signer_unreachable
    # The queue is not merely paused, it is emptied: a signer that comes
    # back should not first work through requests nobody is waiting for.
    assert not sync._decrypt_queue and not sync._pending
    assert len(sent) == _SILENCE_LIMIT


def test_the_remaining_drafts_do_not_each_cost_a_timeout():
    # The defect this is really about: 54 drafts x 30s of "Decrypting".
    sent: List[str] = []
    sync = _sync(sent)
    _arrive(sync, *[str(i) for i in range(54)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))
    assert len(sent) == _SILENCE_LIMIT < 54


def test_wraps_still_arriving_after_we_gave_up_are_not_left_decrypting():
    # The relay subscription stays open, so wraps keep coming. A row left
    # in LOADING claims a request is in flight when none is.
    sync = _sync()
    _arrive(sync, *[str(i) for i in range(_SILENCE_LIMIT)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))

    _arrive(sync, "late")
    assert sync._store.get("late").state is DraftState.FAILED


def test_a_failed_handshake_gives_up_without_spending_two_more_timeouts():
    # The handshake is the strongest evidence we ever get.
    sync = _sync()
    sync._read_relays = ["wss://r.test/"]
    sync._on_bunker_unavailable(sync._generation, SILENT)
    assert sync._signer_unreachable


def test_an_undeliverable_request_counts_as_silence_too():
    sync = _sync()
    sync._read_relays = ["wss://r.test/"]
    sync._on_bunker_unavailable(sync._generation, UNDELIVERED)
    assert sync._signer_unreachable


def test_a_signer_that_refuses_is_not_treated_as_absent():
    # A reasoned "no" proves someone is home. Latching would send the
    # user to restart an app that is already running.
    sync = _sync()
    _arrive(sync, "a", "b")
    _time_out(sync, "a", "user rejected the request")
    _time_out(sync, "b", "user rejected the request")
    assert not sync._signer_unreachable


def test_an_answer_between_two_silences_resets_the_tally():
    # Silences have to be consecutive, or a signer that is merely slow
    # accumulates its way to being declared dead over a long session.
    sync = _sync()
    _arrive(sync, "a", "b", "c")
    _time_out(sync, "a")
    sync._on_decrypt_failure(sync._generation, "b", "e-b", "malformed payload")
    _time_out(sync, "c")
    assert not sync._signer_unreachable


def test_a_stale_callback_cannot_declare_the_new_profile_dead():
    sync = _sync()
    _arrive(sync, "a")
    stale = sync._generation
    sync.stop()
    for _ in range(_SILENCE_LIMIT + 1):
        sync._on_decrypt_failure(stale, "a", "e-a", SILENT)
    assert not sync._signer_unreachable


# --------------------------------------------------------------------- #
# 2. Say so                                                             #
# --------------------------------------------------------------------- #

def test_giving_up_is_announced_once():
    sync = _sync()
    seen: List[bool] = []
    sync.signer_unreachable.connect(seen.append)
    _arrive(sync, *[str(i) for i in range(5)])
    for i in range(_SILENCE_LIMIT + 2):
        _time_out(sync, str(i))
    assert seen == [True]


def test_every_waiting_draft_is_marked_not_left_mid_decrypt():
    sync = _sync()
    _arrive(sync, *[str(i) for i in range(6)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))
    assert all(r.state is DraftState.FAILED for r in sync._store)


def test_the_row_names_the_signer_rather_than_the_transport():
    sync = _sync()
    _arrive(sync, "a")
    _time_out(sync, "a")
    assert "signer" in sync._store.get("a").failure_reason.lower()


def test_the_publish_error_says_where_the_problem_is():
    # "timed out waiting for signer" is accurate and useless: it does not
    # say that the phone in the user's hand is the thing to fix.
    assert is_signer_silent(SILENT) and is_signer_silent(UNDELIVERED)
    for reason in (SILENT, UNDELIVERED):
        message = humanize_failure(reason)
        assert "signer" in message.lower()
        assert "try again" in message.lower()


def test_a_failure_we_have_no_better_words_for_is_passed_through():
    assert humanize_failure("signer returned malformed user pubkey") == (
        "signer returned malformed user pubkey"
    )
    assert not is_signer_silent("user rejected the request")


# --------------------------------------------------------------------- #
# 3. Leave a door open                                                  #
# --------------------------------------------------------------------- #

def test_retrying_one_row_lets_the_whole_queue_run_again():
    sent: List[str] = []
    sync = _sync(sent)
    _arrive(sync, *[str(i) for i in range(4)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))
    before = len(sent)

    sync.retry_decrypt("0")
    assert not sync._signer_unreachable
    assert len(sent) > before


def test_retry_signer_requeues_everything_it_gave_up_on():
    sent: List[str] = []
    sync = _sync(sent)
    _arrive(sync, *[str(i) for i in range(5)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))
    sent.clear()

    sync.retry_signer()
    assert not sync._signer_unreachable
    # Serialized queue: one goes out now, the rest follow as it drains.
    assert len(sent) == 1
    assert sync._decrypt_queue or sync._decrypt_inflight


def test_retry_signer_reconnects_when_the_handshake_never_completed():
    # ``retry_decrypt`` alone cannot recover this: it returns early while
    # ``_bunker`` is None, so nothing would ever ask again.
    sync = _sync()
    sync._bunker = None
    sync._read_relays = ["wss://r.test/"]
    sync._on_bunker_unavailable(sync._generation, SILENT)
    assert sync._signer_unreachable

    sync.retry_signer()
    assert sync._session_pool.get.called
    assert not sync._signer_unreachable


def test_a_signer_that_comes_back_clears_the_state_by_answering():
    sync = _sync()
    seen: List[bool] = []
    _arrive(sync, "a", "b")
    for ident in ("a", "b"):
        _time_out(sync, ident)
    assert sync._signer_unreachable
    sync.signer_unreachable.connect(seen.append)

    sync._on_decrypt_success(
        sync._generation, "a", "e-a", '{"kind":1,"content":"hi"}',
    )
    assert not sync._signer_unreachable
    assert seen == [False]


def test_reconnecting_asks_again_for_drafts_the_relay_will_not_resend():
    # The store already holds these wraps at this ``created_at``, so the
    # re-opened subscription delivers nothing and only an explicit
    # re-queue gets them decrypted.
    sent: List[str] = []
    sync = _sync(sent)
    _arrive(sync, *[str(i) for i in range(3)])
    for i in range(_SILENCE_LIMIT):
        _time_out(sync, str(i))
    sent.clear()

    sync._on_bunker_ready(sync._generation, sync._bunker)
    assert not sync._signer_unreachable
    assert sent


def test_giving_up_does_not_survive_a_profile_switch():
    sync = _sync()
    _arrive(sync, "a", "b")
    for ident in ("a", "b"):
        _time_out(sync, ident)
    assert sync._signer_unreachable

    sync.stop()
    assert not sync._signer_unreachable
    assert sync._consecutive_silences == 0
