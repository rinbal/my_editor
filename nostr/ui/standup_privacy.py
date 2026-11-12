# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What Submit to a Publication says about who can read a submission
(plan 10.2, MyEditor's wording): one line with a lock, and behind its
Details the lines of :func:`privacy_words`. Part of
:mod:`.standup_submit_sheet` before.
"""

from __future__ import annotations

from typing import Callable, List, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout

from i18n import _, ngettext

from . import appearance
from .post_list import symbol_pixmap


def privacy_words(publication: str, pictures: int, public: int, operator: str) -> List[str]:
    """The lines behind the lock: ``publication`` the chosen one's name
    (empty: none chosen yet), ``pictures`` the pictures that go
    encrypted, ``public`` those that are public links already, and
    ``operator`` who runs the server (empty: not known)."""
    name = publication or _("the publication")
    lines = [_("Only you and the editorial team of {publication} can read it.").format(
                 publication=name),
             _("The server sees the title, the publication and who takes part, never "
               "the content.")]
    if pictures and not public:
        lines.append(ngettext(
            "The picture is encrypted on your media server. It sees when it is fetched, "
            "never what is in it.",
            "The pictures are encrypted on your media server. It sees when they are "
            "fetched, never what is in them.", pictures))
    elif pictures:
        # Some stay public: the encrypted ones are counted, never all of
        # them called encrypted (contract 7, feat/redaktion ee3e5d7).
        lines.append(ngettext(
            "{count} picture is encrypted on your media server. It sees when it is "
            "fetched, never what is in it.",
            "{count} pictures are encrypted on your media server. It sees when they are "
            "fetched, never what is in them.", pictures).format(count=pictures))
    if public:
        lines.append(ngettext(
            "{count} picture was already public on the web and stays public.",
            "{count} pictures were already public on the web and stay public.",
            public).format(count=public))
    lines.append(_("Withdrawing deletes nothing the editorial team already has."))
    lines.append(_("The server of {operator} keeps the submission encrypted.").format(
        operator=operator) if operator
        else _("The server keeps the submission encrypted."))
    return lines


class PrivacyBox(QFrame):
    """The lock line and, behind :attr:`toggle` (Details), the
    :attr:`lines` that ``words()`` gives, read anew each time they show
    (:meth:`refresh` after what they say changed)."""

    def __init__(self, words: Callable[[], Sequence[str]], *, is_dark: bool) -> None:
        super().__init__()
        self._words = words
        self._is_dark = is_dark
        self.setObjectName("privacy")
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 8, 12, 10)
        column.setSpacing(6)
        line = QHBoxLayout()
        self._lock = QLabel()
        line.addWidget(self._lock)
        said = QLabel(_("Content end-to-end encrypted. The server sees the title."))
        said.setWordWrap(True)
        line.addWidget(said, 1)
        self.toggle = QToolButton()
        self.toggle.setObjectName("disclosure")
        self.toggle.setCheckable(True)
        self.toggle.setText(_("Details"))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setAccessibleName(_("Privacy details"))
        self.toggle.toggled.connect(self.refresh)
        line.addWidget(self.toggle)
        column.addLayout(line)
        self.lines = QLabel()
        self.lines.setObjectName("muted")
        self.lines.setWordWrap(True)
        self.lines.setTextFormat(Qt.TextFormat.PlainText)
        self.lines.hide()
        column.addWidget(self.lines)
        self.set_dark(is_dark)

    def refresh(self, *_args) -> None:
        shown = self.toggle.isChecked()
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self.lines.setText("\n".join(f"• {line}" for line in self._words()))
        self.lines.setVisible(shown)

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        r = appearance.roles(is_dark)
        self.setStyleSheet(
            f"QFrame#privacy {{ border: 1px solid {r.line}; border-radius: 10px;"
            f" background: {r.field}; }}")
        self._lock.setPixmap(symbol_pixmap("lock", 15, QColor(r.muted)))


__all__ = ["PrivacyBox", "privacy_words"]
