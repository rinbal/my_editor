# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An answer known at once, given on the next turn of the event loop.

A function that answers through callbacks answers on a later turn, from
the network or a signer app. When it knows its answer before anything is
sent (a refusal, nothing to do), :func:`answer_later` gives it on the
next turn all the same, so a caller never hears an answer while it is
still setting up (its state half made, a second callback not connected
yet). Every such early answer of the app goes through here.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QTimer

__all__ = ["answer_later"]


def answer_later(callback: Callable[..., None], *args: Any) -> None:
    """``callback(*args)`` on the next turn of the event loop. Bound to no
    object on purpose: whoever answers decides then whether the answer
    still matters (a cancelled call is no longer live)."""
    QTimer.singleShot(0, lambda: callback(*args))
