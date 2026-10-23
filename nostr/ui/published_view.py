# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Published view: what the account published (articles, curations and
notes), and taking it back.

  ┌──────────────────────────────────────────┐
  │  Why self-custody still matters          │  an article's title, or a
  │  Article · 7 October 2026                │  note's first line; what it is
  │  Hello from MyEditor                     │  and when it first went out
  │  Note · 6 October 2026                   │
  │             Load More Notes              │  while older notes may be there
  └──────────────────────────────────────────┘

The view owns its list, its states and its actions, and fits in any
container; the Drafts panel shows it as its second view, Published
(:func:`add_to_panel`). Its host gives it an account and hears from it
through two signals:

    set_account(profile)     whose items to show (None: nobody's)
    refresh()                read again (the panel's refresh button)
    set_search_text(text)    show only what holds ``text`` (the panel's
                             search field, while this view is shown)
    apply_theme(is_dark)
    open_requested(object)   an article to edit in a tab (a PublishedItem):
                             bound to the article's identifier, so that
                             publishing it again replaces it and keeps its
                             first publication date
    status_message(str)      a sentence for the host's status line ("" to
                             clear it)

What is read and how an item is deleted is nostr/published.py
(PublishedList); the view shows that and passes on what the person asks.

The actions are on the context menu (also from the Menu key): Edit (an
article), Open in Browser, Copy Link, Delete from Nostr…. Return opens a
row (an article for editing, a note in the browser), Delete or Backspace
deletes it, the arrow keys move. Deleting asks first, with Cancel as the
default button, as everything that cannot be undone does; a deletion no
relay took is said in an alert that offers Try Again.

States: no account, the first read under way, the list, nothing
published yet, and could not be read (with Try Again).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor, QContextMenuEvent, QDesktopServices, QFontMetrics, QGuiApplication, QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QLabel, QListView, QMenu, QProgressBar,
    QPushButton, QStackedLayout, QStyle, QVBoxLayout, QWidget,
)

import alerts
import theme
from i18n import _, ngettext

from .. import web_readers
from ..bunker import humanize_failure, is_signer_silent
from ..draft_store import DraftState
from ..drafts import derive_preview_snippet
from ..publish_pipeline import refused_by_relay
from ..published import (
    DELETING, LOADING, READY, SIGNED_OUT, UNREACHABLE, PublishedItem, PublishedList,
)
from . import post_list
from .drafts_preview import PreviewAction, PreviewController
from .drafts_common import (
    LIST_GUTTER, MIN_CONTROL_PX, ROW_LINE_GAP, THEME_TOKENS, format_absolute_time,
    secondary_font,
)

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1     # the PublishedItem, None for Load More
STATE_ROLE = Qt.ItemDataRole.UserRole + 4    # the item's deletion state
PROBLEM_ROLE = Qt.ItemDataRole.UserRole + 5  # Load More: what went wrong, or ""

# Where a long title is cut in an alert's title.
ALERT_TITLE_CHARS = 60


@dataclass
class PublishedRecord:
    """A published item in the shape the preview reads a draft in
    (drafts_preview.py), so one preview serves both lists: what it says,
    its text and its tags. ``is_published`` makes its origin line say when
    it first went out, not when it was saved."""

    identifier: str
    inner_kind: int
    title: str
    snippet: str
    content: str
    created_at: int
    event_id: str
    inner_tags: List[list] = field(default_factory=list)
    state: DraftState = DraftState.READY
    expiration: Optional[int] = None
    ciphertext: str = ""
    failure_reason: str = ""
    is_published: bool = True


def preview_record_of(item: PublishedItem) -> PublishedRecord:
    content = str(item.event.get("content") or "")
    tags = [list(tag) for tag in item.event.get("tags", []) or [] if isinstance(tag, list)]
    if item.is_curation:
        # A curation's text is what it collects; its description is the
        # preview's summary.
        description = _tag_value(item, "description")
        if description and not _tag_value(item, "summary"):
            tags.append(["summary", description])
        content = ngettext("Collects {n} article.", "Collects {n} articles.",
                           item.curated).format(n=item.curated)
    return PublishedRecord(
        identifier=item.id, inner_kind=item.kind, title=display_title(item),
        snippet=derive_preview_snippet(content), content=content,
        created_at=item.first_published, event_id=item.id, inner_tags=tags)


# --------------------------------------------------------------------------- #
# Words                                                                       #
# --------------------------------------------------------------------------- #

def kind_word(item: PublishedItem) -> str:
    if item.is_curation:
        return _("Curation")
    return _("Article") if item.is_article else _("Note")


