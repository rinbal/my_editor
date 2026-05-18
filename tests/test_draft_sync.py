"""DraftSync — cancellation, queue invariants, trust boundary.

Three critical correctness properties from the code-review pass live here:

  1. Generation-counter cancellation: a callback from a previous
     ``start_for`` / ``stop`` cycle must not mutate the store after a
     new profile has been bound.

  2. Lost-ciphertext re-queue: when a newer wrap for an *already
     in-flight* identifier arrives, the new ciphertext must still get
     decrypted after the in-flight one resolves. The original code path
     skipped the re-queue and stranded the new ciphertext.

  3. Inner-pubkey verification: a misbehaving signer cannot smuggle an
     inner event whose declared pubkey differs from the active profile.

Plus relay-selection precedence for ``_select_read_relays``.
"""

from __future__ import annotations

import sys
from typing import List
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication

from nostr.draft_store import DraftState, DraftStore
from nostr.draft_sync import DraftSync, _select_read_relays
from nostr.drafts import (
    DraftWrapMeta,
    build_inner_event,
    serialize_inner_event,
)
from nostr.outbox import RelayList


PK = "a" * 64
OTHER_PK = "b" * 64


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def _make_profile(pubkey: str):
    p = MagicMock()
    p.user_pubkey = pubkey
    p.bunker_pubkey = "b" * 64
    p.bunker_relays = ["wss://bunker.test/"]
    p.local_secret_hex = "0" * 64
    return p


def _make_sync(store: DraftStore | None = None) -> DraftSync:
    return DraftSync(
        relay_pool=MagicMock(),
        relay_list_cache=MagicMock(),
        session_pool=MagicMock(),
        store=store or DraftStore(),
    )


def _wrap(*, ident: str, event_id: str, ct: str, created_at: int = 100) -> DraftWrapMeta:
    return DraftWrapMeta(
        identifier=ident, inner_kind=1, event_id=event_id,
        pubkey=PK, created_at=created_at, expiration=None,
        ciphertext=ct,
    )


# --------------------------------------------------------------------------- #
# 1. Generation-counter cancellation                                          #
# --------------------------------------------------------------------------- #

def test_stop_bumps_generation():
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    before = sync._generation
    sync.stop()
    assert sync._generation > before


def test_callback_from_stopped_session_is_ignored():
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    gen_a = sync._generation

    sync.stop()
    # Bind a new profile — store is now keyed on OTHER_PK.
    sync._profile = _make_profile(OTHER_PK)
    sync._store.bind_profile(OTHER_PK)

    # A late callback from the prior epoch arrives. It must NOT leak.
    poison = serialize_inner_event(build_inner_event(kind=1, content="poison", pubkey_hex=PK))
    sync._on_decrypt_success(gen_a, "stale-id", "stale-event-id", poison)
    assert "stale-id" not in sync._store


def test_relay_list_callback_from_stopped_session_does_not_proceed():
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    gen_a = sync._generation
    sync.stop()

    # The session_pool.get is the next step after _on_relay_list_ready.
    sync._on_relay_list_ready(gen_a, RelayList(write=["wss://x/"], read=[]))
    sync._session_pool.get.assert_not_called()


def test_bunker_ready_callback_from_stopped_session_does_not_open_sub():
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    sync._read_relays = ["wss://r/"]
    gen_a = sync._generation
    sync.stop()

    fake_bunker = MagicMock()
    sync._on_bunker_ready(gen_a, fake_bunker)
    sync._relay_pool.subscribe.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. Decryption-queue invariants                                              #
# --------------------------------------------------------------------------- #

def test_first_wrap_triggers_decrypt():
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    calls = []
    bunker = MagicMock()
    bunker.nip44_decrypt_self.side_effect = lambda ct, on_success, on_failure: calls.append(ct)
    sync._bunker = bunker

    m = _wrap(ident="x", event_id="e1", ct="CT1")
    sync._store.upsert_skeleton(m)
    sync._enqueue_decrypt(m)
    assert calls == ["CT1"]


