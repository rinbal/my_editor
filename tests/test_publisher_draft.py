"""DraftPublishJob, DraftDeleteJob, _safe_reason — publisher hardening.

Covers the four publisher-tier defects flagged by the code review:
  - Plaintext size pre-flight rejects oversized drafts before
    spending a bunker round-trip + signer approval.
  - ``cancel()`` suppresses signal emissions so a destroyed dialog
    doesn't get post-teardown signal traffic.
  - ``DraftDeleteJob`` emits a ``tombstoned`` signal before the
    publish results land, mirroring ``DraftPublishJob.stashed``.
  - Signer-error strings are clipped via ``_safe_reason`` so a
    misbehaving signer can't echo plaintext through to a log line.
"""

from __future__ import annotations

import sys
from typing import List
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication

from nostr.drafts import (
    MAX_INNER_PAYLOAD_BYTES,
    build_inner_event,
)
from nostr.publisher import (
    DraftDeleteJob,
    DraftPublishJob,
    _safe_reason,
)


PK = "a" * 64


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def _make_profile(pubkey: str = PK):
    p = MagicMock()
    p.user_pubkey = pubkey
    p.bunker_pubkey = "b" * 64
    p.bunker_relays = ["wss://bunker.test/"]
    p.local_secret_hex = "0" * 64
    return p


# --------------------------------------------------------------------------- #
# Construction-time validation                                                #
# --------------------------------------------------------------------------- #

def test_draft_publish_job_rejects_pubkey_mismatch():
    profile = _make_profile(PK)
    foreign_inner = build_inner_event(kind=1, content="x", pubkey_hex="b" * 64)
    with pytest.raises(ValueError, match="does not match"):
        DraftPublishJob(
            relay_pool=MagicMock(),
            relay_list_cache=MagicMock(),
            session_pool=MagicMock(),
            profile=profile,
            inner_event=foreign_inner,
            identifier="x",
        )


def test_draft_publish_job_rejects_empty_identifier():
    profile = _make_profile()
    inner = build_inner_event(kind=1, content="x", pubkey_hex=PK)
    with pytest.raises(ValueError, match="identifier"):
        DraftPublishJob(
            relay_pool=MagicMock(), relay_list_cache=MagicMock(),
            session_pool=MagicMock(), profile=profile,
            inner_event=inner, identifier="",
        )


def test_draft_delete_job_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="unsupported inner kind"):
        DraftDeleteJob(
            relay_pool=MagicMock(), relay_list_cache=MagicMock(),
            session_pool=MagicMock(), profile=_make_profile(),
            identifier="x", inner_kind=9999,
        )


def test_draft_delete_job_rejects_empty_identifier():
    with pytest.raises(ValueError, match="identifier"):
        DraftDeleteJob(
            relay_pool=MagicMock(), relay_list_cache=MagicMock(),
            session_pool=MagicMock(), profile=_make_profile(),
            identifier="", inner_kind=1,
        )


# --------------------------------------------------------------------------- #
# Plaintext-size pre-flight                                                   #
# --------------------------------------------------------------------------- #

def test_oversized_plaintext_fails_before_bunker_call():
    profile = _make_profile()
    # A note whose serialized payload exceeds NIP-44's 65535-byte cap.
    huge = build_inner_event(
        kind=1,
        content="x" * (MAX_INNER_PAYLOAD_BYTES + 100),
        pubkey_hex=PK,
    )
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        inner_event=huge, identifier="big",
    )

    failures: List[str] = []
    job.failed.connect(failures.append)

    fake_bunker = MagicMock()
    job._on_bunker_ready(fake_bunker, publish_relays=["wss://r/"])

    fake_bunker.nip44_encrypt_self.assert_not_called()
    assert failures and "too large" in failures[0].lower()


def test_payload_within_cap_proceeds_to_encrypt():
    profile = _make_profile()
    small = build_inner_event(kind=1, content="hi", pubkey_hex=PK)
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        inner_event=small, identifier="ok",
    )
    fake_bunker = MagicMock()
    job._on_bunker_ready(fake_bunker, publish_relays=["wss://r/"])
    fake_bunker.nip44_encrypt_self.assert_called_once()


# --------------------------------------------------------------------------- #
# cancel() suppresses emissions                                               #
# --------------------------------------------------------------------------- #