def web_kind(item: PublishedItem) -> str:
    """What a reading app is asked to show (web_readers): an article, a
    curation or a short note."""
    if item.is_curation:
        return web_readers.CURATION
    return web_readers.ARTICLE if item.is_article else web_readers.NOTE


def _tag_value(item: PublishedItem, name: str) -> str:
    for tag in item.event.get("tags", []) or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            return " ".join(str(tag[1]).split())
    return ""


def curation_words(item: PublishedItem) -> str:
    """What a curation is, in words: its description, else how many
    articles it collects."""
    return (_tag_value(item, "description") or _tag_value(item, "summary")
            or ngettext("Collects {n} article", "Collects {n} articles",
                        item.curated).format(n=item.curated))


def display_title(item: PublishedItem) -> str:
    return item.title or _("Untitled")


def short_title(item: PublishedItem, limit: int = ALERT_TITLE_CHARS) -> str:
    title = display_title(item)
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "…"


def published_row(item: PublishedItem, deletion: str = "", *, now: int) -> post_list.PostRow:
    """One published item as its row draws it (post_list.py): the title,
    then what it is, when it first went out, and the words after the
    title (an article's summary when it has one), never the title again."""
    when = post_list.list_date(item.first_published, now)
    kind = web_kind(item)
    if deletion == DELETING:
        return post_list.PostRow(display_title(item), kind=kind, when=when,
                                 detail=_("Deleting…"), state=post_list.DELETING)
    content = str(item.event.get("content") or "")
    if item.is_curation:
        return post_list.PostRow(display_title(item), kind=kind, when=when,
                                 detail=curation_words(item))
    summary = _summary(item) if item.is_article else ""
    detail = summary or post_list.words_after_title(item.title,
                                                    derive_preview_snippet(content))
    return post_list.PostRow(display_title(item), kind=kind, when=when,
                             detail=detail or _("No additional text"))


def _summary(item: PublishedItem) -> str:
    return _tag_value(item, "summary")


def accessible_text(item: PublishedItem, deletion: str = "") -> str:
    """What a screen reader says for a row, in reading order."""
    sentences = [display_title(item), kind_word(item),
                 _("First published {time}").format(
                     time=format_absolute_time(item.first_published))]
    if deletion == DELETING:
        sentences.append(_("Deleting"))
    return ". ".join(s.rstrip(". ") for s in sentences) + "."


def more_label(model: PublishedList) -> str:
    """The Load More row's words, which stay its action after a failure:
    pressing it again is trying again."""
    return _("Loading…") if model.loading_more else _("Load More Notes")


def more_problem(model: PublishedList) -> str:
    """Under the action, after a Load More that reached no relay. Short,
    so it fits the narrowest panel; the status line says the rest."""
    return _("Couldn’t load.") if model.more_failed and not model.loading_more else ""


def more_accessible(model: PublishedList) -> str:
    if model.loading_more:
        return _("Loading more notes")
    if model.more_failed:
        return _("Load More Notes. Couldn’t load more notes. Press Return to try again.")
    return _("Load More Notes")


# --------------------------------------------------------------------------- #
# The rows                                                                    #
# --------------------------------------------------------------------------- #

