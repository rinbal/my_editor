# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Imports window's list of posts.

Each row shows what is needed to decide whether a post is worth
importing, the way a mail list shows a message:

    [ ] Why self-custody still matters                     [cover]
        A  A thoughtful journal · 2 h ago · 7 min · 3 images
        Every cycle brings a new reason to hand your keys to
        someone else. The trade always looks small at the time.

The box in front checks a post; the actions of the window apply to the
checked posts, or to the open one when none is checked (Mail's rule,
workspace.targets). A word after the title says what became of a post
(Importing, Imported, Skipped, Failed) wherever the list itself does not
already say it.

Keys (the conventions of Mail, Finder and Reminders): Up and Down move,
Space checks or unchecks the open post, Command-A or Ctrl+A checks every
post, Escape unchecks them all, Delete or Backspace asks to skip.
Typing a title's first letters still finds it.

:class:`PostListModel` holds the posts and the checks and loads a long
list a page at a time as it scrolls; :class:`PostList` is the view;
:class:`PostDelegate` paints a row. Covers come from an image source
(the controller's RemoteImages): a row asks for its cover when it is
painted and is repainted when the cover arrives.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Set

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
)

from . import appearance
from i18n import _, ngettext

from ..imports.workspace import Post, date_text, selectable, state_word
from .imports_glyphs import is_dark, source_avatar

PostRole = Qt.ItemDataRole.UserRole + 1
CheckedRole = Qt.ItemDataRole.UserRole + 2

PADDING = 12
CHECK = 18
THUMB = 64
ROW_HEIGHT = 96

# fetch(after) -> the next posts after ``after`` (None: the first ones).
Fetch = Callable[[Optional[Post]], List[Post]]


