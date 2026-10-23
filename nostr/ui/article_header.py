# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An article's head on the page: its cover, title and summary, written
where the article starts.

    ┌──────────────────────────────────────────┐
    │ [ cover, 16:9 as the article card ]      │  Change Cover…  Remove (hover)
    │                                          │
    │ Plain text, forever                      │  the title (placeholder: the
    │ Why the files outlive the apps.          │  one it publishes with)
    │ ──────────────────────────────────────── │  the summary
    │ Every tool I loved is gone …             │  the text (the editor's)
    └──────────────────────────────────────────┘

The same details Article Details shows (nostr/article_details.py, kept
with the tab by the window), edited in place: what is typed goes to the
window as ``edited(title, summary)`` and comes back with every other
change (the inspector, the publish window), so the two never disagree.
It is shown by the window in the editor's header slot
(editor_header.EditorHeader), above the text and not in it, while a
Nostr account is in use and the tab is an article.

Return in the title goes to the summary, Return in the summary to the
text; Up on the text's first line comes back here (EditorHeader). The
cover is chosen the way Article Details chooses it (``cover_requested``
opens its picker) and shows the media library's copy of a library
picture; an address typed is never downloaded to show it
(cover_field.library_picture_hash).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from constants import DARK_BORDER, DARK_FG, DARK_MUTED_FG, LIGHT_BORDER, LIGHT_FG, LIGHT_MUTED_FG
from editor_header import HeaderWidget
from i18n import _
from . import appearance

from ..blossom.store import MediaStore
from .cover_field import library_picture_hash
from .thumbnail_loader import ThumbnailLoader

# The cover in the article card's 16:9 framing (cover_field.THUMB), as
# readers see it.
COVER_RATIO = (16, 9)
COVER_RADIUS = 8
# The title in the writing font at this scale, the summary at this one.
TITLE_SCALE = 1.75
SUMMARY_SCALE = 1.1
SUMMARY_MAX_LINES = 5


def _scaled(font: QFont, scale: float) -> QFont:
    """``font`` at ``scale`` times its size, in the unit it is set in (the
    writing font is set in pixels)."""
    scaled = QFont(font)
    if font.pixelSize() > 0:
        scaled.setPixelSize(round(font.pixelSize() * scale))
    else:
        scaled.setPointSizeF(font.pointSizeF() * scale)
    return scaled


class _CoverBanner(QWidget):
    """The cover across the column in 16:9, centre-cropped and rounded;
    words in its place while its picture can't be shown."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Its height follows its width, which a layout asks only when the
        # size policy says so.
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._picture = QPixmap()
        self._words = ""
        self._dark = False

    def hasHeightForWidth(self) -> bool:  # noqa: N802, Qt's name
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802, Qt's name
        across, down = COVER_RATIO
        return width * down // across

    def sizeHint(self) -> QSize:  # noqa: N802, Qt's name
        return QSize(480, self.heightForWidth(480))

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        self._place_corner()

    def set_corner(self, widget: QWidget) -> None:
        """``widget`` sits in the bottom right corner, over the picture. Placed
        by hand: a layout of its own would answer for the banner's height
        (Qt asks a widget's layout, not the widget, for it)."""
        widget.setParent(self)
        self._corner = widget
        self._place_corner()

    def _place_corner(self) -> None:
        corner = getattr(self, "_corner", None)
        if corner is not None:
            size = corner.sizeHint()
            corner.setGeometry(self.width() - size.width() - 10,
                               self.height() - size.height() - 10, size.width(), size.height())

    def set_picture(self, picture: QPixmap, words: str = "") -> None:
        self._picture, self._words = picture, words
        self.update()

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()), COVER_RADIUS, COVER_RADIUS)
        painter.setClipPath(shape)
        if self._picture.isNull():
            painter.fillRect(self.rect(), QColor(127, 127, 127, 28))
            painter.setPen(QColor(DARK_MUTED_FG if self._dark else LIGHT_MUTED_FG))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._words)
            return
        scaled = self._picture.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((self.width() - scaled.width()) // 2,
                           (self.height() - scaled.height()) // 2, scaled)


class _Summary(QPlainTextEdit):
    """The summary: as many lines as it has, up to SUMMARY_MAX_LINES; Return
    leaves it rather than starting a line (a summary is one paragraph)."""

    leave_down = Signal()
    leave_up = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTabChangesFocus(True)
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self.textChanged.connect(self.updateGeometry)

    def sizeHint(self) -> QSize:  # noqa: N802, Qt's name
        lines = max(1, min(SUMMARY_MAX_LINES, self._lines()))
        return QSize(super().sizeHint().width(),
                     lines * self.fontMetrics().lineSpacing() + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802, Qt's name
        return self.sizeHint()

    def _lines(self) -> int:
        # A plain text layout measures its document in lines, wrapped ones
        # counted.
        return int(self.document().documentLayout().documentSize().height())

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        self.updateGeometry()

    def keyPressEvent(self, event) -> None:  # noqa: N802, Qt's name
        cursor = self.textCursor()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.leave_down.emit()
            return
        if event.key() == Qt.Key.Key_Down and cursor.atEnd():
            self.leave_down.emit()
            return
        if event.key() == Qt.Key.Key_Up and cursor.block().layout().lineForTextPosition(
                cursor.positionInBlock()).lineNumber() == 0:
            self.leave_up.emit()
            return
        super().keyPressEvent(event)


class ArticleHeader(HeaderWidget):
    """An article's cover, title and summary on the page (see the module)."""

    edited = Signal(str, str)          # title, summary, as typed
    cover_requested = Signal()         # Add Cover / Change Cover…
    cover_removed = Signal()

    def __init__(self, *, media_store: Optional[MediaStore] = None, is_dark: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("article_header")
        self.setAccessibleName(_("Article"))
        self._media_store = media_store
        self._dark = is_dark
        self._filling = False
        # The cover shown; None until the first is, so that one is applied.
        self._cover: Optional[str] = None
        self._cover_hash = ""
        self._loader: Optional[ThumbnailLoader] = (
            ThumbnailLoader(parent=self) if media_store is not None else None)
        if self._loader is not None:
            self._loader.ready.connect(self._on_picture)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 4, 0, 18)
        column.setSpacing(6)

        # "Add Cover" keeps its room and shows on hover or focus, so the
        # page doesn't jump as the pointer passes.
        self.add_cover = QToolButton(self)
        self.add_cover.setObjectName("article_header_add_cover")
        self.add_cover.setText(_("Add Cover"))
        self.add_cover.setAutoRaise(True)
        self.add_cover.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.add_cover.clicked.connect(self.cover_requested)
        self._add_cover_look = QGraphicsOpacityEffect(self.add_cover)
        self.add_cover.setGraphicsEffect(self._add_cover_look)
        column.addWidget(self.add_cover, 0, Qt.AlignmentFlag.AlignLeft)

        self.banner = _CoverBanner(self)
        self.banner.setAccessibleName(_("Cover"))
        self._cover_actions = QWidget(self.banner)
        self.change_cover = QPushButton(_("Change Cover…"), self._cover_actions)
        self.remove_cover = QPushButton(_("Remove"), self._cover_actions)
        self.remove_cover.setAccessibleName(_("Remove Cover"))
        actions = QHBoxLayout(self._cover_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        for button in (self.change_cover, self.remove_cover):
            button.setObjectName("article_header_cover_action")
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            actions.addWidget(button)
        self.banner.set_corner(self._cover_actions)
        self._cover_actions_look = QGraphicsOpacityEffect(self._cover_actions)
        self._cover_actions.setGraphicsEffect(self._cover_actions_look)
        self.change_cover.clicked.connect(self.cover_requested)
        self.remove_cover.clicked.connect(self.cover_removed)
        column.addWidget(self.banner)
        column.addSpacing(8)

        self.title_edit = QLineEdit(self)
        self.title_edit.setObjectName("article_header_title")
        self.title_edit.setFrame(False)
        # A line edit keeps 2 px before its text; the title starts on the
        # column's edge, with the summary and the text under it.
        self.title_edit.setTextMargins(-2, 0, 0, 0)
        self.title_edit.setAccessibleName(_("Title"))
        self.title_edit.textChanged.connect(self._typed)
        self.title_edit.returnPressed.connect(self._to_summary)
        column.addWidget(self.title_edit)

        self.summary_edit = _Summary(self)
        self.summary_edit.setObjectName("article_header_summary")
        self.summary_edit.setPlaceholderText(_("Add a summary"))
        self.summary_edit.setAccessibleName(_("Summary"))
        self.summary_edit.textChanged.connect(self._typed)
        self.summary_edit.leave_down.connect(self.leave_down)
        self.summary_edit.leave_up.connect(self.title_edit.setFocus)
        column.addWidget(self.summary_edit)

        self._rule = QFrame(self)
        self._rule.setObjectName("article_header_rule")
        self._rule.setFixedHeight(1)
        column.addSpacing(10)
        column.addWidget(self._rule)

        for watched in (self, self.add_cover, self.banner, self.change_cover,
                        self.remove_cover, self.title_edit, self.summary_edit):
            watched.installEventFilter(self)
        self.setTabOrder(self.add_cover, self.change_cover)
        self.setTabOrder(self.change_cover, self.remove_cover)
        self.setTabOrder(self.remove_cover, self.title_edit)
        self.setTabOrder(self.title_edit, self.summary_edit)
        self._show_cover("")
        self.set_dark(is_dark)

    # -- what it shows ----------------------------------------------------------

    def show_article(self, title: str, summary: str, cover: str) -> None:
        """The article's details as they are now, without handing them back;
        a field being typed in keeps what it holds."""
        self._filling = True
        try:
            if not self.title_edit.hasFocus() and self.title_edit.text() != title:
                self.title_edit.setText(title)
            if not self.summary_edit.hasFocus() and self.summary_edit.toPlainText() != summary:
                self.summary_edit.setPlainText(summary)
        finally:
            self._filling = False
        self._show_cover(cover)

    def set_derived_title(self, title: str) -> None:
        """The title the article publishes with while none is typed (its
        first heading or line), as the title's placeholder."""
        self.title_edit.setPlaceholderText(title or _("Title"))

    def set_text_font(self, font: QFont) -> None:
        """Set the title and summary in the writing font, at their scales."""
        title = _scaled(font, TITLE_SCALE)
        title.setWeight(QFont.Weight.DemiBold)
        self.title_edit.setFont(title)
        self.summary_edit.setFont(_scaled(font, SUMMARY_SCALE))
        self.summary_edit.updateGeometry()

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self.banner.set_dark(dark)
        ink, muted, line = ((DARK_FG, DARK_MUTED_FG, DARK_BORDER) if dark
                            else (LIGHT_FG, LIGHT_MUTED_FG, LIGHT_BORDER))
        self.setStyleSheet(
            f"QLineEdit#article_header_title {{ background: transparent; color: {ink};"
            f" border: none; padding: 0; }}"
            f"QPlainTextEdit#article_header_summary {{ background: transparent; color: {muted};"
            f" border: none; padding: 0; }}"
            f"QFrame#article_header_rule {{ background: {line}; border: none; }}"
            f"QToolButton#article_header_add_cover {{ color: {appearance.roles(dark).ink}; border: none;"
            f" padding: 2px 0; }}"
            "QPushButton#article_header_cover_action { color: white; background: rgba(0,0,0,150);"
            " border: none; border-radius: 6px; padding: 3px 10px; }")

    # -- the cover ----------------------------------------------------------------

    def _show_cover(self, cover: str) -> None:
        cover = cover.strip()
        if cover == self._cover:
            return
        self._cover = cover
        self.banner.setVisible(bool(cover))
        self.add_cover.setVisible(not cover)
        sha = library_picture_hash(self._media_store, cover) if self._loader else ""
        self._cover_hash = sha
        if sha:
            self.banner.set_picture(QPixmap())
            self._loader.load(sha, cover)
        elif cover:
            self.banner.set_picture(QPixmap(), _("Cover from the web"))
        self._show_hover_actions()

    def _on_picture(self, sha: str, _path: str, picture: QPixmap) -> None:
        if sha.lower() == self._cover_hash and not picture.isNull():
            self.banner.set_picture(picture)

    def _show_hover_actions(self) -> None:
        """Add Cover, and a cover's Change and Remove, show while the pointer
        is over the head or one of them has focus."""
        near = self.underMouse() or any(
            widget.hasFocus() for widget in (self.add_cover, self.change_cover,
                                             self.remove_cover, self.title_edit,
                                             self.summary_edit))
        self._add_cover_look.setOpacity(1.0 if near else 0.0)
        cover_near = self.banner.underMouse() or self.change_cover.hasFocus() \
            or self.remove_cover.hasFocus()
        self._cover_actions_look.setOpacity(1.0 if cover_near else 0.0)

    # -- typing -------------------------------------------------------------------

    def _typed(self) -> None:
        if not self._filling:
            self.edited.emit(self.title_edit.text().strip(),
                             self.summary_edit.toPlainText().strip())

    def _to_summary(self) -> None:
        self.summary_edit.setFocus(Qt.FocusReason.TabFocusReason)

    def enter_from_below(self) -> None:
        self.summary_edit.setFocus(Qt.FocusReason.BacktabFocusReason)
        self.summary_edit.moveCursor(QTextCursor.MoveOperation.End)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn,
                            QEvent.Type.FocusOut):
            self._show_hover_actions()
        if (watched is self.title_edit and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Down):
            self._to_summary()
            return True
        return False
