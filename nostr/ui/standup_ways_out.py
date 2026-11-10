# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Under the publication Submit to a Publication sends to: every answer
that leaves the sheet waiting has its way out (lesson 12). Try Again for
publications not loaded, a list not read or read in part, a round the
server could not tell; dimmed while a limit's wait is not over, and
back by itself once it is (contract 15, RG-M2r2a 5). An earlier try the
server has no receipt for (contract 13): Submit sends it again, Discard
takes it off the list. What each does is the sheet's."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from i18n import _

from .standup_waits import WaitTimer


class WaysOut(QWidget):
    """``retry_requested`` once a wait is over, ``discard_requested``,
    ``wait_over`` when a limit's wait has passed (the sheet says its
    state again). ``now`` is the sheet's clock."""

    retry_requested = Signal()
    discard_requested = Signal()
    wait_over = Signal()

    def __init__(self, hint: QLabel, *, now: Callable[[], float],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._now = now
        self._until: Optional[float] = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self.retry = QPushButton(_("Try Again"))
        self.retry.setAutoDefault(False)
        self.retry.clicked.connect(self._on_retry)
        self.retry.hide()
        column.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignLeft)
        self.unsent = QWidget()
        row = QHBoxLayout(self.unsent)
        row.setContentsMargins(0, 0, 0, 0)
        hint.setText(_("An earlier try wasn’t sent. Submit sends it again."))
        row.addWidget(hint, 1)
        self.discard = QPushButton(_("Discard"))
        self.discard.setAutoDefault(False)
        self.discard.clicked.connect(self.discard_requested)
        row.addWidget(self.discard, 0, Qt.AlignmentFlag.AlignTop)
        self.unsent.hide()
        column.addWidget(self.unsent)
        self.timer = WaitTimer(self._wait_passed, self)

    def show_ways(self, *, retry: bool, until: Optional[float], unsent: bool) -> None:
        """Try Again shown (dimmed until ``until``, a clock time), and the
        earlier try not sent."""
        self._until = until
        self.retry.setVisible(retry)
        self.retry.setEnabled(not self.timer.follow(until, now=self._now()))
        self.unsent.setVisible(unsent)

    def waiting(self) -> bool:
        """Whether a limit's wait for Try Again is not over yet."""
        return self._until is not None and self._until > self._now()

    def _on_retry(self) -> None:
        if not self.waiting():
            self.retry_requested.emit()

    def _wait_passed(self) -> None:
        self.wait_over.emit()


__all__ = ["WaysOut"]