class _Rows(QAbstractListModel):
    """The list's rows: the items, then Load More while older notes may
    be there. ``reload`` keeps the selection and the scroll position."""

    _MORE = "\x00more"

    def __init__(self, model: PublishedList, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._items: List[PublishedItem] = []
        self._rows: List[post_list.PostRow] = []
        self._more = False
        self._needle = ""

    def set_search(self, text: str) -> None:
        """Show only the items whose title or text holds ``text``."""
        self._needle = " ".join(text.split()).casefold()
        self.reload()

    def searching(self) -> bool:
        return bool(self._needle)

    def _matches(self, item: PublishedItem) -> bool:
        if not self._needle:
            return True
        return self._needle in f"{item.title} {item.event.get('content') or ''}".casefold()

    def keys(self) -> List[str]:
        return [item.id for item in self._items] + ([self._MORE] if self._more else [])

    def reload(self) -> None:
        items = ([item for item in self._model.items() if self._matches(item)]
                 if self._model.state == READY else [])
        more = self._model.state == READY and (
            self._model.has_more or self._model.loading_more or self._model.more_failed)
        old_keys = self.keys()
        new_keys = [item.id for item in items] + ([self._MORE] if more else [])
        if new_keys == old_keys:
            self._items = items
            self._draw_rows()
            if new_keys:
                self.dataChanged.emit(self.index(0), self.index(len(new_keys) - 1))
            return
        self.layoutAboutToBeChanged.emit()
        persistent = self.persistentIndexList()
        moved = [old_keys[index.row()] if 0 <= index.row() < len(old_keys) else None
                 for index in persistent]
        self._items, self._more = items, more
        self._draw_rows()
        rows = {key: row for row, key in enumerate(new_keys)}
        for index, key in zip(persistent, moved):
            row = rows.get(key, -1)
            self.changePersistentIndex(index, self.index(row) if row >= 0 else QModelIndex())
        self.layoutChanged.emit()

    def _draw_rows(self) -> None:
        """Each item's row, with its date section on the first of each."""
        now = int(time.time())
        sections = post_list.section_starts([item.first_published for item in self._items],
                                            now)
        self._rows = [published_row(item, self._model.deletion_state(item.id), now=now)
                      ._replace(section=section)
                      for item, section in zip(self._items, sections)]

    def item_at(self, row: int) -> Optional[PublishedItem]:
        return self._items[row] if 0 <= row < len(self._items) else None

    def is_more(self, row: int) -> bool:
        return self._more and row == len(self._items)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items) + (1 if self._more else 0)

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.item_at(index.row())
        if item is None:
            if not self.is_more(index.row()):
                return None
            if role == Qt.ItemDataRole.DisplayRole:
                return more_label(self._model)
            if role == Qt.ItemDataRole.AccessibleTextRole:
                return more_accessible(self._model)
            if role == PROBLEM_ROLE:
                return more_problem(self._model)
            return None
        state = self._model.deletion_state(item.id)
        if role == Qt.ItemDataRole.DisplayRole:
            return display_title(item)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return accessible_text(item, state)
        if role == post_list.ROW_ROLE:
            return self._rows[index.row()]
        if role == ITEM_ROLE:
            return item
        if role == STATE_ROLE:
            return state
        return None


class _RowDelegate(post_list.PostRowDelegate):
    """The rows as the drafts list draws them (post_list.py); Load More is
    centred, in the link colour."""

    def __init__(self, view: "PublishedView", list_view: QListView) -> None:
        super().__init__(list_view, is_dark=view.is_dark)
        self._published = view

    def paint_other(self, painter, option, index) -> None:
        """Load More: its action centred, in the link colour; after a
        failure, the action on line 1 and what went wrong under it."""
        tokens = THEME_TOKENS[self._published.is_dark]
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        inner = option.rect.adjusted(LIST_GUTTER, post_list.ROW_PAD_V, -LIST_GUTTER,
                                     -post_list.ROW_PAD_V)
        loading = self._published.model.loading_more
        action_colour = (tokens["muted"] if loading
                         else theme.dialog_link_color(self._published.is_dark))
        label = index.data(Qt.ItemDataRole.DisplayRole) or ""
        problem = index.data(PROBLEM_ROLE) or ""
        painter.save()
        painter.setFont(QApplication.font())
        painter.setPen(QColor(action_colour))
        centre = int(Qt.AlignmentFlag.AlignCenter)
        if not problem:
            painter.drawText(inner, centre, painter.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideRight, inner.width()))
        else:
            line_1 = QRect(inner.left(), inner.top(), inner.width(),
                           painter.fontMetrics().height())
            painter.drawText(line_1, centre, painter.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideRight, inner.width()))
            secondary = secondary_font()
            line_2 = QRect(inner.left(), line_1.bottom() + 1 + ROW_LINE_GAP, inner.width(),
                           QFontMetrics(secondary).height())
            painter.setFont(secondary)
            painter.setPen(QColor(tokens["error_fg"]))
            painter.drawText(line_2, centre, painter.fontMetrics().elidedText(
                problem, Qt.TextElideMode.ElideRight, inner.width()))
        if selected:
            painter.setPen(QColor(tokens["accent"]))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(option.rect.adjusted(post_list.SELECTION_INSET, 1,
                                                         -post_list.SELECTION_INSET, -1),
                                    post_list.SELECTION_RADIUS, post_list.SELECTION_RADIUS)
        painter.restore()


class _List(QListView):
    """The list, with a context menu for the row it is about: the one
    under the pointer, or from the keyboard (the Menu key) the current
    one, not whatever row the middle of the list happens to show."""

    menu_requested = Signal(QModelIndex, QPoint)     # the row, where (global)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if event.reason() == QContextMenuEvent.Reason.Keyboard:
            index = self.currentIndex()
            where = self.visualRect(index).center() if index.isValid() else QPoint()
            point = self.viewport().mapToGlobal(where)
        else:
            index = self.indexAt(event.pos())
            point = event.globalPos()
            if index.isValid():
                self.setCurrentIndex(index)
        event.accept()
        if index.isValid():
            self.menu_requested.emit(index, point)


