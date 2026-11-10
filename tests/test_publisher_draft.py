# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DraftPublishJob, DraftDeleteJob, _safe_reason: publisher hardening.

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
from nostr.signer_limits import SIGNER_NIP44_MAX_PLAINTEXT, nip44_plaintext_limit
from nostr.publisher import (
    DraftDeleteJob,
    DraftPublishJob,
    _safe_reason,
    draft_size_problem,
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
            relay_directory=MagicMock(),
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
            relay_pool=MagicMock(), relay_directory=MagicMock(),
            session_pool=MagicMock(), profile=profile,
            inner_event=inner, identifier="",
        )


def test_draft_delete_job_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="unsupported inner kind"):
        DraftDeleteJob(
            relay_pool=MagicMock(), relay_directory=MagicMock(),
            session_pool=MagicMock(), profile=_make_profile(),
            identifier="x", inner_kind=9999,
        )


def test_draft_delete_job_rejects_empty_identifier():
    with pytest.raises(ValueError, match="identifier"):
        DraftDeleteJob(
            relay_pool=MagicMock(), relay_directory=MagicMock(),
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
        relay_pool=MagicMock(), relay_directory=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        inner_event=huge, identifier="big",
    )

    failures: List[str] = []
    job.failed.connect(failures.append)

    fake_bunker = MagicMock()
    job._on_bunker_ready(fake_bunker, publish_relays=["wss://r/"])

    fake_bunker.nip44_encrypt_self.assert_not_called()
    # Too long for NIP-44 itself: the key kept here would not help either.
    assert failures == ["Your signer app can’t carry a draft this long: split it into smaller "
                        "drafts or publish it directly."]


# -- the limit depends on who encrypts (M1b) -----------------------------------

def _note_of_bytes(size: int) -> dict:
    """A short note whose serialized inner event is exactly ``size`` bytes."""
    empty = build_inner_event(kind=1, content="", pubkey_hex=PK, created_at=1)
    from nostr.drafts import serialize_inner_event
    frame = len(serialize_inner_event(empty).encode("utf-8"))
    return build_inner_event(kind=1, content="x" * (size - frame), pubkey_hex=PK, created_at=1)


def _local_profile():
    profile = _make_profile()
    profile.signer = "local"
    return profile


def test_the_limit_is_nip44s_with_a_key_here_and_lower_through_a_signer_app():
    assert nip44_plaintext_limit(_local_profile()) == MAX_INNER_PAYLOAD_BYTES == 65535
    assert nip44_plaintext_limit(_make_profile()) == SIGNER_NIP44_MAX_PLAINTEXT == 40_960
    from types import SimpleNamespace
    assert nip44_plaintext_limit(SimpleNamespace(signer="remote")) == SIGNER_NIP44_MAX_PLAINTEXT


@pytest.mark.parametrize("size, remote, local", [
    (SIGNER_NIP44_MAX_PLAINTEXT, None, None),
    (SIGNER_NIP44_MAX_PLAINTEXT + 1, "signer app", None),
    (MAX_INNER_PAYLOAD_BYTES, "signer app", None),
    (MAX_INNER_PAYLOAD_BYTES + 1, "signer app", "too long to be encrypted"),
])
def test_a_draft_is_measured_against_what_its_signer_can_encrypt(size, remote, local):
    inner = _note_of_bytes(size)
    for profile, words in ((_make_profile(), remote), (_local_profile(), local)):
        problem = draft_size_problem(inner, profile)
        if words is None:
            assert problem is None
        else:
            assert words in problem and "split it into smaller drafts" in problem
            assert problem.count(".") == 1 and "000" not in problem      # never the numbers


def test_a_draft_too_large_for_a_signer_app_fails_before_anything_is_sent():
    # Between 40,961 and 65,535 bytes a signer app cannot encrypt the
    # draft: said at once, in words, with the real limit.
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(),
        session_pool=MagicMock(**{"key_here.return_value": False}),
        profile=_make_profile(), inner_event=_note_of_bytes(50_000), identifier="big")
    failures: List[str] = []
    job.failed.connect(failures.append)
    fake_bunker = MagicMock()
    job._on_bunker_ready(fake_bunker, publish_relays=["wss://r/"])
    fake_bunker.nip44_encrypt_self.assert_not_called()
    assert failures == ["Your signer app can’t carry a draft this long: split it into smaller "
                        "drafts or publish it directly."]


def test_with_the_key_also_kept_here_the_words_offer_it():
    # Contract 12.5 (d5cc9a7): MyEditor offers its local key when one is
    # kept for the account.
    pool = MagicMock(**{"key_here.return_value": True})
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(), session_pool=pool,
        profile=_make_profile(), inner_event=_note_of_bytes(50_000), identifier="big")
    failures: List[str] = []
    job.failed.connect(failures.append)
    job._on_bunker_ready(MagicMock(), publish_relays=["wss://r/"])
    assert failures == ["Your signer app can’t carry a draft this long: split it into smaller "
                        "drafts, or save it with your key kept on this computer."]
    pool.key_here.assert_called_once_with(PK)


