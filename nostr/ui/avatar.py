"""Shared avatar + chevron rendering helpers.

Used by both ``profile_chip`` (header) and ``publish_note_dialog``
(in-dialog "Publishing as …" switcher). Keeping these in one place
guarantees the chip looks identical wherever it's drawn.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)


AVATAR_SIZE: int = 26   # matches the existing 26x26 B/I/U header buttons
CHEVRON_SIZE: int = 10
CHIP_GAP: int = 4
CHIP_TOTAL_WIDTH: int = AVATAR_SIZE + CHIP_GAP + CHEVRON_SIZE


# --------------------------------------------------------------------------- #
# Identity-derived helpers                                                    #
# --------------------------------------------------------------------------- #

def initials_for(display_name: str) -> str:
    """First letter of first word + first letter of last word, uppercased.

    Single-word names return one letter. Empty input returns ``?``.
    """
    name = (display_name or "").strip()
    if not name:
        return "?"
    parts = name.split()
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def color_for_pubkey(pubkey_hex: str) -> QColor:
    """Deterministic mid-saturation hue from the first 6 hex chars."""
    if len(pubkey_hex) < 6:
        return QColor("#888888")
    hue = int(pubkey_hex[:6], 16) % 360
    return QColor.fromHsv(hue, 165, 165)


# --------------------------------------------------------------------------- #
# Pixmap builders                                                             #
# --------------------------------------------------------------------------- #

def make_initials_pixmap(initials: str, color: QColor, size: int = AVATAR_SIZE) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size, size)
    p.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(max(8, int(size * 0.42)))
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, initials)
    p.end()
    return pix


def make_avatar_pixmap_from_image(source: QPixmap, size: int = AVATAR_SIZE) -> QPixmap:
    """Crop ``source`` to a centered circle of ``size`` pixels."""
    scaled = source.scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    p.setClipPath(path)
    dx = (size - scaled.width()) // 2
    dy = (size - scaled.height()) // 2
    p.drawPixmap(dx, dy, scaled)
    p.end()
    return out


def make_disconnected_pixmap(is_dark: bool, size: int = AVATAR_SIZE) -> QPixmap:
    """Grey circle with a centered '+' for the not-connected state."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    bg = QColor("#2D2D30") if is_dark else QColor("#ECECEC")
    border = QColor("#3C3C3C") if is_dark else QColor("#CCCCCC")
    fg = QColor("#858585") if is_dark else QColor("#999999")
    p.setBrush(bg)
    p.setPen(QPen(border, 1))
    p.drawEllipse(0, 0, size - 1, size - 1)
    p.setPen(fg)
    font = QFont()
    font.setBold(True)
    font.setPointSize(max(10, int(size * 0.55)))
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, "+")
    p.end()
    return pix


def make_chevron_pixmap(color: QColor, size: int = CHEVRON_SIZE) -> QPixmap:
    """A small downward caret, painted with rounded line joins."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    top_y = size * 0.38
    bot_y = size * 0.66
    left_x = size * 0.22
    right_x = size * 0.78
    mid_x = size * 0.5
    p.drawLine(QPointF(left_x, top_y), QPointF(mid_x, bot_y))
    p.drawLine(QPointF(mid_x, bot_y), QPointF(right_x, top_y))
    p.end()
    return pix


def compose_chip_icon(avatar: QPixmap, chevron_color: QColor) -> QIcon:
    """Compose avatar + gap + chevron into one icon (platform-consistent)."""
    chevron = make_chevron_pixmap(chevron_color)
    out = QPixmap(CHIP_TOTAL_WIDTH, AVATAR_SIZE)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, avatar)
    chev_y = (AVATAR_SIZE - chevron.height()) // 2
    p.drawPixmap(AVATAR_SIZE + CHIP_GAP, chev_y, chevron)
    p.end()
    return QIcon(out)


def pixmap_for_profile(
    display_name: str,
    user_pubkey_hex: str,
    avatar_image: Optional[QPixmap] = None,
    size: int = AVATAR_SIZE,
) -> QPixmap:
    """One-shot 'give me an avatar' helper.

    Returns a circular pixmap built from ``avatar_image`` if available,
    otherwise initials painted on a deterministic color disc.
    """
    if avatar_image is not None and not avatar_image.isNull():
        return make_avatar_pixmap_from_image(avatar_image, size=size)
    return make_initials_pixmap(
        initials_for(display_name),
        color_for_pubkey(user_pubkey_hex),
        size=size,
    )
