# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The membership card at the top of the member page, as Wallet shows a pass.

    ╭──────────────────────────────────────────────────╮
    │ EINUNDZWANZIG                                     │
    │ Member 2026                                       │
    │ satoshi@einundzwanzig.space                       │
    │ ● Active · until December 31, 2026                │
    ╰──────────────────────────────────────────────────╯

A dark card with the association's orange in its name and in one
corner, the same in the light and the dark appearance, the way a pass in
Wallet keeps its colors; on a dark window a hairline sets it off. It says
whose card it is (the Nostr address, when the member chose one, and
never a key), for which year, and until when: membership runs for a
calendar year.

The words are labels, so a screen reader reads them as they stand. The
dot before the status is drawn, and says nothing the word "Active" does
not.
"""

from __future__ import annotations

import datetime
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

import i18n
from i18n import _
from . import appearance
from nostr.ui.assistant import text_label
from nostr.ui.grouped_list import spoken

# EINUNDZWANZIG's orange (Bitcoin's), and the card it is set on.
ORANGE = QColor(appearance.roles(True).orange)
ACTIVE = QColor("#32D74B")      # the dot before "Active"
RADIUS = 12
DOT = 8
DOT_SPACE = 6                   # between the dot and the status

_STYLE = f"""
QLabel {{ background: transparent; }}
QLabel#card_brand {{
    color: {ORANGE.name()}; font-size: 11px; font-weight: 700; letter-spacing: 1.6px;
}}
QLabel#card_title {{ color: #FFFFFF; font-size: 22px; font-weight: 600; }}
QLabel#card_address {{ color: rgba(255, 255, 255, 0.78); font-size: 13px; }}
QLabel#card_status {{ color: rgba(255, 255, 255, 0.88); font-size: 12px; }}
"""


class MembershipCard(QWidget):
    """The year, the address and until when (see the module)."""

    def __init__(self, *, is_dark: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(_("Membership card"))
        self.setStyleSheet(_STYLE)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 16, 20, 16)
        column.setSpacing(0)
        # The association's name as it writes it, in every language.
        self.brand = text_label("EINUNDZWANZIG", "card_brand", wrap=False)
        self.title = text_label("", "card_title", wrap=False)
        self.address = text_label("", "card_address", wrap=False)
        self.status = text_label("", "card_status")
        column.addWidget(self.brand)
        column.addSpacing(8)
        column.addWidget(self.title)
        column.addSpacing(2)
        column.addWidget(self.address)
        column.addSpacing(8)
        status = QHBoxLayout()
        status.setContentsMargins(DOT + DOT_SPACE, 0, 0, 0)
        status.addWidget(self.status, 1)
        column.addLayout(status)

    def set_member(self, year: int, address: str = "") -> None:
        """A member of ``year``, known by ``address`` ("" before they
        chose one): active until the year's last day."""
        self.title.setText(_("Member {year}").format(year=year))
        self.address.setText(address)
        self.address.setVisible(bool(address))
        last_day = i18n.format_day(datetime.date(year, 12, 31))
        self.status.setText(_("Active · until {date}").format(date=last_day))
        self.status.setAccessibleName(spoken(self.status.text()))

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(appearance.roles(self._is_dark).edge), 1)
                       if self._is_dark else Qt.PenStyle.NoPen)
        appearance.membership_background(painter, card, RADIUS)
        appearance.relay_icon(True, 22).paint(painter,
            int(card.right()) - 42, int(card.top()) + 16, 22, 22)
        # The dot before the status, at the middle of its capitals.
        metrics = self.status.fontMetrics()
        line = self.status.geometry()
        middle = line.top() + metrics.ascent() - metrics.capHeight() / 2
        painter.setBrush(ACTIVE)
        painter.drawEllipse(QPointF(line.left() - DOT_SPACE - DOT / 2, middle),
                            DOT / 2, DOT / 2)
