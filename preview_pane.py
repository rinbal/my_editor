# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The preview of a document, and the switch between writing and reading.

Write, Preview and Split (the View menu, and the switch at the end of the
format toolbar) show a Markdown-capable document's text, its preview, or
both side by side. The preview is drawn by Qt's own Markdown reader
through nostr/preview.py, the code the publish preview uses (decision
Q-E): it shows what the document's Markdown says, which is what a file,
a draft or a publication carries, not the editor's own drawing of it.

Without a Nostr account in use the preview shows the document as it
reads, and says when it leaves out colors, highlights and underlines,
which Markdown cannot carry. With one, it shows what will be published,
as an article (cover, title, summary, byline, the body and hashtags) or
as a note (plain text, the way every app shows notes), chosen with
Preview As; and it says what will not reach readers the way it looks in
the editor.

The pane draws only while it is shown, 300 ms after the text last
changed, and keeps its place in the text across redraws.

Split needs room for two readable columns (SPLIT_MIN_WIDTH). Narrower,
the tab shows the side used last, the switch flips between the two, and
it splits again when there is room (decision Q-N).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from constants import DARK_BORDER, DARK_MUTED_FG, LIGHT_BORDER, LIGHT_MUTED_FG
from i18n import _, pgettext
from segmented_control import ActionSegments
from nostr.preview import Article, ImageLookup, MediaNames, NameLookup, NostrPreview

# What the preview shows.
DOCUMENT = "document"      # the document as it reads (no Nostr account in use)
ARTICLE = "article"        # what an article will look like to its readers
NOTE = "note"              # what a note will look like

# The three ways a tab shows a document.
WRITE = "write"
PREVIEW = "preview"
SPLIT = "split"
MODES = (WRITE, PREVIEW, SPLIT)

# Two readable columns: below this width a tab shows one side (Q-N).
SPLIT_MIN_WIDTH = 1100
RENDER_DELAY_MS = 300


@dataclass(frozen=True)
class PreviewContent:
    """What to draw: the text in ``kind``'s way, and what to say about it."""

    kind: str
    text: str
    article: Article = field(default_factory=Article)
    author: str = ""
    names: Optional[NameLookup] = None
    checks: Tuple[str, ...] = ()


class PreviewPane(QWidget):
    """The preview beside or in place of one editor.

    ``content(kind)`` gives what to draw, ``kind`` being ARTICLE or NOTE as
    chosen in Preview As (the window answers DOCUMENT while no Nostr
    account is in use); ``images`` finds the pictures it names,
    ``media_names`` the names of the audio and video files, and ``play``
    plays one whose card is clicked."""

    def __init__(self, content: Callable[[str], PreviewContent], *,
                 images: Optional[ImageLookup] = None,
                 media_names: Optional[MediaNames] = None, dark: bool = False,
                 note: bool = False, play: Optional[Callable[[str], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewPane")
        self._content = content
        self._dark = dark

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QWidget()
        self._header.setObjectName("PreviewHeader")
        row = QHBoxLayout(self._header)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(8)
        label = QLabel(_("Preview As:"))
        self.kind_box = QComboBox()
        self.kind_box.addItem(_("Article"), ARTICLE)
        self.kind_box.addItem(_("Note"), NOTE)
        self.kind_box.setCurrentIndex(1 if note else 0)
        label.setBuddy(self.kind_box)
        # Its list, when it opens, is named too (Qt leaves it unnamed).
        self.kind_box.view().setAccessibleName(label.text().rstrip(":").strip())
        self.kind_box.currentIndexChanged.connect(lambda _i: self.render())
        self._caption = QLabel(_("Live preview of what will be published"))
        self._caption.setObjectName("PreviewCaption")
        row.addWidget(label)
        row.addWidget(self.kind_box)
        row.addSpacing(8)
        row.addWidget(self._caption, 1)
        layout.addWidget(self._header)

        self._checks = QLabel()
        self._checks.setObjectName("PreviewChecks")
        self._checks.setWordWrap(True)
        self._checks.setVisible(False)
        layout.addWidget(self._checks)

        self.view = NostrPreview(images=images, media_names=media_names, dark=dark, play=play)
        layout.addWidget(self.view, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_DELAY_MS)
        self._timer.timeout.connect(self.render)
        self._apply_colors()

    # -- what is shown -----------------------------------------------------------

    def kind(self) -> str:
        return self.kind_box.currentData()

    def schedule(self) -> None:
        """The text changed: draw again shortly, while shown."""
        if self.isVisible():
            self._timer.start()

    def render(self) -> None:
        """Draw the preview now, keeping the place in the text."""
        self._timer.stop()
        content = self._content(self.kind())
        bar = self.view.verticalScrollBar()
        ratio = bar.value() / bar.maximum() if bar.maximum() > 0 else 0.0
        self._header.setVisible(content.kind != DOCUMENT)
        if content.kind == NOTE:
            self.view.show_note(content.text, author=content.author, names=content.names)
        elif content.kind == ARTICLE:
            self.view.show_article(content.text, content.article, names=content.names)
        else:
            self.view.show_markdown(content.text, names=content.names)
        self._checks.setText("\n".join(content.checks))
        self._checks.setVisible(bool(content.checks))
        bar.setValue(round(ratio * bar.maximum()))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.render()

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.view.set_dark(dark)
        self._apply_colors()
        if self.isVisible():
            self.render()

    def _apply_colors(self) -> None:
        muted = DARK_MUTED_FG if self._dark else LIGHT_MUTED_FG
        border = DARK_BORDER if self._dark else LIGHT_BORDER
        self.setStyleSheet(
            f"#PreviewHeader {{ border-bottom: 1px solid {border}; }}"
            f"#PreviewCaption {{ color: {muted}; }}"
            f"#PreviewChecks {{ color: {muted};"
            f" padding: 8px 16px; border-bottom: 1px solid {border}; }}")


class ViewSwitch(ActionSegments):
    """Write, Preview and Split as one segmented control, made of the
    window's own View commands (the same actions as the menu, which keep
    one of them checked; the segments only show them). It is the shared
    control (segmented_control.py), so it reads as the panel's Drafts ·
    Published beside it does: a neutral thumb, not an accent fill."""

    def __init__(self, actions: Sequence[QAction], *, dark: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(actions, dark=dark, parent=parent)
        self.setObjectName("ViewSwitch")
        self.setAccessibleName(pgettext("view modes", "View"))
        self.buttons = self.segments
        # The segments' places, for anything that styles the ends.
        places = ["first"] + ["middle"] * (len(self.buttons) - 2) + ["last"]
        for button, place in zip(self.buttons, places):
            button.setObjectName(place)
