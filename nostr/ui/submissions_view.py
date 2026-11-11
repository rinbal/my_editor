# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einreichungen: the third view of the Drafts panel, beside Entwürfe
and Veröffentlicht (plan 10.1, 10.4; the prototype's screens 14, 31, 32).

    Du bist dran
    [cover] Ein Jahr mit Lightning im Café            2 Std.
            Änderungen erbeten · Einundzwanzig Magazin     ●
    Läuft
    [ ▤ ]   Steuern auf Sats, einfach erklärt         3 Tg.
            Eingereicht · Bitcoin-Wiki
    › Erledigt (1)

The rows are :class:`~.standup_following.Following`'s, in three
sections: Du bist dran, Läuft, and Erledigt, which is closed until it
is opened and says how many it holds. A row has the cover (the kind's
symbol until a version was opened here), the title on up to two lines,
one status word with the publication (the writer's name first on a work
the person only takes part in; the newest message while it is unread),
its age at the trailing edge and a blue dot when something is unread,
"Neu" to a screen reader (drawn by :mod:`.submission_rows`). A row opens
the detail in the same panel (:mod:`.submission_detail`); ‹ Einreichungen
comes back to the row.

Nothing here knows the protocol: the words come from the one table
(``nostr/standup/following.py``). When the status could not be loaded
after the quiet tries, one line under the list says so once, with Try
Again (lessons 36); a list that could not be read, no account, and an
empty list each have their own placeholder, the empty one with the way
to submit.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, List, Optional

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListView, QMenu, QPushButton,
    QStackedLayout, QVBoxLayout, QWidget,
)

import announce
from i18n import N_, _

from ..standup import following as rules
from ..standup import thread as threads
from . import appearance, post_list
from .drafts_common import LIST_GUTTER, MIN_CONTROL_PX, THEME_TOKENS, secondary_font
from .submission_detail import SubmissionDetail
from .submission_rows import ITEM_ROLE, Item, RowDelegate, Rows

PANEL_VIEW = "submissions"


EMPTY_BODY = N_("Follow here what you submit to publications. Open an article and choose "
                "Nostr › Submit to a Publication….")


def add_to_panel(panel, view: "SubmissionsView", *, server: str) -> bool:
    """Make ``view`` the Drafts panel's third view, Einreichungen, when a
    server is set (RG-M3-b 4): without one nothing can be submitted, and
    a view that could only say so is left out. Whether it was added."""
    if not server:
        return False
    panel.add_view(PANEL_VIEW, _("Submissions"), _("What you submitted to publications"), view,
                   refresh=view.refresh, refresh_name=_("Refresh Submissions"),
                   search=view.set_search_text, search_name=_("Search Submissions"))
    view.detail_shown.connect(lambda shown: panel.set_view_focused(PANEL_VIEW, shown))
    return True


