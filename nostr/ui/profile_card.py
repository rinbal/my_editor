# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a profile looks to other people, at the top of Edit Profile.

    ┌──────────────────────────────────────────┐
    │                 banner                   │
    │  ╭────╮                                  │
    └──│ pic│──────────────────────────────────┘
       ╰────╯  Name
               you@example.com

The banner fills the top, cropped as Nostr apps crop it; the picture sits
in a circle over the banner's lower edge, as on a profile page; beside it
the name and the address people find the account by. Without a picture,
the circle holds the name's initials, as Contacts shows a card without a
photo; without a banner, a quiet tint. The window sets what to show as
the fields change: the card itself only draws, it loads nothing.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from constants import DARK_MUTED_FG, LIGHT_MUTED_FG
from i18n import _

BANNER_HEIGHT = 80
AVATAR = 64
RING = 3                 # the window's color around the picture
INSET = 16               # the picture's distance from the card's left edge
RADIUS = 10              # the banner's rounded corners


def initials(name: str) -> str:
    """The first letter of up to two words of ``name``, as Contacts shows
    for a card without a photo ("Ann Bell" gives "AB")."""
    words = [word for word in name.split() if word[:1].isalnum()]
    return "".join(word[0] for word in words[:2]).upper()


class ProfileCard(QWidget):
    """The banner, the picture, the name and the address (see the module)."""

    def __init__(self, *, is_dark: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self._picture: Optional[QPixmap] = None
        self._banner: Optional[QPixmap] = None
        self._name = ""
        self._detail = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(_("Your profile as others see it"))
        self._describe()
        self.set_dark(is_dark)

    # -- what is shown ---------------------------------------------------------

    def set_picture(self, pixmap: Optional[QPixmap]) -> None:
        self._picture = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

    def set_banner(self, pixmap: Optional[QPixmap]) -> None:
        self._banner = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

    def set_text(self, name: str, detail: str) -> None:
        """``name`` in bold, ``detail`` (the address people find the account
        by) muted under it."""
        self._name, self._detail = name.strip(), detail.strip()
        self._describe()
        self.update()

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        import theme
        self.setPalette(theme._palette(is_dark))
        self.update()

    def name(self) -> str:
        return self._name

    def detail(self) -> str:
        return self._detail

    def _describe(self) -> None:
        # What a screen reader says for the card: the profile as it reads.
        self.setAccessibleDescription(
            ", ".join(part for part in (self._name, self._detail) if part)
            or _("No name yet"))

    # -- drawing ---------------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802, Qt's name
        return QSize(420, BANNER_HEIGHT + AVATAR // 2 + 24)

    def minimumSizeHint(self) -> QSize:  # noqa: N802, Qt's name
        return QSize(260, self.sizeHint().height())

    def _colors(self):
        if self._is_dark:
            return QColor("#2A2C30"), QColor("#3A3D42"), QColor("#1E1E1E"), QColor(DARK_MUTED_FG)
        return QColor("#E9ECEF"), QColor("#D5DAE0"), QColor("#FFFFFF"), QColor(LIGHT_MUTED_FG)

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        tint, circle_fill, ring, muted = self._colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ring)
        painter.drawRoundedRect(QRectF(self.rect()), RADIUS, RADIUS)
        banner = QRectF(0, 0, self.width(), BANNER_HEIGHT)
        path = QPainterPath()
        path.addRoundedRect(banner, RADIUS, RADIUS)
        painter.save()
        painter.setClipPath(path)
        if self._banner is not None:
            _draw_cover(painter, self._banner, banner)
        else:
            painter.fillRect(banner, tint)
        painter.restore()

        avatar = QRectF(INSET, BANNER_HEIGHT - AVATAR / 2, AVATAR, AVATAR)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ring)
        painter.drawEllipse(avatar.adjusted(-RING, -RING, RING, RING))
        circle = QPainterPath()
        circle.addEllipse(avatar)
        painter.save()
        painter.setClipPath(circle)
        if self._picture is not None:
            _draw_cover(painter, self._picture, avatar)
        else:
            painter.fillRect(avatar, circle_fill)
            letters = initials(self._name)
            if letters:
                font = QFont(self.font())
                font.setPixelSize(int(AVATAR * 0.38))
                font.setWeight(QFont.Weight.DemiBold)
                painter.setFont(font)
                painter.setPen(muted)
                painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, letters)
        painter.restore()

        left = INSET + AVATAR + 12
        text_box = QRectF(left, BANNER_HEIGHT + 4, max(0, self.width() - left), 22)
        name_font = QFont(self.font())
        name_font.setPixelSize(15)
        name_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name_font)
        painter.setPen(self.palette().color(self.foregroundRole()))
        shown = self._name or _("Your name")
        if not self._name:
            painter.setPen(muted)
        painter.drawText(text_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(shown, Qt.TextElideMode.ElideRight,
                                                          int(text_box.width())))
        if self._detail:
            detail_font = QFont(self.font())
            detail_font.setPixelSize(12)
            painter.setFont(detail_font)
            painter.setPen(muted)
            detail_box = text_box.translated(0, 20)
            painter.drawText(
                detail_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(self._detail, Qt.TextElideMode.ElideMiddle,
                                                 int(detail_box.width())))
        painter.end()


def _draw_cover(painter: QPainter, pixmap: QPixmap, target: QRectF) -> None:
    """``pixmap`` filling ``target``, cropped around its middle, as a
    banner or a profile picture is shown in Nostr apps."""
    scaled = pixmap.size().scaled(target.size().toSize(),
                                  Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    if scaled.isEmpty():
        return
    sx = pixmap.width() / scaled.width()
    sy = pixmap.height() / scaled.height()
    source = QRectF((scaled.width() - target.width()) / 2 * sx,
                    (scaled.height() - target.height()) / 2 * sy,
                    target.width() * sx, target.height() * sy)
    painter.drawPixmap(target, pixmap, source)