def test_the_same_draft_with_the_key_here_is_encrypted():
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(), session_pool=MagicMock(),
        profile=_local_profile(), inner_event=_note_of_bytes(50_000), identifier="big")
    fake_signer = MagicMock()
    job._on_bunker_ready(fake_signer, publish_relays=["wss://r/"])
    fake_signer.nip44_encrypt_self.assert_called_once()


def test_the_signer_apps_limit_is_said_in_german_too():
    import i18n
    i18n.install("de")
    try:
        problem = draft_size_problem(_note_of_bytes(50_000), _make_profile())
    finally:
        i18n.install("en")
    assert problem == ("Deine Signer-App kann keinen so langen Entwurf übertragen: Teile ihn "
                       "in kleinere Entwürfe auf oder veröffentliche ihn direkt.")


def test_payload_within_cap_proceeds_to_encrypt():
    profile = _make_profile()
    small = build_inner_event(kind=1, content="hi", pubkey_hex=PK)
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(),
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
        relay_pool=MagicMock(), relay_directory=MagicMock(),
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
    job._on_relays_ready(["wss://r/"])
    job._on_signed({"id": "x", "kind": 31234, "created_at": 1, "pubkey": PK, "tags": [], "content": ""}, ["wss://r/"])
    job._on_publish_done([("wss://r/", True, "ok")])
    assert captured == []


def test_delete_job_cancel_silences_all_signals():
    profile = _make_profile()
    job = DraftDeleteJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(),
        session_pool=MagicMock(), profile=profile,
        identifier="x", inner_kind=1,
    )
    captured = []
    job.tombstoned.connect(lambda *a: captured.append(a))
    job.failed.connect(lambda r: captured.append(("failed", r)))
    job.completed.connect(lambda r: captured.append(("completed", r)))

    job.cancel()
    job._emit_failed("ignored")
    job._on_relays_ready(["wss://r/"])
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
        relay_pool=MagicMock(), relay_directory=MagicMock(),
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

    # ``tombstoned`` fires *before* completed, so the panel must update
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
        relay_pool=MagicMock(), relay_directory=MagicMock(),
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


def test_the_save_command_says_the_signer_apps_limit_before_starting(monkeypatch):
    """The editor's Save as Draft asks the same helper, so a draft a
    signer app cannot encrypt is refused in words before any job starts."""
    import types
    import main_window
    from main_window import MainWindow
    shown, made = [], []
    monkeypatch.setattr(main_window, "inform", lambda parent, **kw: shown.append(kw))
    monkeypatch.setattr(main_window, "DraftPublishJob", lambda **kw: made.append(kw))
    host = types.SimpleNamespace(_is_stash_in_flight_for=lambda _ed: False,
                                 _key_vault=types.SimpleNamespace(has=lambda _pk: False))
    choice = types.SimpleNamespace(identifier="big")
    MainWindow._fire_draft_publish_job(host, object(), _make_profile(), choice,
                                       _note_of_bytes(50_000))
    assert made == []
    assert [kw["title"] for kw in shown] == ["Your draft wasn’t saved"]
    assert shown[0]["message"] == ("Your signer app can’t carry a draft this long: split it "
                                   "into smaller drafts or publish it directly.")
    # The numbers only behind Details (lessons 26, 36).
    assert "40,960" in shown[0]["details"] and "40,960" not in shown[0]["message"]


def _note_of_quotes(count: int) -> dict:
    """A short note of ``count`` quotes: two bytes each in the draft's
    plaintext, four in a signer app's answer to ``nip44_decrypt``."""
    return build_inner_event(kind=1, content='"' * count, pubkey_hex=PK, created_at=1)


def test_a_draft_the_signer_app_could_encrypt_but_not_hand_back_is_refused():
    # RG-M0 R7: 40,0xx bytes pass the 40,960 limit, but the answer that
    # carries them back would be 80,0xx bytes: the draft would be saved
    # and never read again.
    from nostr.drafts import serialize_inner_event
    inner = _note_of_quotes(20_000)
    assert len(serialize_inner_event(inner).encode("utf-8")) <= SIGNER_NIP44_MAX_PLAINTEXT
    problem = draft_size_problem(inner, _make_profile())
    assert problem == ("Your signer app can’t carry a draft this long: split it into smaller "
                       "drafts or publish it directly.")
    # With the key on this computer nothing is handed back.
    assert draft_size_problem(inner, _local_profile()) is None
    # And a draft of quotes that does come back is saved.
    assert draft_size_problem(_note_of_quotes(16_000), _make_profile()) is None


def test_such_a_draft_fails_before_anything_is_sent():
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(), session_pool=MagicMock(),
        profile=_make_profile(), inner_event=_note_of_quotes(20_000), identifier="quotes")
    failures: List[str] = []
    job.failed.connect(failures.append)
    fake_bunker = MagicMock()
    job._on_bunker_ready(fake_bunker, publish_relays=["wss://r/"])
    fake_bunker.nip44_encrypt_self.assert_not_called()
    assert len(failures) == 1 and failures[0].startswith(
        "Your signer app can’t carry a draft this long:")