class SubmissionsView(QWidget):
    """See the module docstring. ``name_of(pubkey)`` is a person's name
    (``""`` when not known), ``avatar_of(pubkey)`` their picture.

    Signals:
      action_requested(str, object)   a sid and the primary Action chosen
      open_in_editor(str, int)        a sid and a version (0: the text worked on)
      compare_requested(str, int)     a sid and the version to compare with the one before
      submit_requested()              the empty list's button
    """

    action_requested = Signal(str, object)
    open_in_editor = Signal(str, int)
    compare_requested = Signal(str, int)
    submit_requested = Signal()
    detail_shown = Signal(bool)             # a detail, or the list, is shown now

    def __init__(self, *, name_of: Callable[[str], str],
                 avatar_of: Callable[[str], Optional[QPixmap]], is_dark: bool = True,
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("submissions_view")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._name_of, self._clock, self._is_dark = name_of, clock, bool(is_dark)
        self._following = None
        self._signed_in = False
        self._server = True
        self._done_open = False
        self._search = ""
        self._said = ""
        self._rows = Rows(self)
        self._build(avatar_of)
        self.apply_theme(self._is_dark)
        self._reload()

    # -- building ----------------------------------------------------------------

    def _build(self, avatar_of) -> None:
        self._stack = QStackedLayout(self)
        self._stack.currentChanged.connect(lambda _i: self.detail_shown.emit(self.showing_detail()))
        self._stack.setContentsMargins(0, 0, 0, 0)
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.list = QListView()
        self.list.setObjectName("submissions_list")
        self.list.setAccessibleName(_("Submissions"))
        self.list.setModel(self._rows)
        self._delegate = RowDelegate(self.list, is_dark=self._is_dark)
        self.list.setItemDelegate(self._delegate)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(False)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_menu)
        self.list.activated.connect(self._on_activated)
        self.list.clicked.connect(self._on_clicked)
        column.addWidget(self.list, 1)
        # One quiet line, in place, when the status could not be loaded.
        self.problem_bar = QFrame()
        self.problem_bar.setObjectName("submissions_problem")
        bar = QHBoxLayout(self.problem_bar)
        bar.setContentsMargins(LIST_GUTTER, 6, LIST_GUTTER, 6)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setFont(secondary_font())
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.try_again = QPushButton(_("Try Again"))
        self.try_again.clicked.connect(self.refresh)
        bar.addWidget(self.problem, 1)
        bar.addWidget(self.try_again)
        self.problem_bar.hide()
        column.addWidget(self.problem_bar)
        self._stack.addWidget(page)
        self._list_page = page

        self._placeholder = QWidget()
        empty = QVBoxLayout(self._placeholder)
        empty.setContentsMargins(LIST_GUTTER, 24, LIST_GUTTER, 24)
        empty.setSpacing(8)
        empty.addStretch(1)
        self._empty_symbol = QLabel()
        self._empty_symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self._empty_symbol)
        self.empty_title = QLabel()
        self.empty_title.setObjectName("submissions_empty_title")
        self.empty_body = QLabel()
        self.empty_body.setObjectName("submissions_empty_body")
        self.empty_body.setFont(secondary_font())
        for label in (self.empty_title, self.empty_body):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            empty.addWidget(label)
        self.empty_action = QPushButton(_("Try Again"))
        self.empty_action.setObjectName("submissions_empty_action")
        self.empty_action.setMinimumHeight(MIN_CONTROL_PX)
        self.empty_action.clicked.connect(self._on_empty_action)
        empty.addWidget(self.empty_action, 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addStretch(2)
        self._empty_handler: Callable[[], None] = lambda: None
        self._stack.addWidget(self._placeholder)

        self.detail = SubmissionDetail(name_of=self._name_of, avatar_of=avatar_of,
                                       is_dark=self._is_dark)
        self.detail.back_requested.connect(self.show_list)
        self.detail.action_requested.connect(self.action_requested)
        self.detail.open_in_editor.connect(self.open_in_editor)
        self.detail.compare_requested.connect(self.compare_requested)
        self._stack.addWidget(self.detail)

    # -- the host's interface ------------------------------------------------------

    def set_following(self, following, *, signed_in: bool, server: bool = True) -> None:
        """The account whose submissions are shown (``following`` None
        while none is in use, or none could be followed): everything of
        the account before goes at once (criterion 19)."""
        if following is self._following and signed_in == self._signed_in:
            return
        before, self._following = self._following, following
        if before is not None:
            before.changed.disconnect(self._reload)
        self.detail.clear()
        self._signed_in, self._server = signed_in, server
        self._done_open = False
        if following is not None:
            following.changed.connect(self._reload)
        self._reload()

    def set_handled(self, actions: Iterable[rules.Action]) -> None:
        self.detail.set_handled(actions)

    def set_edited(self, edited: Callable[[rules.Row], bool]) -> None:
        self.detail.set_edited(edited)

    def set_search_text(self, text: str) -> None:
        self._search = text.strip().casefold()
        self._reload()

    def refresh(self) -> None:
        if self._following is not None:
            self._following.refresh()

    def opened(self) -> None:
        """The view came into sight: look now (plan 10.4)."""
        if self._following is not None:
            self._following.view_opened()

    def show_detail(self, sid: str) -> None:
        if self._following is None or self._following.row(sid) is None:
            return
        self.detail.show_submission(self._following, sid)
        self._stack.setCurrentWidget(self.detail)

    def show_list(self) -> None:
        """‹ Einreichungen: the list again, the focus on the row left."""
        sid = self.detail.sid
        self.detail.clear()
        self._reload()
        row = self._rows.row_of(sid)
        if row >= 0:
            self.list.setCurrentIndex(self._rows.index(row))
        if self._stack.currentWidget() is self._list_page:
            self.list.setFocus()

    def showing_detail(self) -> bool:
        return self._stack.currentWidget() is self.detail

    def items(self) -> List[Item]:
        return list(self._rows.items)

    # -- what is shown -------------------------------------------------------------

    def _item(self, row: rules.Row) -> Item:
        following = self._following
        publication = following.publication_name(row.publication)
        rest, short = publication, ""
        if row.role != "writer":
            rest = " · ".join(part for part in (self._name_of(row.writer), publication)
                              if part)
        if row.unread:
            sender, role, text = following.snippet(row.sid)
            if text:
                label = threads.sender_label(role, self._name_of(sender))
                rest, short = (f"{label}: {text}" if label else text), text
        return Item(
            "row", row.title or _("Untitled"), sid=row.sid,
            word=rules.status_word(row, self._name_of), rest=rest, short=short,
            age=post_list.list_date(row.updated_at, int(self._clock())), unread=row.unread,
            turn=row.turn, work=row.kind, cover=following.cover(row.sid),
            quiet=row.situation in (rules.Situation.UNSENT, rules.Situation.UNCONFIRMED,
                                    rules.Situation.SENDING, rules.Situation.NOT_HANDED_IN))

    def _reload(self) -> None:
        if self.showing_detail() and self._following is not None \
                and self._following.row(self.detail.sid) is not None:
            return                                  # the detail follows on its own
        following = self._following
        rows = following.rows() if following is not None else []
        if self._search:
            rows = [row for row in rows if self._search in " ".join((
                row.title, following.publication_name(row.publication))).casefold()]
        items: List[Item] = []
        for section, title in ((rules.Section.TURN, _("Your Turn")),
                               (rules.Section.RUNNING, _("In Progress"))):
            mine = [row for row in rows if row.section is section]
            if mine:
                items.append(Item("header", title))
                items += [self._item(row) for row in mine]
        done = [row for row in rows if row.section is rules.Section.DONE]
        if done:
            items.append(Item("done", _("Done ({count})").format(count=len(done)),
                              open=self._done_open))
            if self._done_open:
                items += [self._item(row) for row in done]
        current = self.list.currentIndex()
        keep = self._rows.items[current.row()] if current.isValid() \
            and current.row() < len(self._rows.items) else None
        self._rows.show(items)
        if keep is not None:
            row = next((i for i, item in enumerate(items)
                        if (item.kind, item.sid) == (keep.kind, keep.sid)), -1)
            if row >= 0:
                self.list.setCurrentIndex(self._rows.index(row))
        self._show_state(bool(rows))

    def _show_state(self, any_rows: bool) -> None:
        following = self._following
        problem = _(following.problem) if following is not None and following.problem else ""
        self.problem.setText(_("The current status couldn’t be loaded. {cause}").format(
            cause=problem) if problem else "")
        self.problem_bar.setVisible(bool(problem) and any_rows)
        if problem and problem != self._said and self.isVisible():
            announce.announce(self, self.problem.text())
        self._said = problem
        if not self._signed_in:
            self._placeholder_of(_("No account in use"),
                                 _("Choose an account in the Nostr menu to see what you "
                                   "submitted."))
        elif not self._server or following is None:
            # Not in the panel without a server; a moment between accounts.
            self._placeholder_of(_("No Submissions Yet"), "", symbol="article")
        elif following.list_problem:
            self._placeholder_of(_("Your submissions couldn’t be loaded"),
                                 following.list_problem, symbol="alert",
                                 action=(_("Try Again"), following.reload))
        elif self._search and not any_rows:
            self._placeholder_of(_("No Results for “{query}”").format(query=self._search),
                                 _("Check the spelling or try a new search."), symbol="search")
        elif not any_rows and problem:
            self._placeholder_of(_("Your submissions couldn’t be loaded"), problem,
                                 symbol="alert", action=(_("Try Again"), self.refresh))
        elif not any_rows and not following.listed:
            self._placeholder_of(_("Loading …"), "", symbol="article")     # not empty yet
        elif not any_rows:
            self._placeholder_of(
                _("No Submissions Yet"), _(EMPTY_BODY), symbol="article",
                action=(_("Submit to a Publication…"), self.submit_requested.emit))
        else:
            self._stack.setCurrentWidget(self._list_page)

    def _placeholder_of(self, title: str, body: str, *, symbol: str = "",
                        action: Optional[tuple] = None) -> None:
        self._symbol_name = symbol
        self._paint_symbol()
        self.empty_title.setText(title)
        self.empty_body.setText(body)
        self.empty_action.setVisible(action is not None)
        if action is not None:
            self.empty_action.setText(action[0])
            self._empty_handler = action[1]
        self._stack.setCurrentWidget(self._placeholder)

    def _paint_symbol(self) -> None:
        name = getattr(self, "_symbol_name", "")
        self._empty_symbol.setVisible(bool(name))
        if name:
            self._empty_symbol.setPixmap(post_list.symbol_pixmap(
                name, post_list.EMPTY_SYMBOL_SIZE, QColor(THEME_TOKENS[self._is_dark]["muted"]),
                self.devicePixelRatioF() or 2.0))

    def _on_empty_action(self) -> None:
        self._empty_handler()

    # -- what the person does ------------------------------------------------------

    def _on_clicked(self, index: QModelIndex) -> None:
        self._on_activated(index)

    def _on_activated(self, index: QModelIndex) -> None:
        item = index.data(ITEM_ROLE)
        if not isinstance(item, Item):
            return
        if item.kind == "done":
            self._done_open = not self._done_open
            self._reload()
            announce.announce(self.list, _("Done, expanded") if self._done_open
                              else _("Done, collapsed"))
        elif item.kind == "row":
            self.show_detail(item.sid)

    def _on_menu(self, pos) -> None:
        index = self.list.indexAt(pos)
        item = index.data(ITEM_ROLE) if index.isValid() else None
        row = self._following.row(item.sid) if isinstance(item, Item) and item.sid \
            and self._following is not None else None
        if row is None:
            return
        menu = QMenu(self)
        menu.addAction(_("Open"), lambda: self.show_detail(row.sid))
        if row.role == "writer":
            menu.addAction(_("Open in Editor"), lambda: self.open_in_editor.emit(row.sid, 0))
        if row.situation is rules.Situation.UNSENT:
            menu.addSeparator()
            menu.addAction(_("Send Again"), lambda: self.action_requested.emit(
                row.sid, rules.Action.RESEND))
            menu.addAction(_("Discard"), lambda: self._following.discard(
                row.sid, lambda _stored: None))
        self._menu = menu
        menu.popup(self.list.viewport().mapToGlobal(pos))

    def showEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().showEvent(event)
        self.opened()

    # -- appearance ------------------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        appearance.bind(self)
        self.setPalette(appearance.palette(is_dark))
        self._is_dark = bool(is_dark)
        t = THEME_TOKENS[self._is_dark]
        self.setStyleSheet(f"""
            QWidget#submissions_view {{ background: {t["panel_bg"]}; }}
            QListView#submissions_list {{ background: {t["panel_bg"]}; border: none; }}
            QLabel#submissions_empty_title {{ color: {t["chrome_fg"]}; font-weight: 600; }}
            QLabel#submissions_empty_body {{ color: {t["muted"]}; }}
            QFrame#submissions_problem {{ border-top: 1px solid {t["border"]}; }}
            QFrame#submissions_problem QLabel {{ color: {t["muted"]}; }}
            QPushButton {{ background: transparent; color: {t["row_fg"]};
                border: 1px solid {t["border"]}; border-radius: 4px; padding: 4px 12px; }}
            QPushButton:hover {{ background: {t["hover_bg"]}; }}
            QPushButton:focus {{ border-color: {t["accent"]}; }}
        """)
        self._delegate.set_dark(self._is_dark)
        self.detail.apply_theme(self._is_dark)
        self._paint_symbol()


__all__ = ["ITEM_ROLE", "Item", "PANEL_VIEW", "SubmissionsView", "add_to_panel"]