def test_newer_wrap_during_inflight_is_decrypted_after():
    """The bug that the code-review caught: wrap arrives while inflight,
    newer ciphertext gets stranded in ``_pending`` forever."""
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    calls: List[str] = []
    bunker = MagicMock()
    bunker.nip44_decrypt_self.side_effect = lambda ct, on_success, on_failure: calls.append(ct)
    sync._bunker = bunker

    # v1 arrives → decrypt fires immediately, queue empty, inflight=x.
    sync._store.upsert_skeleton(_wrap(ident="x", event_id="e1", ct="CT1", created_at=10))
    sync._enqueue_decrypt(_wrap(ident="x", event_id="e1", ct="CT1", created_at=10))
    assert calls == ["CT1"]

    # v2 arrives during inflight — held in _pending, NOT yet decrypted.
    sync._store.upsert_skeleton(_wrap(ident="x", event_id="e2", ct="CT2", created_at=20))
    sync._enqueue_decrypt(_wrap(ident="x", event_id="e2", ct="CT2", created_at=20))
    assert calls == ["CT1"]
    assert sync._pending["x"] == ("e2", "CT2")

    # v1 completes → after_decrypt must re-queue and pump CT2.
    inner_v1 = build_inner_event(kind=1, content="v1", pubkey_hex=PK)
    sync._on_decrypt_success(sync._generation, "x", "e1", serialize_inner_event(inner_v1))
    assert calls == ["CT1", "CT2"], "newer ciphertext stranded after inflight completed"


def test_multiple_intervening_wraps_collapse_to_latest():
    # Rapid edits to the same draft must not blow up the bunker with N
    # decryption requests — last-write-wins.
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    calls: List[str] = []
    bunker = MagicMock()
    bunker.nip44_decrypt_self.side_effect = lambda ct, on_success, on_failure: calls.append(ct)
    sync._bunker = bunker

    sync._store.upsert_skeleton(_wrap(ident="x", event_id="e1", ct="CT1", created_at=10))
    sync._enqueue_decrypt(_wrap(ident="x", event_id="e1", ct="CT1", created_at=10))
    # Four more wraps arrive while CT1 inflight.
    for i, (e, c, t) in enumerate(
        [("e2", "CT2", 20), ("e3", "CT3", 30), ("e4", "CT4", 40), ("e5", "CT5", 50)],
        start=2,
    ):
        sync._store.upsert_skeleton(_wrap(ident="x", event_id=e, ct=c, created_at=t))
        sync._enqueue_decrypt(_wrap(ident="x", event_id=e, ct=c, created_at=t))
    assert calls == ["CT1"]  # still inflight

    sync._on_decrypt_success(
        sync._generation, "x", "e1",
        serialize_inner_event(build_inner_event(kind=1, content="v1", pubkey_hex=PK)),
    )
    # Only the LAST pending ciphertext gets sent — not CT2/3/4 in turn.
    assert calls == ["CT1", "CT5"]


def test_tombstone_drains_pending_ciphertext():
    # If a tombstone arrives while a decryption is queued, we must
    # drop the queued ciphertext — otherwise we'd briefly resurrect
    # the deleted draft.
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)

    sync._store.upsert_skeleton(_wrap(ident="x", event_id="e1", ct="CT1"))
    sync._bunker = MagicMock()
    sync._enqueue_decrypt(_wrap(ident="x", event_id="e1", ct="CT1"))
    assert sync._decrypt_inflight == "x"

    # Simulate tombstone arrival via _on_wrap_event path
    tomb_event = {
        "kind": 31234,
        "tags": [["d", "x"], ["k", "1"]],
        "id": "tomb-id",
        "pubkey": PK,
        "created_at": 999,
        "content": "",
    }
    sync._on_wrap_event(tomb_event)
    assert "x" not in sync._pending


# --------------------------------------------------------------------------- #
# 3. Inner-pubkey trust boundary                                              #
# --------------------------------------------------------------------------- #

def test_inner_pubkey_mismatch_fails_the_record():
    # A malicious or buggy signer could decrypt an inner event with a
    # foreign pubkey. We must refuse to populate the record — promoting
    # that draft to publish would sign foreign content under our key.
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    sync._store.upsert_skeleton(_wrap(ident="y", event_id="e", ct="ct"))

    poisoned = build_inner_event(kind=1, content="not-ours", pubkey_hex=OTHER_PK)
    sync._on_decrypt_success(sync._generation, "y", "e", serialize_inner_event(poisoned))

    record = sync._store.get("y")
    assert record.state is DraftState.FAILED
    assert "different identity" in record.failure_reason


