# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The publications Submit to a Publication offers (plan 10.2): each
one's logo, its rules as the picker says them under it, whether it can
be chosen, who publishes an accepted article there, and the order they
are listed in. They were part of :mod:`.standup_submit_sheet`, which
still offers them.

A logo comes only from the server that lists the publication, as
``GET /api/avatars/by/<pk>/<id>?v=<tag>`` with the tag its read gave
(contract 12.9), keyed by the publication's address
(:class:`PublicationLogos`): never from the profile's ``picture``,
whose host would learn who looks when, and never into the people's
avatars, where a publication's key would stand for a person (F5 M5).
Without one it is the monogram, as in the web app.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QStandardItemModel
from PySide6.QtWidgets import QComboBox

from i18n import _, ngettext

from ..standup import formats
from ..standup.situations import and_list
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


# 12.9: a logo the server is still fetching answers 503 with Retry-After 5;
# the monogram stands meanwhile and the address is asked once more.
RETRY_MS = 5000


class PublicationLogos(QObject):
    """Publication logos by address. ``fetch(key, url, on_done(pixmap or
    None))`` is the picture loader's (:meth:`AvatarLoader.fetch`, with a
    key of their own, so nothing lands among the people's avatars).
    ``changed(address)`` once one arrived."""

    changed = Signal(str)

    def __init__(self, fetch: Callable, *,
                 later: Callable[[int, Callable[[], None]], None] = QTimer.singleShot,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._fetch, self._later = fetch, later
        self._held: Dict[str, QPixmap] = {}
        self._asked: Set[Tuple[str, str]] = set()

    def get(self, address: str) -> Optional[QPixmap]:
        return self._held.get(address)

    def request(self, server: str, address: str, tag: Optional[str]) -> None:
        """The logo of ``address`` from ``server`` with the ``tag`` a read
        gave (none: the monogram stays); once per address and tag."""
        try:
            url = server.rstrip("/") + formats.avatar_path(address, tag or "")
        except (formats.FormatError, AttributeError):
            return
        if not url.startswith(("https://", "http://")) or (address, url) in self._asked:
            return
        self._asked.add((address, url))
        self._ask(address, url, again=True)

    def _ask(self, address: str, url: str, *, again: bool) -> None:
        def done(pixmap: Optional[QPixmap]) -> None:
            if pixmap is not None and not pixmap.isNull():
                self._held[address] = pixmap
                self.changed.emit(address)
            elif again:
                self._later(RETRY_MS, lambda: self._ask(address, url, again=False))

        key = "publication-" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:32]
        self._fetch(key, url, done)


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
        parts.append(_("Asks for {items}").format(items=and_list(asked)))
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


def fill_picker(picker: QComboBox, destinations: Sequence[Destination], *, loading: bool,
                logo_for: Callable[[Destination], QPixmap],
                request_logo: Callable[[Destination], None]) -> None:
    """The publications in Submit to a Publication's picker, each with its
    logo (asked for when its listing names one), one that cannot be chosen
    dimmed with why, and the first that can chosen; while loading, or
    with none, one dimmed line (the reason is said once, by the sheet)."""
    picker.blockSignals(True)
    picker.clear()
    model: QStandardItemModel = picker.model()
    if loading or not destinations:
        picker.addItem(_("Loading publications …") if loading else _("No publication"))
        picker.setEnabled(False)
    else:
        for destination in destinations:
            if destination.avatar:
                request_logo(destination)
            label = destination.name
            if not can_choose(destination):
                label = f"{destination.name} · {rules_words(destination.key.rules)}"
            picker.addItem(logo_for(destination), label)
            model.item(picker.count() - 1).setEnabled(can_choose(destination))
        picker.insertSeparator(picker.count())
        picker.addItem(_("Recently used first"))
        model.item(picker.count() - 1).setEnabled(False)
        picker.setEnabled(True)
        picker.setCurrentIndex(next((i for i, d in enumerate(destinations) if can_choose(d)),
                                    -1))
    picker.blockSignals(False)


def recent_first(destinations: Sequence[Destination], used: Sequence[str]) -> List[Destination]:
    """The publications submitted to before first (``used``, newest
    first), then the others in the server's order."""
    rank = {address: i for i, address in enumerate(used)}
    return sorted(destinations, key=lambda d: rank.get(d.address, len(rank)))


__all__ = ["PublicationLogos", "RETRY_MS", "can_choose", "fill_picker", "publication_logo",
           "publish_by", "recent_first", "rules_words", "takes_articles"]
