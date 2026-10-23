# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source followed just before quitting still reaches the relays.

The list of sources is published five seconds after the last change. On
quit it is flushed, but a key kept on this computer signs on the next
turn of the event loop, and the window closed the signer right after
the flush: the change never left. Quitting now waits for it, a few
seconds at most, before the signer and the relay sockets close.
"""

from __future__ import annotations

import tempfile

from PySide6.QtCore import QTimer

from main_window import QUIT_SYNC_WAIT_MS, MainWindow
from nostr.imports.subscriptions import FeedSubscriptionStore
from tests.outbox_fakes import FakeRelayDirectory, settle
from tests.test_imports_subscriptions import (
    PROFILE,
    FakeRelay,
    FakeScheduler,
    FakeSessionPool,
    FakeSigner,
)
import pytest
from tests.widget_lifetime import delete_new_windows


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every window and panel a test makes is deleted after it: left to
    the cycle collector, one without a parent can crash it."""
    yield from delete_new_windows()


class NextTurnSigner(FakeSigner):
    """Signs on the next turn of the event loop, as LocalSigner does."""

    def sign_event(self, unsigned, on_success, on_failure, **_kw):
        QTimer.singleShot(0, lambda: FakeSigner.sign_event(
            self, unsigned, on_success, on_failure))


def store_with(relay):
    store = FeedSubscriptionStore(
        session_pool=FakeSessionPool(NextTurnSigner()), relay_pool=None,
        relay_directory=FakeRelayDirectory(), cache_dir=tempfile.mkdtemp(prefix="quit-test-"),
        query=relay, publisher=relay, scheduler=FakeScheduler(),
        clock=lambda: 1_800_000_000)
    store.bind_profile(PROFILE)
    settle()
    return store


def test_a_source_added_just_before_quitting_is_sent():
    relay = FakeRelay()
    store = store_with(relay)
    store.add_feed("https://blog.example/feed")
    store.flush()
    assert store.is_busy                           # on its way
    assert store.wait_until_settled(QUIT_SYNC_WAIT_MS) is True
    assert [f["url"] for f in relay.payload()["feeds"]] == ["https://blog.example/feed"]


def test_nothing_to_send_means_no_wait():
    store = store_with(FakeRelay())
    store.flush()
    assert not store.is_busy


def closing_host(monkeypatch, order):
    """A stand-in for the main window with what closeEvent touches;
    ``order`` records the steps that matter for the list of sources."""
    import types
    from PySide6.QtWidgets import QMainWindow, QTabWidget
    from nostr.ui.context_widgets import ContextStatusBar
    import main_window
    monkeypatch.setattr(main_window.workspace, "discard_workspace", lambda: None)
    host = QMainWindow()

    class Subscriptions:
        is_busy = True

        def wait_until_settled(self, ms):
            order.append(("wait", host.isEnabled(), host.status.currentMessage()))
            return True

    host._imports = types.SimpleNamespace(flush=lambda: order.append("flush"),
                                          shutdown=lambda: order.append("inbox closed"),
                                          subscriptions=Subscriptions())
    host._resolve_unsaved_before_closing = lambda: True
    host._asset_manager = types.SimpleNamespace(flush=lambda: None)
    host._media_saver = types.SimpleNamespace(cancel=lambda: order.append("saves stopped"))
    host._save_session = lambda: None
    host._remember_window_geometry = lambda: None
    host._release_file_tabs = lambda: None
    host.tabs = QTabWidget()
    host.status = ContextStatusBar()
    host._session_pool = types.SimpleNamespace(
        close_all=lambda: order.append("signer closed"))
    host._relay_pool = types.SimpleNamespace(close_all=lambda: order.append("relays closed"))
    return host


def test_quitting_waits_before_closing_the_signer(monkeypatch):
    """The list is sent, quitting waits for it, and only then the signer
    goes (review L16: this used to read closeEvent's source)."""
    from PySide6.QtGui import QCloseEvent
    order = []
    MainWindow.closeEvent(closing_host(monkeypatch, order), QCloseEvent())
    steps = [step if isinstance(step, str) else step[0] for step in order]
    assert steps.index("flush") < steps.index("wait") < steps.index("signer closed")
    # The inbox closes, and its lock goes, once the list is sent (CP2 live
    # run: the lock was left to its destructor, after the exit handlers).
    assert steps.index("wait") < steps.index("inbox closed")


def test_nothing_can_be_typed_while_quitting_waits(monkeypatch):
    """Engine review M2: while quitting waited for the list of sources,
    the window stayed editable and what was typed then was lost without
    a word. It takes no more input while it waits."""
    from PySide6.QtGui import QCloseEvent
    order = []
    MainWindow.closeEvent(closing_host(monkeypatch, order), QCloseEvent())
    waits = [step for step in order if isinstance(step, tuple)]
    assert waits == [("wait", False, "Saving your list of sources\u2026")]
