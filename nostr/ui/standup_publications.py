# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The publications Submit to a Publication offers (plan 10.2): each
one's logo, its rules as the picker says them under it, whether it can
be chosen, who publishes an accepted article there, and the order they
are listed in. They were part of :mod:`.standup_submit_sheet`, which
still offers them.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap

from i18n import _, ngettext

from ..standup import formats
from ..standup.trust import Destination
from .avatar import color_for_pubkey, initials_for


def publication_logo(name: str, pubkey: str, picture: Optional[QPixmap], size: int) -> QPixmap:
    """A publication's logo as a rounded square: its picture, cropped to
    a square, or a monogram of its name (as Contacts draws one)."""
    out = QPixmap(size * 2, size * 2)
    out.setDevicePixelRatio(2.0)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)
    painter.setClipPath(path)
    if picture is not None and not picture.isNull():
        side = min(picture.width(), picture.height())
        square = picture.copy((picture.width() - side) // 2, (picture.height() - side) // 2,
                              side, side)
        painter.drawPixmap(0, 0, size, size, square)
    else:
        painter.fillPath(path, color_for_pubkey(pubkey))
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(max(8, int(size * 0.45)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(0, 0, size, size, Qt.AlignmentFlag.AlignCenter, initials_for(name))
    painter.end()
    return out


def _and(items: Sequence[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return _("{items} and {last}").format(items=", ".join(items[:-1]), last=items[-1])


def rules_words(rules) -> str:
    """The rules of a publication, as the picker says them under it."""
    if rules and not rules.get("open", False):
        return _("Not taking submissions right now")
    if "article" not in (rules.get("kinds") or ["article"]):
        return _("Takes audio only") if rules.get("kinds") == ["audio"] \
            else _("Takes no articles")
    parts = []
    asked = [str(item.get("label")) for item in rules.get("checklist") or ()
             if isinstance(item, dict) and item.get("level") == "required" and item.get("label")]
    if asked:
        parts.append(_("Asks for {items}").format(items=_and(asked)))
    most = rules.get("max_open_per_writer")
    if formats.is_int(most, low=1):
        parts.append(ngettext("at most {count} open submission",
                              "at most {count} open submissions", most).format(count=most))
    return " · ".join(parts)


def takes_articles(destination: Destination) -> bool:
    rules = destination.key.rules if destination.key is not None else {}
    return "article" in (rules.get("kinds") or ["article"])


def can_choose(destination: Destination) -> bool:
    """Whether the picker offers it: open, for articles. One whose key
    could not be confirmed is offered and says why it cannot be used."""
    return destination.key is None or (destination.usable and takes_articles(destination))


def publish_by(destination: Destination) -> str:
    """Who publishes an accepted article: "writer", "publication" or
    "both" (the writer chooses)."""
    rules = destination.key.rules if destination.key is not None else {}
    by = rules.get("publish_by")
    value = by.get("article") if isinstance(by, dict) else by
    return value if value in ("writer", "publication", "both") else "writer"


def recent_first(destinations: Sequence[Destination], used: Sequence[str]) -> List[Destination]:
    """The publications submitted to before first (``used``, newest
    first), then the others in the server's order."""
    rank = {address: i for i, address in enumerate(used)}
    return sorted(destinations, key=lambda d: rank.get(d.address, len(rank)))


__all__ = ["can_choose", "publication_logo", "publish_by", "recent_first", "rules_words",
           "takes_articles"]