# --------------------------------------------------------------------------- #
# The view                                                                    #
# --------------------------------------------------------------------------- #

def _view_css(is_dark: bool) -> str:
    """Colour and weight from the drafts surfaces' tokens; sizes are set in
    Python, so they follow the application font."""
    t = THEME_TOKENS[bool(is_dark)]
    return f"""
QWidget#published_view {{ background: {t["panel_bg"]}; }}
QListView#published_list {{ background: {t["panel_bg"]}; border: none; }}
QLabel#published_empty_title {{ color: {t["chrome_fg"]}; font-weight: 600; }}
QLabel#published_empty_body {{ color: {t["muted"]}; }}
QPushButton#published_empty_action {{
    background: transparent;
    color: {t["row_fg"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    padding: 4px 12px;
}}
QPushButton#published_empty_action:hover {{ background: {t["hover_bg"]}; }}
QPushButton#published_empty_action:focus {{ border-color: {t["accent"]}; }}
QProgressBar#published_busy {{
    background: {t["chrome_bg"]};
    border: 1px solid {t["border"]};
    border-radius: 3px;
    max-height: 6px;
}}
QProgressBar#published_busy::chunk {{ background: {t["accent"]}; border-radius: 2px; }}
"""


# The view's key among the Drafts panel's views.
PANEL_VIEW = "published"


def add_to_panel(panel, view: "PublishedView") -> None:
    """Make ``view`` the Drafts panel's second view, Published: the
    switch names it beside Drafts, and the refresh button reads it again
    while it is shown."""
    panel.add_view(PANEL_VIEW, _("Published"), _("Your published notes and articles"), view,
                   refresh=view.refresh,
                   refresh_name=_("Refresh Published Notes and Articles"),
                   search=view.set_search_text, search_name=_("Search Published"))


