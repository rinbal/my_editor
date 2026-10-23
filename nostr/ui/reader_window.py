# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A preview kept open in a window of its own: the detached popover.

    ┌ Why I write in plain text ───────────── ● ● ● ┐
    │  [ cover ]                                    │
    │  Why I write in plain text                    │  the preview's header
    │  Saved October 14, 2026 at 14:05              │
    │  ───────────────────────────────────────────  │
    │  Every tool I loved is gone. My files are     │  the text, styled as
    │  still here, and so is every word …           │  Nostr shows it
    │  ───────────────────────────────────────────  │
    │  ⋯                   Publish…  Continue Editing│  the same commands
    └───────────────────────────────────────────────┘

Apple's guidelines for popovers on macOS: "Consider letting people
detach a popover. People might appreciate being able to convert a
popover into a panel if they want to view other information while the
popover remains visible", and "Make minimal appearance changes to a
detached popover". Dragging the full preview by its header, or More >
Open in New Window, puts it here: the same header, text and commands,
in a panel that stays while the person writes beside it, and that hides
with the app, as a Mac panel does (a tool window).

The window holds a copy of what the preview showed when it was
detached; it is a reading surface, not a live view. Its commands go to
the same host as the preview's (``action_chosen(key)``); the ones that
move on from the post (Continue Editing, Edit Article, Publish…, Delete)
close it, the ones that keep it in view (Copy Link, Open in) do not.
Escape and the system's Close key close it.
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import appearance
from i18n import _

from .drafts_common import THEME_TOKENS, scaled, secondary_font
from .preview_parts import (
    IMAGE_UNAVAILABLE, IMAGE_WAITING, PAD, ActionBar, ClampedLabel, HeroImage, PreviewAction, PreviewFields, PreviewReader,
    body_font, preview_css, title_font,
)

# The header's line clamps, as the full preview has them.
_TITLE_LINES, _SUMMARY_LINES, _ORIGIN_LINES, _TAG_LINES = 3, 5, 2, 2

# The commands after which the post is no longer what the person reads.
_KEEPS_WINDOW = ("copy", "copy_id", "open_in", "open_with")


class ReaderWindow(QWidget):
    """One post, detached from its list (see the module)."""

    action_chosen = Signal(str)

    def __init__(self, *, fields: PreviewFields, kind: int, content: str,
                 actions: Tuple[PreviewAction, ...], size: QSize, is_dark: bool = True,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setObjectName("reader_window")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(fields.title)
        self.setAccessibleName(_("Preview of {title}").format(title=fields.title))
        self._is_dark = bool(is_dark)
        self.fields = fields
        self._image_url = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.hero = HeroImage(self, is_dark=self._is_dark)
        self.hero.setVisible(bool(fields.image_url))
        if fields.image_url:
            self.hero.reset(width=size.width())
        layout.addWidget(self.hero)

        header = QWidget(self)
        column = QVBoxLayout(header)
        pad = scaled(PAD)
        column.setContentsMargins(pad, pad, pad, pad)
        column.setSpacing(scaled(4))
        inner = max(1, size.width() - 2 * pad)
        for name, text, font, lines in (
                ("drafts_preview_title", fields.title, title_font(), _TITLE_LINES),
                ("drafts_preview_summary", fields.summary, body_font(), _SUMMARY_LINES),
                ("drafts_preview_origin", fields.origin, secondary_font(), _ORIGIN_LINES),
                ("drafts_preview_stats", fields.stats, secondary_font(), 1),
                ("drafts_preview_tags", fields.tags, secondary_font(), _TAG_LINES)):
            label = ClampedLabel(name, header)
            label.setFont(font)
            label.apply(text, width=inner, max_lines=lines)
            column.addWidget(label)
        layout.addWidget(header)

        self.reader = PreviewReader(dark=self._is_dark, parent=self)
        if kind == 30023:
            self.reader.show_markdown(content)
        else:
            self.reader.show_note(content)
        layout.addWidget(self.reader, 1)

        self.bar = ActionBar(self, is_dark=self._is_dark)
        self.bar.set_actions(tuple(action for action in actions
                                   if action.key != "detach"))
        self.bar.chosen.connect(self._on_chosen)
        layout.addWidget(self.bar)

        for keys in (QKeySequence(Qt.Key.Key_Escape), QKeySequence(QKeySequence.StandardKey.Close)):
            QShortcut(keys, self, activated=self.close)
        self.apply_theme(self._is_dark)
        self.resize(size)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        appearance.surface(self, is_dark)
        appearance.bind(self)
        tokens = THEME_TOKENS[self._is_dark]
        self.setStyleSheet(preview_css(self._is_dark)
                           + f"QWidget#reader_window {{ background: {tokens['chrome_bg']}; }}")
        self.reader.set_dark(self._is_dark)
        self.bar.apply_theme(self._is_dark)
        self.hero.apply_theme(self._is_dark)

    def set_image(self, pixmap: QPixmap) -> None:
        """The cover's picture. Hidden, not merely not shown yet: the window
        is given it before it first shows."""
        if not self.hero.isHidden():
            self.hero.set_pixmap(pixmap)

    def set_image_note(self, text: str) -> None:
        if not self.hero.isHidden():
            self.hero.set_note(text)

    def load_image(self, loader, url: str) -> None:
        """Ask ``loader`` (thumbnail_loader.ThumbnailLoader) for the cover at
        ``url``, which the preview had not got yet, and show it when it
        comes, or say it can't."""
        self._image_url = url
        loader.url_ready.connect(self._on_image_ready)
        loader.url_failed.connect(self._on_image_failed)
        self.set_image_note(IMAGE_WAITING)
        loader.load_url(url)

    def _on_image_ready(self, url: str, _sha: str, pixmap: QPixmap) -> None:
        if url == self._image_url:
            self.set_image(pixmap)

    def _on_image_failed(self, url: str, _reason: str) -> None:
        if url == self._image_url:
            self.set_image_note(IMAGE_UNAVAILABLE)

    def _on_chosen(self, key: str) -> None:
        self.action_chosen.emit(key)
        if not (key in _KEEPS_WINDOW or key.startswith("open:")):
            self.close()
