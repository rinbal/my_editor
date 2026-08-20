# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the panel shows when the signer stops answering.

The observed failure was a panel that read "Decrypting 0 of 54" for as
long as anyone cared to watch. Every part of that line was false: no
decryption was in flight, none was queued, and none was going to happen.
The user was never told that the thing to fix was in their pocket.

So: the status line has to lose to nothing except an incapable signer,
the explanation has to carry a way out, and it must not take the screen
away from drafts that did decrypt before the signer went quiet.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.draft_store import DraftStore
from nostr.drafts import DraftWrapMeta
from nostr.profiles import Profile
from nostr.ui.drafts_panel import DraftsPanel

PK = "a" * 64


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def make_profile() -> Profile:
    return Profile(
        user_pubkey=PK, bunker_pubkey="b" * 64,
        bunker_relays=["wss://relay.example"], local_secret_hex="c" * 64,
        display_name="Alice",
    )


def wrap(identifier: str, created_at: int = 1700000000) -> DraftWrapMeta:
    return DraftWrapMeta(
        identifier=identifier, inner_kind=0, event_id=identifier + "e",
        pubkey=PK, created_at=created_at, expiration=None, ciphertext="ct",
    )


def locked_store(count: int = 54) -> DraftStore:
    """Every draft present, none of them readable. The reported state."""
    store = DraftStore()
    store.bind_profile(PK)
    for i in range(count):
        store.upsert_skeleton(wrap(f"d{i}", created_at=1700000000 + i))
    return store


def panel(store: DraftStore) -> DraftsPanel:
    p = DraftsPanel()
    p.set_active_profile(make_profile())
    p.bind_store(store)
    return p


def status_of(p: DraftsPanel):
    return p._status_line()


def showing_placeholder(p: DraftsPanel) -> bool:
    return p._body_stack.currentIndex() == 1


# --------------------------------------------------------------------- #
# The status line                                                       #
# --------------------------------------------------------------------- #

def test_the_reported_bug_the_line_claims_progress_that_cannot_happen():
    p = panel(locked_store())
    assert status_of(p) == ("Decrypting 0 of 54", False)

    p.set_signer_unreachable(True)
    text, is_error = status_of(p)
    assert "Decrypting" not in text
    assert "signer" in text.lower() and is_error


def test_it_outranks_the_failure_count_too():
    # Once we give up, every row is FAILED, and "54 drafts could not be
    # decrypted" is true but describes the symptom, not the cause.
    store = locked_store(3)
    for i in range(3):
        store.set_failed(f"d{i}", "your signer did not answer")
    p = panel(store)
    assert status_of(p)[1] is True

    p.set_signer_unreachable(True)
    assert "signer is not responding" in status_of(p)[0].lower()


def test_a_stale_narration_line_does_not_outrank_the_explanation():
    # ``_sync_message`` wins over the counts and lives on a TTL, so
    # without clearing it the user gets "Refreshing drafts" during the
    # exact window they are looking for a reason.
    p = panel(locked_store(2))
    p.set_status("Refreshing drafts…")
    assert status_of(p)[0] == "Refreshing drafts…"

    p.set_signer_unreachable(True)
    assert "signer is not responding" in status_of(p)[0].lower()


def test_an_incapable_signer_still_wins_because_no_retry_can_help():
    p = panel(locked_store(2))
    p.set_signer_unreachable(True)
    p.set_signer_unsupported(True)
    assert "NIP-44" in status_of(p)[0]


def test_clearing_it_returns_the_line_to_normal():
    p = panel(locked_store(2))
    p.set_signer_unreachable(True)
    p.set_signer_unreachable(False)
    assert status_of(p) == ("Decrypting 0 of 2", False)


# --------------------------------------------------------------------- #
# The way out                                                           #
# --------------------------------------------------------------------- #

def test_nothing_readable_gets_an_explanation_and_a_button():
    p = panel(locked_store())
    p.set_signer_unreachable(True)
    assert showing_placeholder(p)
    assert "not responding" in p._empty_title.text().lower()
    assert p._empty_action.isVisible() or p._empty_action.isVisibleTo(p)
    assert p._empty_action.text() == "Try again"


def test_the_body_says_what_to_do_not_what_broke():
    p = panel(locked_store())
    p.set_signer_unreachable(True)
    body = p._empty_body.text().lower()
    assert "signer" in body and "try again" in body


def test_the_button_asks_the_host_to_retry():
    p = panel(locked_store())
    p.set_signer_unreachable(True)
    seen = []
    p.retry_signer.connect(lambda: seen.append(True))
    p._empty_action.click()
    assert seen == [True]


def test_drafts_that_did_decrypt_keep_the_screen():
    # A signer that dies halfway leaves readable work on screen, and
    # replacing it with an explanation takes away more than it gives.
    store = locked_store(4)
    store.set_decrypted("d0", inner={"kind": 1, "content": "Readable", "tags": []})
    p = panel(store)
    p.set_signer_unreachable(True)
    assert not showing_placeholder(p)
    # The line still carries the message, and each row keeps its retry.
    assert "signer is not responding" in status_of(p)[0].lower()


def test_recovering_puts_the_list_back():
    p = panel(locked_store())
    p.set_signer_unreachable(True)
    assert showing_placeholder(p)
    p.set_signer_unreachable(False)
    assert not showing_placeholder(p)


# --------------------------------------------------------------------- #
# The shared button                                                     #
# --------------------------------------------------------------------- #

def test_the_search_placeholder_still_clears_the_search():
    # The action button now dispatches, where it used to be wired to
    # ``_search_edit.clear`` for the life of the panel.
    store = locked_store(2)
    store.set_decrypted("d0", inner={"kind": 1, "content": "Pineapple", "tags": []})
    p = panel(store)
    p._search_edit.setText("zzzznomatch")
    assert showing_placeholder(p)
    assert p._empty_action.text() == "Clear search"

    p._empty_action.click()
    assert p._search_edit.text() == ""
    assert not showing_placeholder(p)


def test_the_button_does_not_keep_the_previous_branch_action():
    # Both branches share one button, so a handler left connected from
    # the other one would fire the wrong thing.
    p = panel(locked_store())
    p.set_signer_unreachable(True)
    seen = []
    p.retry_signer.connect(lambda: seen.append(True))

    p.set_signer_unreachable(False)
    p._search_edit.setText("zzzznomatch")
    p._empty_action.click()
    assert seen == []
    assert p._search_edit.text() == ""


def test_a_placeholder_with_no_remedy_hides_the_button():
    p = DraftsPanel()
    p.set_active_profile(None)
    assert showing_placeholder(p)
    assert not p._empty_action.isVisibleTo(p)