class PostListModel(QAbstractListModel):
    """The posts of the list shown, and which of them are checked.

    Signals:
      checks_changed()   a post was checked or unchecked
    """

    checks_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._posts: List[Post] = []
        self._checked: Set[str] = set()
        self._fetch: Optional[Fetch] = None
        self._paged = False
        self._more = False
        self._page_size = 50
        self._busy: Set[str] = set()
        self._failed: Set[str] = set()
        self._hidden_word = ""

    # -- what is shown ---------------------------------------------------------

    def show(self, fetch: Fetch, *, paged: bool, page_size: int = 50,
             hidden_word: str = "") -> None:
        """Show a new list: its first page now, more as it scrolls.
        ``hidden_word`` is the state word the list itself already says
        (every post in Skipped is skipped)."""
        self.beginResetModel()
        self._fetch, self._paged, self._page_size = fetch, paged, page_size
        self._hidden_word = hidden_word
        self._posts = list(fetch(None))
        self._more = paged and len(self._posts) >= page_size
        self._checked.clear()
        self.endResetModel()
        self.checks_changed.emit()

    def reload(self) -> None:
        """Read the list again (posts arrived, moved or changed), keeping
        as many rows loaded as before and the checks that still apply."""
        if self._fetch is None:
            return
        wanted = max(len(self._posts), 1)
        posts: List[Post] = []
        after: Optional[Post] = None
        more = False
        while True:
            page = self._fetch(after)
            posts.extend(page)
            more = self._paged and len(page) >= self._page_size
            if not more or len(posts) >= wanted or not page:
                break
            after = page[-1]
        if [(p.key, p) for p in posts] == [(p.key, p) for p in self._posts] and \
                more == self._more:
            return
        self.layoutAboutToBeChanged.emit()
        old_keys = [p.key for p in self._posts]
        persistent = self.persistentIndexList()
        self._posts, self._more = posts, more
        new_rows = {p.key: row for row, p in enumerate(self._posts)}
        for index in persistent:
            key = old_keys[index.row()] if 0 <= index.row() < len(old_keys) else ""
            row = new_rows.get(key, -1)
            self.changePersistentIndex(index, self.index(row) if row >= 0 else QModelIndex())
        self.layoutChanged.emit()
        before = set(self._checked)
        # One pass, not a search per check (review M10: quadratic with
        # thousands of posts checked).
        self._checked &= {p.key for p in posts if selectable(p, self._busy)}
        if self._checked != before:
            self.checks_changed.emit()

    def set_progress(self, busy: Iterable[str], failed: Iterable[str]) -> None:
        """Identifiers an import is making now, and those it could not."""
        busy, failed = set(busy), set(failed)
        if busy == self._busy and failed == self._failed:
            return
        self._busy, self._failed = busy, failed
        if self._posts:
            self.dataChanged.emit(self.index(0), self.index(len(self._posts) - 1))

    def busy_identifiers(self) -> Set[str]:
        return set(self._busy)

    def word_for(self, post: Post) -> str:
        word = state_word(post, importing=post.d_tag in self._busy,
                          failed=post.d_tag in self._failed)
        return "" if word == self._hidden_word else word

    # -- reading ---------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._posts)

    def posts(self) -> List[Post]:
        return list(self._posts)

    def post(self, row: int) -> Optional[Post]:
        return self._posts[row] if 0 <= row < len(self._posts) else None

    def post_by_key(self, key: str) -> Optional[Post]:
        return next((p for p in self._posts if p.key == key), None)

    def row_of(self, key: str) -> int:
        return next((row for row, p in enumerate(self._posts) if p.key == key), -1)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        post = self.post(index.row())
        if post is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return post.title or _("Untitled")
        if role == PostRole:
            return post
        if role == CheckedRole:
            return post.key in self._checked
        if role == Qt.ItemDataRole.CheckStateRole:
            # The check, for assistive technology too (review M11): a
            # screen reader says "checked" for it and "selected" for the
            # open row, which are two different things.
            return (Qt.CheckState.Checked if post.key in self._checked
                    else Qt.CheckState.Unchecked)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            parts = [post.title or _("Untitled"), post.source_title,
                     date_text(post.published_at or post.found_at)]
            word = self.word_for(post)
            if word:
                parts.append(word)
            return ", ".join(p for p in parts if p)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or self.post(index.row()) is None:
            return False
        checked = Qt.CheckState(value) == Qt.CheckState.Checked if not isinstance(
            value, bool) else value
        self.set_checked(index.row(), checked)
        return True

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        post = self.post(index.row())
        if post is not None and selectable(post, self._busy):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    # -- paging ----------------------------------------------------------------

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and self._more

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or not self._more or self._fetch is None or not self._posts:
            return
        page = self._fetch(self._posts[-1])
        known = {p.key for p in self._posts}
        page = [p for p in page if p.key not in known]
        self._more = len(page) >= self._page_size
        if not page:
            self._more = False
            return
        start = len(self._posts)
        self.beginInsertRows(QModelIndex(), start, start + len(page) - 1)
        self._posts.extend(page)
        self.endInsertRows()

    # -- checks ----------------------------------------------------------------

    def checked(self) -> List[Post]:
        return [p for p in self._posts if p.key in self._checked]

    def is_checked(self, post: Post) -> bool:
        return post.key in self._checked

    def set_checked(self, row: int, checked: bool) -> None:
        post = self.post(row)
        if post is None or (checked and not selectable(post, self._busy)):
            return
        if checked == (post.key in self._checked):
            return
        if checked:
            self._checked.add(post.key)
        else:
            self._checked.discard(post.key)
        index = self.index(row)
        self.dataChanged.emit(index, index)
        self.checks_changed.emit()

    def toggle(self, row: int) -> None:
        post = self.post(row)
        if post is not None:
            self.set_checked(row, post.key not in self._checked)

    def check_all(self) -> None:
        """Check every post that can be imported (loading the whole list)."""
        while self.canFetchMore():
            self.fetchMore()
        keys = {p.key for p in self._posts if selectable(p, self._busy)}
        if keys == self._checked:
            return
        self._checked = keys
        self._emit_all()

    def clear_checks(self) -> None:
        if not self._checked:
            return
        self._checked.clear()
        self._emit_all()

    def selectable_count(self) -> int:
        return sum(1 for p in self._posts if selectable(p, self._busy))

    def check_state(self) -> Qt.CheckState:
        if not self._checked:
            return Qt.CheckState.Unchecked
        if len(self._checked) >= self.selectable_count() and not self._more:
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def _emit_all(self) -> None:
        if self._posts:
            self.dataChanged.emit(self.index(0), self.index(len(self._posts) - 1))
        self.checks_changed.emit()

    def rows_with_image(self, url: str) -> List[int]:
        return [row for row, p in enumerate(self._posts) if p.image == url]


