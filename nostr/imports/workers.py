# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a blocking function off the GUI thread, deliver the result on it.

The importer's CPU-heavy steps (feed parsing, HTML-to-Markdown
conversion) must not stall the UI; a 16 MiB feed can take visible time.
:func:`run_blocking` executes the function on the global ``QThreadPool``
and invokes exactly one of ``on_done`` / ``on_error`` back on the thread
that made the call (via a queued signal to a holder object created
there).

Lifetime: the holder is parented to ``parent`` when given. If the parent
is deleted before the task finishes, the queued delivery is dropped with
it, so callbacks never fire into dead objects.

Callers that need determinism (tests) skip this module entirely by
injecting an inline executor with the same ``(fn, on_done, on_error)``
shape.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_log = logging.getLogger(__name__)

# Holders currently awaiting delivery. The QRunnable auto-deletes after
# ``run()`` and with it the only other Python reference to the holder;
# without this registry the holder (and its pending queued event) could
# be garbage-collected before the caller's thread processes it.
_ACTIVE_HOLDERS: set = set()


class _CallbackHolder(QObject):
    """Receives worker-thread results via queued signals and forwards
    them to plain callables on its own (the caller's) thread."""

    done = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        on_done: Callable[[Any], None],
        on_error: Optional[Callable[[BaseException], None]],
        parent: Optional[QObject],
    ) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self._on_error = on_error
        # Bound-method receivers give the connection this object's
        # thread affinity, so emission from the pool thread is queued.
        self.done.connect(self._deliver_done)
        self.failed.connect(self._deliver_failed)
        _ACTIVE_HOLDERS.add(self)
        # If a parent dies before delivery, Qt destroys the C++ holder
        # and drops the queued event; release the Python side too so the
        # registry cannot leak.
        self.destroyed.connect(lambda *_: _ACTIVE_HOLDERS.discard(self))

    def _deliver_done(self, result: object) -> None:
        try:
            self._on_done(result)
        finally:
            _ACTIVE_HOLDERS.discard(self)
            self.deleteLater()

    def _deliver_failed(self, exc: object) -> None:
        try:
            if self._on_error is not None:
                self._on_error(exc)
            else:
                _log.warning("run_blocking task failed: %s", exc)
        finally:
            _ACTIVE_HOLDERS.discard(self)
            self.deleteLater()


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], holder: _CallbackHolder) -> None:
        super().__init__()
        self._fn = fn
        self._holder = holder

    def run(self) -> None:  # executes on a pool thread
        try:
            result = self._fn()
        except BaseException as exc:  # noqa: BLE001, must never kill the pool thread
            self._holder.failed.emit(exc)
            return
        self._holder.done.emit(result)


def run_blocking(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Optional[Callable[[BaseException], None]] = None,
    *,
    parent: Optional[QObject] = None,
    pool: Optional[QThreadPool] = None,
) -> None:
    """Execute ``fn()`` on a worker thread; call back on this thread.

    Exactly one of ``on_done(result)`` / ``on_error(exception)`` fires,
    unless ``parent`` is deleted first, in which case neither does.
    """
    holder = _CallbackHolder(on_done, on_error, parent)
    (pool or QThreadPool.globalInstance()).start(_Task(fn, holder))