def test_inner_pubkey_match_accepts_the_record():
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    sync._store.upsert_skeleton(_wrap(ident="y", event_id="e", ct="ct"))

    ok = build_inner_event(kind=1, content="ours", pubkey_hex=PK)
    sync._on_decrypt_success(sync._generation, "y", "e", serialize_inner_event(ok))

    assert sync._store.get("y").state is DraftState.READY


# --------------------------------------------------------------------------- #
# 4. Bunker NIP-44 unsupported latching                                       #
# --------------------------------------------------------------------------- #

def test_bunker_unsupported_latches_and_marks_all_loading_failed():
    # An "unknown method" reply means the signer doesn't speak NIP-44 at
    # all. We latch the flag so we stop spamming the signer and mark all
    # outstanding LOADING records as failed so the panel can render the
    # locked state.
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    sync._bunker = MagicMock()
    for ident in ("a", "b", "c"):
        sync._store.upsert_skeleton(_wrap(ident=ident, event_id="e" + ident, ct="ct"))

    errors = []
    sync.bunker_error.connect(errors.append)

    sync._on_decrypt_failure(
        sync._generation, "a", "e-a", "method not found",
    )
    assert sync._bunker_unsupported is True
    assert errors, "bunker_error must be emitted on latch"
    for ident in ("a", "b", "c"):
        assert sync._store.get(ident).state is DraftState.FAILED


def test_per_call_failure_does_not_latch():
    # "user rejected" / generic permission denied must stay per-call,
    # not poison the entire profile.
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)
    sync._bunker = MagicMock()
    sync._store.upsert_skeleton(_wrap(ident="x", event_id="e", ct="ct"))

    sync._on_decrypt_failure(sync._generation, "x", "e", "user rejected the request")
    assert sync._bunker_unsupported is False
    assert sync._store.get("x").state is DraftState.FAILED


# --------------------------------------------------------------------------- #
# 5. _select_read_relays precedence                                           #
# --------------------------------------------------------------------------- #

def test_read_relays_prefer_user_read_over_write_over_bunker():
    rl = RelayList(write=["wss://w.example"], read=["wss://r.example"])
    result = _select_read_relays(rl, bunker_relays=["wss://b.example"])
    # Read first, then write, then bunker — most-trusted-user-choice first.
    assert result.index("wss://r.example") < result.index("wss://w.example")
    assert result.index("wss://w.example") < result.index("wss://b.example")


def test_read_relays_fall_back_to_defaults_when_nothing_specific():
    result = _select_read_relays(RelayList(write=[], read=[]), bunker_relays=())
    assert result  # never empty
    # The DEFAULT_RELAYS set lives in nostr/__init__.py — checking
    # length-non-zero is the contract.


def test_read_relays_deduplicate_case_and_slash():
    rl = RelayList(
        write=["wss://X.example/"],
        read=["wss://x.example", "wss://X.example/"],
    )
    result = _select_read_relays(rl, bunker_relays=())
    # All three normalize to one key.
    assert sum(1 for u in result if "x.example" in u.lower()) == 1


# --------------------------------------------------------------------------- #
# 6. ``_on_wrap_event`` filters foreign pubkeys                              #
# --------------------------------------------------------------------------- #

def test_on_wrap_event_drops_events_from_other_authors():
    # Defensive: a misbehaving relay could return events not matching
    # our author filter. DraftSync must not accept them, otherwise a
    # stranger's draft could end up keyed under the user's identity.
    sync = _make_sync()
    sync._profile = _make_profile(PK)
    sync._store.bind_profile(PK)

    event = {
        "kind": 31234,
        "tags": [["d", "x"], ["k", "1"]],
        "id": "stranger",
        "pubkey": OTHER_PK,
        "created_at": 1,
        "content": "ct",
    }
    sync._on_wrap_event(event)
    assert "x" not in sync._store