class PublishedView(QWidget):
    """The account's published notes and articles (see the module's text).

    Signals:
      open_requested(object)   an article (PublishedItem) to edit in a tab
      status_message(str)      a sentence for the host's status line
    """

    open_requested = Signal(object)
    status_message = Signal(str)

    def __init__(self, model: PublishedList, *, is_dark: bool = True,
                 page_check=None, reader_choice=lambda: "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("published_view")
        # Asks which of Standup's addresses for a post is there before it
        # is opened (web_readers.PageCheck.first_found), or None: open the
        # first without asking.
        self._page_check = page_check
        # The reading app chosen in Settings ("" for the first offered).
        self._reader_choice = reader_choice
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._model = model
        self._is_dark = bool(is_dark)
        self._rows = _Rows(model, self)
        self._last = (model.state, model.refreshing, model.more_failed)
        self._search_text = ""
        self._build()
        # Bound methods, not lambdas: they go with the view if it goes first.
        model.changed.connect(self._on_changed)
        model.deletion_status.connect(self._on_deletion_status)
        model.deletion_done.connect(self._on_deleted)
        model.deletion_not_taken.connect(self._on_not_taken)
        model.deletion_refused.connect(self._on_refused)
        self.apply_theme(self._is_dark)
        self._on_changed()

    # -- the host's interface -------------------------------------------------

    @property
    def is_dark(self) -> bool:
        return self._is_dark

    @property
    def model(self) -> PublishedList:
        return self._model

    def set_account(self, profile) -> None:
        self._model.set_account(profile)

    def set_search_text(self, text: str) -> None:
        """Filter the list by what is typed in the panel's search field."""
        self._search_text = text.strip()
        self._rows.set_search(text)
        self._show_state()

    def refresh(self) -> None:
        self._model.refresh()

    def apply_theme(self, is_dark: bool) -> None:
        from . import appearance
        appearance.bind(self)
        self.setPalette(appearance.palette(is_dark))
        self._is_dark = bool(is_dark)
        self.setStyleSheet(_view_css(self._is_dark))
        self._delegate.set_dark(self._is_dark)
        self._preview.apply_theme(self._is_dark)
        self._paint_empty_symbol()

    # -- building ---------------------------------------------------------------

    def _build(self) -> None:
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._list = _List()
        self._list.setObjectName("published_list")
        self._list.setAccessibleName(_("Published"))
        self._list.setModel(self._rows)
        self._delegate = _RowDelegate(self, self._list)
        self._list.setItemDelegate(self._delegate)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # A section's first row is taller by the section's title.
        self._list.setUniformItemSizes(False)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setMouseTracking(True)
        self._list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._list.activated.connect(self._on_activated)
        self._list.clicked.connect(self._on_clicked)
        self._list.menu_requested.connect(self._show_menu)
        # Resting the pointer on a row shows its short preview; a click or
        # Space its full one, styled as Nostr shows it (drafts_preview.py,
        # as in the Drafts view).
        self._preview = PreviewController(self)
        self._preview.attach_list(self._list)
        # Return and Delete belong to the list, see ``eventFilter``.
        self._list.installEventFilter(self)
        self._stack.addWidget(self._list)

        self._placeholder = QWidget()
        column = QVBoxLayout(self._placeholder)
        column.setContentsMargins(LIST_GUTTER, 24, LIST_GUTTER, 24)
        column.setSpacing(8)
        column.addStretch(1)
        self._busy = QProgressBar()
        self._busy.setObjectName("published_busy")
        self._busy.setRange(0, 0)               # the wait has no known length
        self._busy.setTextVisible(False)
        self._busy.setMaximumWidth(160)
        self._busy.setAccessibleName(_("Loading"))
        column.addWidget(self._busy, 0, Qt.AlignmentFlag.AlignHCenter)
        # A quiet symbol over the title, as Apple's empty states have one.
        self._empty_symbol = QLabel()
        self._empty_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_symbol_name = ""
        column.addWidget(self._empty_symbol)
        self._empty_title = QLabel()
        self._empty_title.setObjectName("published_empty_title")
        self._empty_body = QLabel()
        self._empty_body.setObjectName("published_empty_body")
        self._empty_body.setFont(secondary_font())
        for label in (self._empty_title, self._empty_body):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(label)
        self._empty_action = QPushButton(_("Try Again"))
        self._empty_action.setObjectName("published_empty_action")
        self._empty_action.setMinimumHeight(MIN_CONTROL_PX)
        self._empty_action.clicked.connect(self._model.refresh)
        column.addWidget(self._empty_action, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(2)
        self._stack.addWidget(self._placeholder)

    # -- what is shown ----------------------------------------------------------

    def _on_changed(self) -> None:
        current = self._list.currentIndex()
        row = current.row() if current.isValid() else -1
        self._rows.reload()
        if row >= 0 and not self._list.currentIndex().isValid() and self._rows.rowCount():
            # The row went (deleted): the keyboard stays where it was.
            nearest = self._rows.index(min(row, self._rows.rowCount() - 1))
            self._list.selectionModel().setCurrentIndex(
                nearest, self._list.selectionModel().SelectionFlag.ClearAndSelect)
        self._show_state()
        self._say_what_changed()

    def _show_state(self) -> None:
        state = self._model.state
        if state == SIGNED_OUT:
            self._show_placeholder(_("No account in use"),
                                   _("Choose an account in the Nostr menu to see what you "
                                     "published."))
        elif state == LOADING:
            self._show_placeholder("", _("Looking for your notes and articles…"), busy=True)
        elif state == UNREACHABLE:
            self._show_placeholder(_("Couldn’t load what you published"),
                                   _("Check your internet connection, then try again."),
                                   action=True, symbol="alert")
        elif not self._model.items() and self._model.loading_more:
            # Reading on past pages that showed nothing (all deleted):
            # still the first read, as far as the person can tell.
            self._show_placeholder("", _("Looking for your notes and articles…"), busy=True)
        elif self._search_text and not self._rows.rowCount():
            self._show_placeholder(
                _("No Results for “{query}”").format(query=self._search_text),
                _("Check the spelling or try a new search."), symbol="search")
        elif not self._model.items() and not (self._model.has_more
                                              or self._model.more_failed):
            self._show_placeholder(_("Nothing Published Yet"),
                                   _("Notes and articles you publish appear here."),
                                   symbol="note")
        else:
            self._stack.setCurrentWidget(self._list)
            if not self._list.currentIndex().isValid():
                # The arrow keys need somewhere to start; nothing is
                # selected until the person moves.
                self._list.selectionModel().setCurrentIndex(
                    self._rows.index(0), self._list.selectionModel().SelectionFlag.NoUpdate)

    def _show_placeholder(self, title: str, body: str, *, busy: bool = False,
                          action: bool = False, symbol: str = "") -> None:
        self._empty_symbol_name = symbol
        self._paint_empty_symbol()
        self._empty_title.setText(title)
        self._empty_title.setVisible(bool(title))
        self._empty_body.setText(body)
        self._busy.setVisible(busy)
        self._empty_action.setVisible(action)
        self._stack.setCurrentWidget(self._placeholder)

    def _paint_empty_symbol(self) -> None:
        name = self._empty_symbol_name
        self._empty_symbol.setVisible(bool(name))
        if name:
            self._empty_symbol.setPixmap(post_list.symbol_pixmap(
                name, post_list.EMPTY_SYMBOL_SIZE, QColor(THEME_TOKENS[self._is_dark]["muted"]),
                self.devicePixelRatioF() or 2.0))

    def _say_what_changed(self) -> None:
        """A sentence for the host when a read began, or ended short of
        everything; "" when a refresh ended well, to clear the last one."""
        model = self._model
        was_state, was_refreshing, more_had_failed = self._last
        self._last = (model.state, model.refreshing, model.more_failed)
        if model.more_failed and not more_had_failed:
            self.status_message.emit(_("Couldn’t load more notes. Check your internet "
                                       "connection, then try again."))
            return
        if model.refreshing and not was_refreshing:
            self.status_message.emit(_("Refreshing what you published…"))
            return
        ended = ((was_refreshing and not model.refreshing)
                 or (was_state == LOADING and model.state == READY))
        if not ended:
            return
        if model.refresh_failed:
            self.status_message.emit(_("Couldn’t refresh. Check your internet connection, "
                                       "then try again."))
        elif model.partial:
            self.status_message.emit(_("Some relays didn’t answer, so something may be "
                                       "missing."))
        elif was_refreshing:
            self.status_message.emit("")

    # -- the actions ------------------------------------------------------------

    def current_item(self) -> Optional[PublishedItem]:
        return self._rows.item_at(self._list.currentIndex().row())

    def _open(self, item: PublishedItem) -> None:
        """What Return and a double click do: an article opens for
        editing, a note in its default reading app on the web."""
        if item.is_article and item.identifier:
            self.open_requested.emit(item)
        else:
            self._open_on(item, self._default_reader(item).key)

    def _default_reader(self, item: PublishedItem) -> web_readers.Reader:
        return web_readers.default_reader(web_kind(item), self._reader_choice())

    def _post(self, item: PublishedItem) -> web_readers.Post:
        """``item`` as the reading apps address it: an article with the
        Standup site it is read in (the account's curation listing it)."""
        site = self._model.site_of(item)
        return web_readers.Post(
            kind=web_kind(item), entity=item.nostr_address() or "", author=item.pubkey,
            identifier=item.identifier,
            site=web_readers.Site(site.pubkey, site.identifier) if site is not None else None)

    def _link(self, item: PublishedItem, key: str) -> str:
        return web_readers.link_for(web_readers.reader(key), self._post(item))

    def _resolve(self, item: PublishedItem, key: str, use) -> None:
        """``use(link, fallen_back)`` with where the post opens in ``key``:
        Standup's first address that is there (asked for in turn), or njump
        when none is (``fallen_back`` True); any other app's own link."""
        links = web_readers.links_for(web_readers.reader(key), self._post(item))
        if not links:
            self.status_message.emit(_("That link cannot be opened."))
            return
        if key != web_readers.DISCOVER or self._page_check is None:
            use(links[0], False)
            return
        fallback = self._link(item, web_readers.NJUMP)

        def answered(link: str, found) -> None:
            if found is False and fallback:
                use(fallback, True)
            elif found is False:
                # Not on Standup, and no Nostr address to open elsewhere.
                self.status_message.emit(_("Standup doesn’t show this post yet."))
            else:
                use(link, False)
        self._page_check(links, answered)

    def _open_on(self, item: PublishedItem, key: str) -> None:
        """Open the post in the reading app ``key``. Where Standup has no
        page for it yet, it opens on njump and the status line says why."""
        def use(link: str, fallen_back: bool) -> None:
            if fallen_back:
                self.status_message.emit(self._not_on_standup(item))
            QDesktopServices.openUrl(QUrl(link))
        self._resolve(item, key, use)

    def _copy_link(self, item: PublishedItem) -> None:
        """The default reading app's link, so a shared link opens where
        the person reads (Standup, for an article): the one that is there."""
        def use(link: str, fallen_back: bool) -> None:
            QGuiApplication.clipboard().setText(link)
            self.status_message.emit(self._not_on_standup(item, copied=True) if fallen_back
                                     else _("Link copied."))
        self._resolve(item, self._default_reader(item).key, use)

    @staticmethod
    def _not_on_standup(item: PublishedItem, *, copied: bool = False) -> str:
        if copied:
            return (_("Standup doesn’t show this curation yet, so its njump link was copied.")
                    if item.is_curation else
                    _("Standup doesn’t show this article yet, so its njump link was copied."))
        return (_("Standup doesn’t show this curation yet, so it opened on njump.")
                if item.is_curation else
                _("Standup doesn’t show this article yet, so it opened on njump."))

    def _ask_to_delete(self, item: PublishedItem) -> None:
        if self._model.deletion_state(item.id) == DELETING:
            return
        if alerts.confirm_destructive(
                self, title=_("Delete “{title}” from Nostr?").format(title=short_title(item)),
                message=_("Relays that keep it are asked to remove it. People and apps that "
                          "already saved a copy may still have it. Your draft stays."),
                action=_("Delete"), caution=True, is_dark=self._is_dark, nostr=True):
            self._model.delete(item.id)

    def _load_more(self) -> None:
        if not self._model.loading_more:
            self._model.load_more()

    # -- the preview's host ------------------------------------------------------

    def set_preview_image_loader(self, loader) -> None:
        """The app's image loader for a preview's cover (None: no covers)."""
        self._preview.set_image_loader(loader)

    def set_preview_pictures(self, lookup, arrived=None) -> None:
        """Where a preview finds the pictures in a post's text (None: frames)."""
        self._preview.set_pictures(lookup, arrived)

    def preview_record(self, key: str) -> Optional[PublishedRecord]:
        item = self._model.item(key) if key else None
        return preview_record_of(item) if item is not None else None

    def _anchor(self, index: QModelIndex) -> Tuple[str, QRect]:
        item = self._rows.item_at(index.row()) if index.isValid() else None
        if item is None:
            return "", QRect()
        rect = self._list.visualRect(index)
        row = index.data(post_list.ROW_ROLE)
        if isinstance(row, post_list.PostRow) and row.section:
            rect = rect.adjusted(0, post_list.section_height(), 0, 0)
        return item.id, QRect(self._list.viewport().mapToGlobal(rect.topLeft()), rect.size())

    def preview_row_at(self, pos) -> Tuple[str, QRect]:
        index = self._list.indexAt(pos)
        key, anchor = self._anchor(index)
        if key and pos.y() < anchor.top() - self._list.viewport().mapToGlobal(QPoint()).y():
            return "", QRect()                                   # over a section's title
        return key, anchor

    def preview_current_row(self) -> Tuple[str, QRect]:
        return self._anchor(self._list.currentIndex())

    def preview_anchor_for(self, key: str) -> QRect:
        for row in range(self._rows.rowCount()):
            item = self._rows.item_at(row)
            if item is not None and item.id == key:
                index = self._rows.index(row)
                if not self._list.viewport().rect().intersects(self._list.visualRect(index)):
                    return QRect()
                return self._anchor(index)[1]
        return QRect()

    def preview_is_available(self) -> bool:
        window = self.window()
        return self.isVisible() and window is not None and window.isActiveWindow()

    def preview_now(self) -> int:
        return int(time.time())

    def preview_actions(self, key: str) -> List[PreviewAction]:
        """The full preview's commands. An article: Open in (its reading
        apps, Standup Discover first) and Edit Article, its next step. A
        note: Open With and Open in its default app. Copy Link and Delete
        from Nostr… in the ⋯ menu, Delete last."""
        item = self._model.item(key)
        if item is None:
            return []
        actions: List[PreviewAction] = []
        first = self._default_reader(item)
        apps = tuple((f"open:{app.key}", app.name)
                     for app in web_readers.readers_for(web_kind(item)))
        has_link = bool(self._link(item, first.key))
        editable = item.is_article and bool(item.identifier)
        if has_link:
            if editable:
                actions.append(PreviewAction("open_in", _("Open in"), menu=apps))
            else:
                actions.append(PreviewAction("open_with", _("Open With"), menu=apps))
                actions.append(PreviewAction(f"open:{first.key}",
                                             _("Open in {app}").format(app=first.name),
                                             primary=True))
            actions.append(PreviewAction("copy", _("Copy Link"), more=True))
        if editable:
            actions.append(PreviewAction("edit", _("Edit Article"), primary=True))
        actions.append(PreviewAction("delete", _("Delete from Nostr…"), more=True,
                                     destructive=True))
        return actions

    def preview_act(self, key: str, action: str) -> None:
        item = self._model.item(key)
        if item is None:
            return
        if action == "edit":
            self.open_requested.emit(item)
        elif action.startswith("open:"):
            self._open_on(item, action[len("open:"):])
        elif action == "copy":
            self._copy_link(item)
        elif action == "delete":
            self._ask_to_delete(item)

    def hideEvent(self, event) -> None:
        self._preview.close(disarm=True)
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._preview.shutdown()
        super().closeEvent(event)

    def _on_activated(self, index: QModelIndex) -> None:
        item = self._rows.item_at(index.row())
        if item is not None:
            self._preview.close()
            self._open(item)

    def _on_clicked(self, index: QModelIndex) -> None:
        # Load More is a button: one click.
        if self._rows.is_more(index.row()):
            self._load_more()

    def _selected_row(self) -> int:
        """The row the keys act on: the current one, when it is selected.
        A row the keyboard merely rests on, with nothing selected (as when
        the list appears), is not acted on, as in Mail and Finder."""
        index = self._list.currentIndex()
        if not index.isValid() or not self._list.selectionModel().isSelected(index):
            return -1
        return index.row()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._list and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            row = self._selected_row()
            item = self._rows.item_at(row)
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._rows.is_more(row):
                    self._load_more()
                elif item is not None:
                    self._open(item)
                return True
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                if item is not None:
                    self._ask_to_delete(item)
                return True
        return super().eventFilter(watched, event)

    def menu_for(self, index: QModelIndex) -> Optional[QMenu]:
        """The row's menu, built but not shown (so tests can read it): Edit
        Article (an article), the default reading app, Open With (every app
        that shows it, as Finder offers apps for a file), Copy Link, and
        Delete from Nostr… last."""
        item = self._rows.item_at(index.row()) if index.isValid() else None
        if item is None:
            return None
        menu = QMenu(self)
        default = None
        if item.is_article and item.identifier:
            default = menu.addAction(_("Edit Article"))
            default.triggered.connect(lambda: self.open_requested.emit(item))
        first = self._default_reader(item)
        has_link = bool(self._link(item, first.key))
        browse = menu.addAction(_("Open in {app}").format(app=first.name))
        browse.setEnabled(has_link)
        browse.triggered.connect(lambda: self._open_on(item, first.key))
        apps = menu.addMenu(_("Open With"))
        apps.setEnabled(has_link)
        for app in web_readers.readers_for(web_kind(item)):
            entry = apps.addAction(app.name)
            entry.triggered.connect(lambda _checked=False, key=app.key: self._open_on(item, key))
        copy = menu.addAction(_("Copy Link"))
        copy.setEnabled(has_link)
        copy.triggered.connect(lambda: self._copy_link(item))
        menu.setDefaultAction(default or browse)
        menu.addSeparator()
        delete = menu.addAction(_("Delete from Nostr…"))
        # The key that does it here, in the platform's own notation.
        delete.setShortcut(QKeySequence(Qt.Key.Key_Backspace if sys.platform == "darwin"
                                        else Qt.Key.Key_Delete))
        delete.setEnabled(self._model.deletion_state(item.id) != DELETING)
        delete.triggered.connect(lambda: self._ask_to_delete(item))
        return menu

    def _show_menu(self, index: QModelIndex, point: QPoint) -> None:
        # Nothing shows over a popover (popovers.md).
        self._preview.close(disarm=True)
        menu = self.menu_for(index)
        if menu is not None:
            menu.exec(point)
            menu.deleteLater()

    # -- deletions that came back -----------------------------------------------

    def _on_deletion_status(self, _item: PublishedItem, text: str) -> None:
        self.status_message.emit(text)

    def _on_deleted(self, item: PublishedItem, results: list) -> None:
        accepted = sum(1 for _url, ok, _message in results if ok)
        self.status_message.emit(ngettext(
            "{accepted} of {total} relay accepted the request to delete “{title}”.",
            "{accepted} of {total} relays accepted the request to delete “{title}”.",
            len(results)).format(accepted=accepted, total=len(results),
                                 title=short_title(item)))

    def _on_not_taken(self, item: PublishedItem, results: list) -> None:
        details = "\n".join(f"{url}: {message or _('no answer')}"
                            for url, _ok, message in results)
        if any(refused_by_relay(message) for _url, _ok, message in results):
            message = _("The relays turned the request down. Show Details says why; you can "
                        "try again later.")
        else:
            message = _("No relay accepted the request. Check your internet connection, "
                        "then try again.")
        self._offer_try_again(item, message, details)

    def _on_refused(self, item: PublishedItem, reason: str) -> None:
        if is_signer_silent(reason):
            self._offer_try_again(item, humanize_failure(reason), "")
        else:
            self._offer_try_again(item, _("Your signer app didn’t approve the request. Try "
                                          "again, and approve it when your signer app "
                                          "asks."), reason)

    def _offer_try_again(self, item: PublishedItem, message: str, details: str) -> None:
        again = alerts.ask(
            self, title=_("Couldn’t delete “{title}”").format(title=short_title(item)),
            message=message, details=details, is_dark=self._is_dark,
            buttons=(alerts.Button(_("Cancel"), False, alerts.CANCEL),
                     alerts.Button(_("Try Again"), True, alerts.DEFAULT)), nostr=True)
        if again is True:
            self._model.try_again(item.id)
        else:
            self._model.dismiss(item.id)
