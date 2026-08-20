# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nostr.imports.workers.run_blocking``.

Verifies the two contract points that matter to callers: the function
runs off the calling thread, and the callback is delivered back ON the
calling thread (so UI code can touch widgets from it).
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from nostr.imports.workers import run_blocking


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def _pump_until(app, predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for callback delivery")
        app.processEvents()
        time.sleep(0.005)


def test_result_delivered_on_calling_thread(qt_app):
    main_thread = threading.get_ident()
    seen = {}

    def work():
        seen["worker_thread"] = threading.get_ident()
        return 21 * 2

    run_blocking(work, lambda r: seen.update(result=r,
                                             cb_thread=threading.get_ident()))
    _pump_until(qt_app, lambda: "result" in seen)

    assert seen["result"] == 42
    assert seen["worker_thread"] != main_thread
    assert seen["cb_thread"] == main_thread


def test_error_routes_to_on_error(qt_app):
    seen = {}

    def bad():
        raise ValueError("boom")

    run_blocking(bad, lambda r: seen.update(result=r),
                 lambda e: seen.update(error=e))
    _pump_until(qt_app, lambda: "error" in seen)

    assert "result" not in seen
    assert isinstance(seen["error"], ValueError)
    assert str(seen["error"]) == "boom"


def test_missing_on_error_swallows_without_crash(qt_app, caplog):
    done = {}

    def bad():
        raise RuntimeError("ignored")

    run_blocking(bad, lambda r: done.update(result=r))
    # Nothing to wait on except "no crash": settle a marker task after
    # it to prove the pool and dispatcher are still healthy.
    run_blocking(lambda: "ok", lambda r: done.update(marker=r))
    _pump_until(qt_app, lambda: "marker" in done)
    assert "result" not in done