def test_publish_job_cancel_silences_all_signals():
    profile = _make_profile()
    inner = build_inner_event(kind=1, content="hi", pubkey_hex=PK)
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        inner_event=inner, identifier="x",
    )
    captured = []
    job.status_changed.connect(lambda s: captured.append(("status", s)))
    job.failed.connect(lambda r: captured.append(("failed", r)))
    job.stashed.connect(lambda *a: captured.append(("stashed", a)))
    job.completed.connect(lambda r: captured.append(("completed", r)))

    job.cancel()
    job._emit_status("ignored")
    job._emit_failed("ignored")
    # Late callbacks from RPC layers must also be silenced
    job._on_relay_list_resolved(MagicMock(write=[], read=[]))
    job._on_signed({"id": "x", "kind": 31234, "created_at": 1, "pubkey": PK, "tags": [], "content": ""}, ["wss://r/"])
    job._on_publish_done([("wss://r/", True, "ok")])
    assert captured == []


def test_delete_job_cancel_silences_all_signals():
    profile = _make_profile()
    job = DraftDeleteJob(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        identifier="x", inner_kind=1,
    )
    captured = []
    job.tombstoned.connect(lambda *a: captured.append(a))
    job.failed.connect(lambda r: captured.append(("failed", r)))
    job.completed.connect(lambda r: captured.append(("completed", r)))

    job.cancel()
    job._emit_failed("ignored")
    job._on_relay_list_resolved(MagicMock(write=[], read=[]))
    job._on_signed(
        {"id": "tomb", "kind": 31234, "created_at": 1, "pubkey": PK, "tags": [], "content": ""},
        ["wss://r/"],
    )
    job._on_publish_done([("wss://r/", True, "ok")])
    assert captured == []


# --------------------------------------------------------------------------- #
# tombstoned signal ordering                                                  #
# --------------------------------------------------------------------------- #

def test_delete_job_emits_tombstoned_before_publish_completes():
    profile = _make_profile()
    job = DraftDeleteJob(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        identifier="x", inner_kind=1,
    )
    tomb_events = []
    completed = []
    job.tombstoned.connect(lambda d, eid: tomb_events.append((d, eid)))
    job.completed.connect(completed.append)

    signed = {
        "id": "evid-deadbeef",
        "kind": 31234,
        "created_at": 1,
        "pubkey": PK,
        "tags": [["d", "x"], ["k", "1"]],
        "content": "",
        "sig": "0" * 128,
    }
    job._relay_pool.publish.return_value = MagicMock()
    job._on_signed(signed, publish_relays=["wss://r/"])

    # ``tombstoned`` fires *before* completed — the panel must update
    # optimistically rather than waiting for the relay round-trip.
    assert tomb_events == [("x", "evid-deadbeef")]
    assert completed == []  # publish hasn't finished yet


# --------------------------------------------------------------------------- #
# _safe_reason                                                                #
# --------------------------------------------------------------------------- #

def test_safe_reason_passes_short_messages_through():
    assert _safe_reason("permission denied") == "permission denied"


def test_safe_reason_truncates_with_ellipsis():
    long = "x" * 5_000
    clipped = _safe_reason(long)
    assert len(clipped) <= 200
    assert clipped.endswith("…")


def test_safe_reason_empty_becomes_placeholder():
    # Empty strings would make UI status panels look broken; we
    # substitute a generic phrase so the failure path is never silent.
    assert _safe_reason("") == "unknown error"
    assert _safe_reason(None) == "unknown error"


# --------------------------------------------------------------------------- #
# stashed signal fires before publish completes                               #
# --------------------------------------------------------------------------- #

def test_publish_job_emits_stashed_before_publish_completes():
    profile = _make_profile()
    inner = build_inner_event(kind=1, content="hi", pubkey_hex=PK)
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_list_cache=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        inner_event=inner, identifier="x",
    )
    stash_events = []
    completed = []
    job.stashed.connect(lambda d, eid, ts: stash_events.append((d, eid, ts)))
    job.completed.connect(completed.append)

    signed = {
        "id": "evid-stashed",
        "kind": 31234,
        "created_at": 42,
        "pubkey": PK,
        "tags": [["d", "x"], ["k", "1"]],
        "content": "ct",
        "sig": "0" * 128,
    }
    job._relay_pool.publish.return_value = MagicMock()
    job._on_signed(signed, publish_relays=["wss://r/"])
    assert stash_events == [("x", "evid-stashed", 42)]
    assert completed == []
