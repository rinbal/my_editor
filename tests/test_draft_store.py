# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DraftStore — emit semantics, stale-event rejection, profile lifecycle.

These tests pin behaviour that an upstream code-review pass identified
as defect-prone: emit order on insert (added before changed), stale
source_event_id rejection so an old decryption can't briefly overwrite
a newer one, case-folded profile binding, and tombstone removal.
"""

from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QCoreApplication

from nostr.draft_store import DraftRecord, DraftState, DraftStore
from nostr.drafts import (
    INNER_KIND_LONG_FORM,
    INNER_KIND_SHORT_NOTE,
    DraftWrapMeta,
    build_inner_event,
)


PK = "a" * 64
OTHER_PK = "b" * 64


# QCoreApplication is required even for signal-only QObjects — without
# it Qt complains about emit-on-no-loop. A module-scoped fixture keeps
# pytest happy without forcing every test to manage its own app.
@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


# --------------------------------------------------------------------------- #
# Signal-capture helper                                                       #
# --------------------------------------------------------------------------- #

class _Capture:
    """Records (signal_name, identifier) pairs in arrival order."""

    def __init__(self, store: DraftStore) -> None:
        self.events: list[tuple[str, str]] = []
        store.record_added.connect(lambda d: self.events.append(("added", d)))
        store.record_changed.connect(lambda d: self.events.append(("changed", d)))
        store.record_removed.connect(lambda d: self.events.append(("removed", d)))
        store.cleared.connect(lambda: self.events.append(("cleared", "")))
        store.loading_state_changed.connect(
            lambda v: self.events.append(("loading", str(bool(v))))
        )


def _wrap_meta(
    *, ident: str, kind: int = 1, event_id: str = "eid", pubkey: str = PK,
    created_at: int = 100, ciphertext: str = "ct",
) -> DraftWrapMeta:
    return DraftWrapMeta(
        identifier=ident, inner_kind=kind, event_id=event_id,
        pubkey=pubkey, created_at=created_at,
        expiration=None, ciphertext=ciphertext,
    )


# --------------------------------------------------------------------------- #
# Emit order on optimistic insert                                             #
# --------------------------------------------------------------------------- #

def test_upsert_from_inner_emits_added_before_changed():
    # The drafts panel relies on this order so it can ``insertRow`` before
    # ``itemChanged`` arrives — reverse order would crash a view that
    # calls ``index(identifier)`` from its changed handler.
    store = DraftStore()
    store.bind_profile(PK)
    cap = _Capture(store)

    inner = build_inner_event(kind=1, content="hello", pubkey_hex=PK)
    store.upsert_from_inner(
        identifier="note-1", inner=inner,
        event_id="eid-v1", created_at=100, expiration=None,
    )
    # First event after the bind must be ``added``, then ``changed``.
    structural = [e for e in cap.events if e[0] in ("added", "changed")]
    assert structural == [("added", "note-1"), ("changed", "note-1")]


# --------------------------------------------------------------------------- #
# Stale-decryption rejection                                                  #
# --------------------------------------------------------------------------- #

def test_set_decrypted_rejects_stale_source_event_id():
    # Scenario: wrap v1 ciphertext is in-flight at the bunker; wrap v2
    # arrives at the relay subscription and updates record.event_id to
    # 'eid-v2'. The lagging v1 decryption then resolves. set_decrypted
    # must NOT overwrite the v2 record with v1 content.
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x", event_id="eid-v1", created_at=100))
    store.upsert_skeleton(_wrap_meta(ident="x", event_id="eid-v2", created_at=200))
    # Record now points at v2.
    record = store.get("x")
    assert record.event_id == "eid-v2"

    cap = _Capture(store)
    stale_inner = build_inner_event(kind=1, content="STALE", pubkey_hex=PK)
    store.set_decrypted("x", inner=stale_inner, source_event_id="eid-v1")
    assert not any(e[0] == "changed" for e in cap.events)
    assert store.get("x").content == ""  # body never populated


def test_set_decrypted_accepts_matching_source_event_id():
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x", event_id="eid-v1", created_at=100))
    inner = build_inner_event(kind=1, content="fresh", pubkey_hex=PK)
    store.set_decrypted("x", inner=inner, source_event_id="eid-v1")
    assert store.get("x").state is DraftState.READY
    assert store.get("x").content == "fresh"


def test_set_decrypted_without_source_id_still_writes():
    # Optimistic update path doesn't necessarily know the event_id yet —
    # absence of source_event_id means "trust the caller".
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x"))
    inner = build_inner_event(kind=1, content="opt", pubkey_hex=PK)
    store.set_decrypted("x", inner=inner)
    assert store.get("x").content == "opt"


# --------------------------------------------------------------------------- #
# Profile-switch lifecycle                                                    #
# --------------------------------------------------------------------------- #

def test_bind_profile_is_case_insensitive_noop():
    # A common bug surface: the active profile stores lowercased hex,
    # but a caller passing the uppercase form would have triggered an
    # unnecessary store reset.
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x"))

    cap = _Capture(store)
    store.bind_profile(PK.upper())
    assert not any(e[0] in ("cleared", "removed") for e in cap.events)
    assert "x" in store


def test_bind_profile_to_different_pubkey_clears_records():
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x"))
    assert len(store) == 1
    store.bind_profile(OTHER_PK)
    assert len(store) == 0


def test_bind_to_none_clears():
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x"))
    store.bind_profile(None)
    assert len(store) == 0
    assert store.profile_pubkey is None


# --------------------------------------------------------------------------- #
# Tombstone handling                                                          #
# --------------------------------------------------------------------------- #

def test_tombstone_removes_existing_record():
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x", created_at=10, ciphertext="alive"))
    cap = _Capture(store)
    store.upsert_skeleton(_wrap_meta(ident="x", created_at=20, ciphertext=""))
    assert ("removed", "x") in cap.events
    assert "x" not in store


def test_tombstone_for_nonexistent_record_is_noop():
    store = DraftStore()
    store.bind_profile(PK)
    cap = _Capture(store)
    store.upsert_skeleton(_wrap_meta(ident="never-seen", ciphertext=""))
    assert not any(e[0] in ("added", "changed", "removed") for e in cap.events)


# --------------------------------------------------------------------------- #
# Stale wrap arrival (older created_at)                                       #
# --------------------------------------------------------------------------- #

def test_older_wrap_does_not_disturb_newer_record():
    # If two relays return wraps out of order, the older one must not
    # reset the record back to LOADING or overwrite event metadata.
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x", event_id="new", created_at=200))
    store.upsert_skeleton(_wrap_meta(ident="x", event_id="old", created_at=100))
    record = store.get("x")
    assert record.event_id == "new"
    assert record.created_at == 200


# --------------------------------------------------------------------------- #
# DraftRecord helpers                                                         #
# --------------------------------------------------------------------------- #

def test_draftrecord_is_article_and_is_note_properties():
    note = DraftRecord(identifier="x", inner_kind=INNER_KIND_SHORT_NOTE)
    article = DraftRecord(identifier="y", inner_kind=INNER_KIND_LONG_FORM)
    unknown = DraftRecord(identifier="z", inner_kind=9999)
    assert note.is_note and not note.is_article
    assert article.is_article and not article.is_note
    assert not unknown.is_article and not unknown.is_note


# --------------------------------------------------------------------------- #
# Ordering: ``all()`` returns newest first                                    #
# --------------------------------------------------------------------------- #

def test_all_returns_records_newest_first():
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="oldest", created_at=100))
    store.upsert_skeleton(_wrap_meta(ident="newest", created_at=300))
    store.upsert_skeleton(_wrap_meta(ident="middle", created_at=200))
    ids = [r.identifier for r in store.all()]
    assert ids == ["newest", "middle", "oldest"]


# --------------------------------------------------------------------------- #
# Failure state                                                               #
# --------------------------------------------------------------------------- #

def test_set_failed_transitions_state_and_emits_changed():
    store = DraftStore()
    store.bind_profile(PK)
    store.upsert_skeleton(_wrap_meta(ident="x"))
    cap = _Capture(store)
    store.set_failed("x", "decryption error")
    assert store.get("x").state is DraftState.FAILED
    assert store.get("x").failure_reason == "decryption error"
    assert ("changed", "x") in cap.events