class PostDelegate(QStyledItemDelegate):
    """Paints one post: check box, title and word, source and figures,
    excerpt, cover."""

    def __init__(self, view: "PostList", images=None) -> None:
        super().__init__(view)
        self._view = view
        self._images = images

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.row_height(option.font))

    @staticmethod
    def row_height(font: QFont) -> int:
        metrics = QFontMetrics(font)
        line = metrics.lineSpacing()
        return max(ROW_HEIGHT, PADDING * 2 + line * 4 + 8)

    @staticmethod
    def check_rect(rect: QRect) -> QRect:
        return QRect(rect.left() + PADDING, rect.top() + PADDING + 1, CHECK, CHECK)

    def paint(self, painter: QPainter, option, index) -> None:
        post: Post = index.data(PostRole)
        if post is None:
            return
        model: PostListModel = index.model()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = option.palette
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = self._view.hasFocus()
        text = palette.color(QPalette.ColorRole.Text)
        muted = palette.color(QPalette.ColorRole.PlaceholderText)
        if selected:
            dark = is_dark(palette)
            fill = (appearance.gradient(dark, QRectF(rect)) if focused
                    else QColor(appearance.roles(dark).soft))
            if focused:
                text = muted = QColor("#FFFFFF")
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(rect).adjusted(5, 1, -5, -1), 8, 8)
        else:
            separator = QColor(palette.color(QPalette.ColorRole.Mid))
            separator.setAlpha(70)
            painter.fillRect(QRect(rect.left() + PADDING + CHECK + 10, rect.bottom(),
                                   rect.width() - PADDING - CHECK - 10, 1), separator)

        # The check box, as the platform draws one.
        box = QStyleOptionButton()
        box.rect = self.check_rect(rect)
        box.state = QStyle.StateFlag.State_Enabled if selectable(
            post, model.busy_identifiers()) else QStyle.StateFlag.State_None
        box.state |= (QStyle.StateFlag.State_On if model.is_checked(post)
                      else QStyle.StateFlag.State_Off)
        style = self._view.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox, box, painter,
                            self._view)

        left = rect.left() + PADDING + CHECK + 10
        right = rect.right() - PADDING
        cover_shown = bool(post.image)
        if cover_shown:
            self._paint_cover(painter, post, QRect(right - THUMB, rect.top() + PADDING,
                                                   THUMB, THUMB), palette)
            right -= THUMB + 12
        width = max(40, right - left)

        base = QFont(option.font)
        title_font = QFont(base)
        title_font.setWeight(QFont.Weight.DemiBold)
        small = QFont(base)
        small.setPointSizeF(max(8.0, base.pointSizeF() - 1.5))
        title_metrics = QFontMetrics(title_font)
        small_metrics = QFontMetrics(small)
        base_metrics = QFontMetrics(base)

        y = rect.top() + PADDING - 1
        # Title, and the word for what became of the post.
        word = model.word_for(post)
        word_width = 0
        if word:
            word_width = small_metrics.horizontalAdvance(word) + 14
        painter.setFont(title_font)
        painter.setPen(text)
        title = title_metrics.elidedText(post.title or _("Untitled"),
                                         Qt.TextElideMode.ElideRight,
                                         width - (word_width + 8 if word else 0))
        painter.drawText(QRect(left, y, width, title_metrics.lineSpacing()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        if word:
            capsule = QRect(left + title_metrics.horizontalAdvance(title) + 8,
                            y + (title_metrics.lineSpacing() - small_metrics.height()) // 2 - 1,
                            word_width, small_metrics.height() + 2)
            self._paint_word(painter, capsule, word, small, palette, selected and focused)
        y += title_metrics.lineSpacing() + 3

        # Source, date and the figures.
        avatar = 14
        icon_for = self._view.icon_for
        image = icon_for(post.source_key, post.source_url) if icon_for is not None else None
        painter.drawPixmap(QRect(left, y + (small_metrics.lineSpacing() - avatar) // 2,
                                 avatar, avatar),
                           source_avatar(post.source_title, post.source_url or post.source_key,
                                         avatar, dark=is_dark(palette),
                                         dpr=self._view.devicePixelRatioF(), image=image))
        parts = [post.source_title, date_text(post.published_at or post.found_at)]
        if post.read_minutes:
            parts.append(ngettext("{n} min", "{n} min", post.read_minutes).format(
                n=post.read_minutes))
        if post.image_count:
            parts.append(ngettext("{n} image", "{n} images", post.image_count).format(
                n=post.image_count))
        meta = "  ·  ".join(p for p in parts if p)
        painter.setFont(small)
        painter.setPen(muted)
        painter.drawText(QRect(left + avatar + 6, y, width - avatar - 6,
                               small_metrics.lineSpacing()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         small_metrics.elidedText(meta, Qt.TextElideMode.ElideRight,
                                                  width - avatar - 6))
        y += small_metrics.lineSpacing() + 4

        # Two lines of the excerpt.
        if post.excerpt:
            painter.setFont(base)
            painter.setPen(muted if not (selected and focused) else text)
            lines = _two_lines(post.excerpt, base_metrics, width)
            for line in lines:
                painter.drawText(QRect(left, y, width, base_metrics.lineSpacing()),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 line)
                y += base_metrics.lineSpacing()
        painter.restore()

    def _paint_word(self, painter: QPainter, rect: QRect, word: str, font: QFont,
                    palette: QPalette, on_highlight: bool) -> None:
        fill = QColor(palette.color(QPalette.ColorRole.Mid))
        fill.setAlpha(80 if not on_highlight else 60)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(rect), rect.height() / 2, rect.height() / 2)
        painter.setFont(font)
        painter.setPen(palette.color(QPalette.ColorRole.HighlightedText) if on_highlight
                       else palette.color(QPalette.ColorRole.Text))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, word)

    def _cover_size(self, rect: QRect) -> QSize:
        """The cover's square in device pixels: what its decode must cover."""
        dpr = self._view.devicePixelRatioF() if self._view is not None else 1.0
        return QSize(int(rect.width() * dpr), int(rect.height() * dpr))

    def _paint_cover(self, painter: QPainter, post: Post, rect: QRect,
                     palette: QPalette) -> None:
        image = self._images.image(post.image) if self._images is not None else None
        painter.save()
        frame = QRectF(rect)
        if image is not None and self._images is not None:
            # A sharper one when only a smaller decode is here.
            self._images.request(post.image, self._cover_size(rect))
        if image is None or image.isNull():
            if self._images is not None:
                self._images.request(post.image, self._cover_size(rect))
            fill = QColor(palette.color(QPalette.ColorRole.Mid))
            fill.setAlpha(60)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(frame, 6, 6)
            painter.restore()
            return
        clip = QPainterPath()
        clip.addRoundedRect(frame, 6, 6)
        painter.setClipPath(clip)
        # Fill the square, cropping the longer side evenly.
        scale = max(rect.width() / image.width(), rect.height() / image.height())
        shown = QRectF(0, 0, image.width() * scale, image.height() * scale)
        shown.moveCenter(frame.center())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(shown, image)
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        """A click on a check box checks the post, without opening it."""
        if event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            if self.check_rect(option.rect).adjusted(-6, -6, 6, 6).contains(
                    event.position().toPoint()):
                if event.type() == QEvent.Type.MouseButtonRelease:
                    model.toggle(index.row())
                return True
        if event.type() == QEvent.Type.MouseButtonPress:
            if self.check_rect(option.rect).adjusted(-6, -6, 6, 6).contains(
                    event.position().toPoint()):
                return True
        return super().editorEvent(event, model, option, index)


def _two_lines(text: str, metrics: QFontMetrics, width: int) -> List[str]:
    """``text`` wrapped at word boundaries into two lines, the second
    elided when there is more."""
    words = text.split()
    first = ""
    while words:
        candidate = (first + " " + words[0]).strip()
        if metrics.horizontalAdvance(candidate) > width:
            break
        first = candidate
        words.pop(0)
    if not first and words:
        return [metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)]
    rest = " ".join(words)
    if not rest:
        return [first]
    return [first, metrics.elidedText(rest, Qt.TextElideMode.ElideRight, width)]


