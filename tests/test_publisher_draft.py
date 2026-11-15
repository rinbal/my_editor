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
from PySide6.QtCore import QCoreApplication, QObject, Signal

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
    assert job.can_retry is False and job.words == failures[0]   # trying again can't help


def test_a_job_that_was_not_signed_says_the_signers_sentence():
    job = DraftPublishJob(
        relay_pool=MagicMock(), relay_directory=MagicMock(), session_pool=MagicMock(),
        profile=_make_profile(), inner_event=build_inner_event(kind=1, content="hi",
                                                              pubkey_hex=PK),
        identifier="x")
    failures: List[str] = []
    job.failed.connect(failures.append)
    fake_bunker = MagicMock()
    job._on_bunker_ready(fake_bunker, publish_relays=["wss://r/"])
    on_failure = fake_bunker.nip44_encrypt_self.call_args.kwargs["on_failure"]
    on_failure("user rejected the request")
    # What the signer said stays for Details; the words are the shared one.
    assert failures == ["Could not encrypt draft: user rejected the request"]
    assert job.words == "Your signer app declined the request: allow it there and try again."
    assert job.can_retry is True


class _SaveJob(QObject):
    """Stands in for DraftPublishJob in the window: the signals it fires,
    in its order, by the test."""

    status_changed = Signal(str)
    signed = Signal(dict)
    stashed = Signal(str, str, int)
    completed = Signal(list)
    failed = Signal(str)
    made: List["_SaveJob"] = []

    def __init__(self, **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.sent_again = None
        self.started = False
        self.words, self.can_retry = "", True
        _SaveJob.made.append(self)

    def start(self):
        self.started = True

    def send_signed(self, signed):
        self.sent_again = signed


def _save_host(monkeypatch, *, try_again=False):
    """The window's draft save, its alerts and what it marks saved."""
    import types
    import main_window
    from main_window import MainWindow
    _SaveJob.made = []
    monkeypatch.setattr(main_window, "DraftPublishJob", _SaveJob)
    host = types.SimpleNamespace(
        said=[], informed=[], saved=[], statuses=[],
        _is_stash_in_flight_for=lambda ed: getattr(ed, "_active_stash_job", None) is not None,
        _key_vault=types.SimpleNamespace(has=lambda _pk: True),
        _relay_pool=None, _relay_directory=None, _session_pool=None,
        _entitled_relays=lambda: ())
    host.status = types.SimpleNamespace(
        showMessage=lambda text, _ms, **kw: host.statuses.append(text))
    host._on_draft_stashed = lambda ed, choice, eid, ts, inner: host.saved.append((eid, ts))
    for name in ("_draft_job", "_fire_draft_publish_job", "_attach_active_stash"):
        setattr(host, name, types.MethodType(getattr(MainWindow, name), host))
    monkeypatch.setattr(main_window, "ask_try_again",
                        lambda parent, **kw: host.said.append(kw) or try_again)
    monkeypatch.setattr(main_window, "inform", lambda parent, **kw: host.informed.append(kw))
    return host


def _save(host):
    import types
    ed = types.SimpleNamespace()
    choice = types.SimpleNamespace(identifier="x")
    from main_window import MainWindow
    MainWindow._fire_draft_publish_job(host, ed, _local_profile(), choice,
                                       build_inner_event(kind=1, content="hi", pubkey_hex=PK))
    return ed, _SaveJob.made[-1]


WRAP = {"id": "e" * 64, "created_at": 42, "kind": 31234}


def test_a_draft_is_saved_only_once_a_relay_keeps_it(monkeypatch):
    # Lessons 9 and 17: signed is not saved; the tab is marked saved once a
    # relay kept it, and never how many relays did (lessons 36).
    host = _save_host(monkeypatch)
    ed, job = _save(host)
    assert job.started
    job.signed.emit(WRAP)
    job.stashed.emit("x", WRAP["id"], 42)
    assert host.saved == []
    job.completed.emit([("wss://a/", False, "no answer"), ("wss://b/", True, "")])
    assert host.saved == [(WRAP["id"], 42)] and host.statuses[-1] == "Draft saved."
    assert host.said == [] and ed._active_stash_job is None


def test_a_draft_no_relay_kept_is_said_once_and_try_again_sends_the_same_wrap(monkeypatch):
    host = _save_host(monkeypatch, try_again=True)
    ed, job = _save(host)
    job.signed.emit(WRAP)
    job.stashed.emit("x", WRAP["id"], 42)
    job.completed.emit([("wss://a/", False, "blocked: members only"),
                        ("wss://b/", False, "no answer")])
    # Never marked saved; what did not happen, the cause, the relays behind
    # Details; Try Again sends the very wrap signed, no signer prompt.
    assert host.saved == [] and "Draft saved." not in host.statuses
    asked, = host.said
    assert asked["title"] == "Your draft wasn’t saved"
    assert asked["message"] == "The servers your posts go to turned it down: try again later."
    assert "wss://a/: blocked: members only" in asked["details"]
    again = _SaveJob.made[-1]
    assert again is not job and again.sent_again == WRAP and not again.started
    assert ed._active_stash_job is again
    again.stashed.emit("x", WRAP["id"], 42)
    again.completed.emit([("wss://b/", False, "no answer")])
    assert host.said[-1]["message"] == "Check your internet connection and try again."
    assert _SaveJob.made[-1].sent_again == WRAP        # the same wrap, every time


def test_a_draft_not_sent_is_said_by_its_words_and_try_again_starts_again(monkeypatch):
    host = _save_host(monkeypatch, try_again=True)
    ed, job = _save(host)
    job.words = "Your signer app didn’t answer: open it and try again."
    job.failed.emit("timed out waiting for signer")
    asked, = host.said[:1]
    assert asked["title"] == "Your draft wasn’t saved"
    assert asked["message"] == job.words and asked["details"] == "timed out waiting for signer"
    assert len(_SaveJob.made) == 2 and _SaveJob.made[-1].started    # a new save, not refused
    assert host.saved == []


def test_a_draft_trying_again_cannot_help_is_said_with_ok_only(monkeypatch):
    host = _save_host(monkeypatch, try_again=True)
    _ed, job = _save(host)
    job.words, job.can_retry = "Your signer app can’t carry a draft this long: split it.", False
    job.failed.emit(job.words)
    assert host.said == [] and len(_SaveJob.made) == 1
    assert host.informed[0]["title"] == "Your draft wasn’t saved"