class PostList(QListView):
    """The list of posts.

    Signals:
      open_post(object)      the open post changed (a Post, or None)
      skip_requested()       Delete or Backspace
    """

    open_post = Signal(object)
    skip_requested = Signal()

    def __init__(self, images=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("imports_posts")
        self.setAccessibleName(_("Posts"))
        self.model_ = PostListModel(self)
        self.setModel(self.model_)
        self.setItemDelegate(PostDelegate(self, images))
        self.icon_for = None        # (source key, address) -> site icon (C12)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.selectionModel().currentChanged.connect(self._on_current)
        self.model_.modelReset.connect(self._on_reset)
        if images is not None:
            images.ready.connect(self._on_image)

    def current_post(self) -> Optional[Post]:
        return self.model_.post(self.currentIndex().row())

    def open(self, key: str) -> bool:
        row = self.model_.row_of(key)
        if row < 0:
            return False
        self.setCurrentIndex(self.model_.index(row))
        return True

    def _on_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        self.open_post.emit(self.model_.post(current.row()))

    def _on_reset(self) -> None:
        self.open_post.emit(self.current_post())

    def _on_image(self, url: str) -> None:
        for row in self.model_.rows_with_image(url):
            self.update(self.model_.index(row))

    def keyPressEvent(self, event) -> None:
        key = event.key()
        row = self.currentIndex().row()
        if key == Qt.Key.Key_Space and not event.modifiers():
            if row >= 0:
                self.model_.toggle(row)
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.model_.check_all()
            return
        if key == Qt.Key.Key_Escape and self.model_.checked():
            self.model_.clear_checks()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not event.modifiers():
            self.skip_requested.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.viewport().update()
        if not self.currentIndex().isValid() and self.model_.rowCount():
            self.setCurrentIndex(self.model_.index(0))

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.viewport().update()


__all__ = ["CheckedRole", "PostDelegate", "PostList", "PostListModel", "PostRole"]
