# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A submission's detail, in the Drafts panel (plan 10.1 D35, 10.4; the
prototype's screens 16, 20, 30).

    ‹ Einreichungen                         (i) (…)
    [cover] Ein Jahr mit Lightning im Café
            [21] Einundzwanzig Magazin
    (P) Paul Berg
    Anna möchte Änderungen.
    Was genau, steht unten im Gespräch.
    [ Im Editor überarbeiten ]
    (A)(T) Anna und die Redaktion von …   🔒 Ende-zu-Ende verschlüsselt
    … the conversation …
    ┌ Nachricht an die Redaktion ───────┐ (↑)

One status sentence with a quieter second line and at most one primary
action, all from the one table (``nostr/standup/following.py``): the
button shows only for an action the window handles (``handled``), so
publishing and confirming, which a later phase adds, add a handler and
nothing here. Info holds the publication, who publishes, the version,
who is responsible, who takes part, the privacy lines and the history;
the menu holds Im Editor öffnen and Zurückziehen …, which asks first
with Cancel as the default.

The header, the sentence and the action stay where they are; only the
conversation (with Info above it) scrolls (RG-M3-b 2). It opens at the
newest message and stays at the end while it grows, after sending too,
unless the person scrolled up to read.

The composer stays at the bottom, one line that grows with the text
(up to :data:`COMPOSER_LINES`): Return sends, Shift-Return starts a new
line, which a quiet hint says while it has the focus (RG-M3-b 9). What cannot be sent as it is (no text, a secret key) is said
above it and the text is kept. The widget shows what
:class:`~.standup_following.Following` knows and forwards the person's
choices; it holds no state of the submission itself.
"""

from __future__ import annotations

from html import escape
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QDateTime, QEvent, QLocale, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QToolButton,
    QVBoxLayout, QWidget,
)

import announce
from alerts import ask_try_again, confirm_destructive
from i18n import N_, _, pgettext

from .. import failures
from ..failures import markup
from ..standup import following as rules, formats, thread as threads
from ..standup.errors import Code, cause_words
from . import appearance
from .drafts_common import (
    LIST_GUTTER, MIN_CONTROL_PX, display_locale, plain_tip, secondary_font,
)
from .flow_layout import FlowLayout
from .post_list import symbol_pixmap
from .standup_publications import publication_logo
from .submission_cover import cover_pixmap
from .submission_thread import ThreadView, discard, face, more_about_button

_COVER = 64
COMPOSER_LINES = 5
SEND_TIP = N_("Send (↩)")
NEW_LINE_HINT = N_("⇧↩ for a new line")
NOT_OPENED = N_("This version isn’t confirmed and is not opened.")
NOT_LOADED = N_("This submission couldn’t be loaded")
NOT_WITHDRAWN = N_("The submission wasn’t withdrawn")
DECIDED_MEANWHILE = N_("It wasn’t withdrawn: {publication} decided on it in the meantime.")
# Why a withdrawal did not go, by its cause (RG-M3-a 7); any other is
# the connection's or the server's (cause_words).
_WITHDRAW_CAUSES = {Code.SIGNER_DECLINED: failures.SIGNER_DECLINED,
                    Code.SIGNER_UNREACHABLE: failures.SIGNER_SILENT}
PRIVACY = (
    N_("Only you, the others taking part and the editorial team of {publication} can read "
       "it."),
    N_("The server sees the title, the status and who takes part, never the content."),
    N_("The pictures stay encrypted on your media server until the article is published."),
    N_("What you have read is kept in step across your devices, encrypted."),
)


class SubmissionDetail(QWidget):
    """See the module docstring.

    Signals:
      back_requested()                 ‹ Einreichungen
      action_requested(str, object)    the sid and the primary Action
      open_in_editor(str, int)         the sid and a version (0: the text worked on)
      compare_requested(str, int)      the sid and the version to compare with the one before
      remind_requested(str, str)       the sid and whom to remind (one button each)
    """

    back_requested = Signal()
    action_requested = Signal(str, object)
    open_in_editor = Signal(str, int)
    compare_requested = Signal(str, int)
    remind_requested = Signal(str, str)

    def __init__(self, *, name_of: Callable[[str], str],
                 avatar_of: Callable[[str], Optional[QPixmap]], is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("submission_detail")
        self._name_of, self._avatar_of, self._is_dark = name_of, avatar_of, is_dark
        self._following = None
        self._sid = ""
        self._handled: frozenset = frozenset()
        self._edited: Callable[[rules.Row], bool] = lambda _row: False
        self._said = ""
        self._line = ""                         # what an action said in place
        self._shown_row: tuple = ()             # the row and the publication's name shown
        self._build()
        self.apply_theme(is_dark)

    # -- building ----------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(LIST_GUTTER - 4, 6, LIST_GUTTER - 4, 4)
        self.back = QToolButton()
        self.back.setText("‹ " + _("Submissions"))
        self.back.setAccessibleName(_("Back to Submissions"))
        self.back.setAutoRaise(True)
        self.back.clicked.connect(self.back_requested)
        self.info_button = QToolButton()
        self.info_button.setCheckable(True)
        self.info_button.setAutoRaise(True)
        self.info_button.setAccessibleName(_("Info"))
        self.info_button.setToolTip(_("Info"))
        self.info_button.toggled.connect(self._show_info)
        self.more_button = QToolButton()
        self.more_button.setAutoRaise(True)
        self.more_button.setAccessibleName(_("More"))
        self.more_button.setToolTip(_("More"))
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self.more_button)
        self.open_action = self.menu.addAction(
            _("Open in Editor"), lambda: self.open_in_editor.emit(self._sid, 0))
        self.menu.addSeparator()
        self.withdraw_action = self.menu.addAction(_("Withdraw …"), self._ask_withdraw)
        self.more_button.setMenu(self.menu)
        top.addWidget(self.back)
        top.addStretch(1)
        top.addWidget(self.info_button)
        top.addWidget(self.more_button)
        root.addLayout(top)

        # Pinned: the header, the sentence and the action.
        pinned = QWidget()
        pinned.setObjectName("submission_detail_content")
        column = QVBoxLayout(pinned)
        column.setContentsMargins(LIST_GUTTER, 4, LIST_GUTTER, 6)
        column.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.cover = QLabel()
        self.cover.setFixedSize(_COVER, _COVER)
        head.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.title = QLabel()
        self.title.setObjectName("submission_title")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.logo = QLabel()
        self.publication = QLabel()
        self.publication.setTextFormat(Qt.TextFormat.PlainText)
        self.publication.setFont(secondary_font())
        named = QHBoxLayout()
        named.setSpacing(5)
        named.addWidget(self.logo)
        named.addWidget(self.publication, 1)
        words.addWidget(self.title)
        words.addLayout(named)
        words.addStretch(1)
        head.addLayout(words, 1)
        column.addLayout(head)
        self._pills = QWidget()
        self._pill_row = FlowLayout(self._pills, spacing=6)
        column.addWidget(self._pills)

        self.status = QFrame()
        self.status.setObjectName("submission_status")
        block = QVBoxLayout(self.status)
        block.setContentsMargins(0, 2, 0, 2)
        block.setSpacing(4)
        self.sentence = QLabel()
        self.sentence.setObjectName("submission_sentence")
        self.more = QLabel()
        self.more.setObjectName("submission_more")
        self.note = QLabel()
        self.note.setObjectName("submission_more")
        # "Mehr dazu" sits on the sentence's line: it is about the
        # sentence, not about the line under it (RG-M3-b 12).
        said = QHBoxLayout()
        said.setSpacing(6)
        said.addWidget(self.sentence, 1)
        self._more_about = QHBoxLayout()
        said.addLayout(self._more_about)
        block.addLayout(said)
        for label in (self.sentence, self.more, self.note):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            if label is not self.sentence:
                block.addWidget(label)
        self.note.setTextFormat(Qt.TextFormat.RichText)
        # The buttons wrap into rows (RG-M4-c 4): a named Erinnern for each
        # person, German words or a narrow pane never clip one.
        button_row = QWidget()
        buttons = self._buttons = FlowLayout(button_row, spacing=6)
        # Named from the start: no unnamed button waits hidden.
        self.action = QPushButton(_("Try Again"))
        self.action.setObjectName("submission_action")
        self.action.setMinimumHeight(MIN_CONTROL_PX)
        self.action.clicked.connect(self._on_action)
        self.second = QPushButton(_("Discard"))
        self.second.setMinimumHeight(MIN_CONTROL_PX)
        self.second.clicked.connect(self._on_second)
        buttons.addWidget(self.action)
        # Erinnern: one named button for each person still to confirm,
        # between the action and the second button.
        self._reminders: list = []
        buttons.addWidget(self.second)
        block.addWidget(button_row)
        column.addWidget(self.status)
        root.addWidget(pinned)

        # Scrolled: Info, who reads, and the conversation.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._stick = True                      # at the end, and kept there as it grows
        bar = self._scroll.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_range)
        content = QWidget()
        content.setObjectName("submission_detail_content")
        column = QVBoxLayout(content)
        column.setContentsMargins(LIST_GUTTER, 2, LIST_GUTTER, 8)
        column.setSpacing(8)

        self.info = QLabel()
        self.info.setObjectName("submission_info")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.hide()
        column.addWidget(self.info)

        who = QHBoxLayout()
        who.setSpacing(6)
        self.faces = QLabel()
        self.readers = QLabel()
        self.readers.setObjectName("submission_readers")
        self.readers.setWordWrap(True)
        self.readers.setTextFormat(Qt.TextFormat.PlainText)
        self.readers.setFont(secondary_font())
        who.addWidget(self.faces)
        who.addWidget(self.readers, 1)
        column.addLayout(who)
        self.encrypted = QLabel(_("End-to-end encrypted"))
        self.encrypted.setObjectName("submission_more")
        self.encrypted.setFont(secondary_font())
        column.addWidget(self.encrypted)
        self.conversation = ThreadView(name_of=self._name_of, avatar_of=self._avatar_of,
                                 is_dark=self._is_dark)
        self.conversation.reacted.connect(self._on_reacted)
        self.conversation.retry.connect(lambda key: self._following and self._following.talk.retry(key))
        self.conversation.open_version.connect(lambda n: self.open_in_editor.emit(self._sid, n))
        self.conversation.compare_version.connect(lambda n: self.compare_requested.emit(self._sid, n))
        column.addWidget(self.conversation, 1)
        self._scroll.setWidget(content)
        root.addWidget(self._scroll, 1)

        self.composer_bar = QFrame()
        self.composer_bar.setObjectName("submission_composer")
        bar = QVBoxLayout(self.composer_bar)
        bar.setContentsMargins(LIST_GUTTER, 6, LIST_GUTTER, 8)
        bar.setSpacing(4)
        self.problem = QLabel()
        self.problem.setObjectName("submission_problem")
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setFont(secondary_font())
        self.problem.hide()
        bar.addWidget(self.problem)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.composer = QPlainTextEdit()
        self.composer.setPlaceholderText(_("Message to the editorial team"))
        self.composer.setAccessibleName(_("Message to the editorial team"))
        self.composer.setTabChangesFocus(True)
        self.composer.installEventFilter(self)
        self.composer.textChanged.connect(self._on_typed)
        self.composer.textChanged.connect(self._fit_composer)
        self.send = QToolButton()
        self.send.setObjectName("submission_send")
        self.send.setFixedSize(MIN_CONTROL_PX, MIN_CONTROL_PX)
        self.send.setText("↑")
        self.send.setAccessibleName(_("Send"))
        self.send.setToolTip(_(SEND_TIP))
        self.send.setEnabled(False)
        self.send.clicked.connect(self._send)
        row.addWidget(self.composer, 1)
        row.addWidget(self.send, 0, Qt.AlignmentFlag.AlignBottom)
        bar.addLayout(row)
        self.hint = QLabel(_(NEW_LINE_HINT))
        self.hint.setObjectName("submission_more")
        self.hint.setFont(secondary_font())
        self.hint.hide()                            # while the composer has the focus
        bar.addWidget(self.hint)
        root.addWidget(self.composer_bar)
        self._fit_composer()

    # -- the host's interface ------------------------------------------------------

    def set_handled(self, actions: Iterable[rules.Action]) -> None:
        """The primary actions the window handles: only they get a button."""
        self._handled = frozenset(actions)

    def set_edited(self, edited: Callable[[rules.Row], bool]) -> None:
        """``edited(row)``: the text in the editor differs from the
        version handed in."""
        self._edited = edited

    @property
    def sid(self) -> str:
        return self._sid

    def show_submission(self, following, sid: str) -> None:
        self.clear()
        self._following, self._sid = following, sid
        following.detail_changed.connect(self._on_changed)
        following.changed.connect(self._on_rows_changed)
        following.open(sid)
        self._stick = True                      # it opens at the newest message
        self.refresh()
        self.back.setFocus()
        self._to_the_end()

    def clear(self) -> None:
        """Nothing of a submission is shown or followed any more (the
        account was let go, or the list is shown again)."""
        following, self._following = self._following, None
        if following is not None:
            following.detail_changed.disconnect(self._on_changed)
            following.changed.disconnect(self._on_rows_changed)
        self._sid = ""
        self._said = ""
        self._line = ""
        self._shown_row = ()
        self.composer.clear()
        self.problem.hide()
        self.info_button.setChecked(False)

    def say(self, text: str) -> None:
        """One line in place, under the sentence (an action that could
        not go on says why there), until the next action."""
        self._line = text
        self.note.setText(escape(text))
        self.note.show()
        announce.announce(self.note, text)

    def _on_changed(self, sid: str) -> None:
        if sid == self._sid:
            self.refresh()

    def _on_rows_changed(self) -> None:
        """Another submission's news is not this detail's: it is drawn
        again only when its own row changed (RG-M3-a 4)."""
        following = self._following
        row = following.row(self._sid) if following is not None and self._sid else None
        if row is None or (row, following.publication_name(row.publication)) != self._shown_row:
            self.refresh()

    def detail(self):
        if self._following is None or not self._sid:
            return None
        return self._following.detail(self._sid)

    # -- showing -------------------------------------------------------------------

    def refresh(self) -> None:
        detail = self.detail()
        if detail is None:
            if self._sid:
                self.back_requested.emit()      # it is gone (discarded, the account left)
            return
        row, r = detail.row, appearance.roles(self._is_dark)
        self._shown_row = (row, detail.publication)
        self.title.setText(row.title)
        self.publication.setText(detail.publication)
        self.logo.setPixmap(self._logo(row.publication, detail.publication, 16))
        self.cover.setPixmap(cover_pixmap(self._following.cover(self._sid), row.kind, _COVER,
                                          QColor(r.soft), QColor(r.icon)))
        self._show_pills(row)
        self._show_status(detail, r)
        self._show_readers(detail)
        self._fill_info(detail)
        self.withdraw_action.setEnabled(row.open)
        # Not with the publication (unsent, elsewhere, not handed in): no
        # conversation to write into, nobody reading, no version to open
        # that would only refuse (RG-M3-b 3).
        self.open_action.setVisible(row.confirmed)
        self.more_button.setVisible(row.confirmed or row.open)
        self.faces.setVisible(row.confirmed)
        self.readers.setVisible(row.confirmed)
        self.encrypted.setVisible(row.confirmed)
        self.conversation.publication_face = lambda size: self._logo(
            row.publication, detail.publication, size)
        self.conversation.show_thread(detail, self._following.me)
        self.composer_bar.setVisible(detail.can_write and row.confirmed)
        self._order_tabs()

    def _order_tabs(self) -> None:
        """Tab goes as the eye does, after every rebuild (RG-M3-b 11): the
        top, the action, the conversation, then the composer."""
        about = [self._more_about.itemAt(i).widget() for i in range(self._more_about.count())]
        chain = [self.back, self.info_button, self.more_button, self.action,
                 *self.reminder_buttons(), self.second] \
            + [w for w in about if w is not None] + self.conversation.focusables() \
            + [self.composer, self.send]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def _logo(self, address: str, name: str, size: int) -> QPixmap:
        try:
            pubkey = formats.parse_address(address)[0]
        except formats.FormatError:
            pubkey = ""
        return publication_logo(name, pubkey, self._avatar_of(pubkey) if pubkey else None, size)

    def _show_pills(self, row: rules.Row) -> None:
        while self._pill_row.count():
            discard(self._pill_row.takeAt(0).widget())
        people = list(row.view.participants) if row.view is not None else []
        if row.role != "writer" and row.writer:
            people.insert(0, row.writer)
        names = [self._name_of(p) or _("Someone") for p in people]
        self._pills.setVisible(bool(people))
        if len(people) >= 4:
            # From four people on, one pill (plan 10.1 D32).
            text = _("{first}, {second} and {count} more").format(
                first=names[0], second=names[1], count=len(people) - 2)
            self._pill_row.addWidget(self._pill(text, None, ", ".join(names)))
            return
        for pubkey, name in zip(people, names):
            self._pill_row.addWidget(self._pill(
                name, QIcon(face(pubkey, name, self._avatar_of(pubkey), 18)), name))

    @staticmethod
    def _pill(text: str, icon: Optional[QIcon], spoken: str) -> QToolButton:
        pill = QToolButton()
        pill.setObjectName("submission_pill")
        pill.setText(text)
        pill.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        if icon is not None:
            pill.setIcon(icon)
            pill.setIconSize(QSize(18, 18))
        pill.setAccessibleName(spoken)
        pill.setToolTip(plain_tip(spoken))
        return pill

    def _show_status(self, detail, r) -> None:
        row = detail.row
        status = rules.status_of(row, publication=detail.publication, name_of=self._name_of,
                                 decision=detail.decision, complete=detail.complete,
                                 edited=self._edited(row), section=detail.section)
        self._status = status
        self.sentence.setText(status.sentence)
        self.more.setText(status.more)
        self.more.setVisible(bool(status.more))
        note = ""
        if detail.problem:
            note = markup(_(NOT_LOADED), _(detail.problem))
        elif self._line:
            note = escape(self._line)
        elif detail.not_confirmed:
            note = _(NOT_OPENED)
        elif detail.loading and not detail.thread:
            note = _("Loading …")
        self.note.setText(note)
        self.note.setVisible(bool(note))
        while self._more_about.count():
            discard(self._more_about.takeAt(0).widget())
        if status.unsigned:
            self._more_about.addWidget(more_about_button(_(rules.NOT_SIGNED), r), 0,
                                       Qt.AlignmentFlag.AlignTop)
        action = status.action
        if detail.problem:
            self.action.setText(_("Try Again"))
        elif action is rules.Action.RELOAD:
            self.action.setText(pgettext("submissions list", "Reload"))
        elif action is not None:
            self.action.setText(_(rules.ACTION_LABELS[action]))
        reminding = not detail.problem and action is rules.Action.REMIND
        self._show_reminders(row.sid, status.remind if reminding
                             and action in self._handled else ())
        self.action.setVisible(bool(detail.problem) or action is rules.Action.RELOAD
                               or (action in self._handled and not reminding))
        quiet = "submission_offered" if status.offered else "submission_action"
        if self.action.objectName() != quiet:
            self.action.setObjectName(quiet)    # offered: a plain button, not the accent
            self.action.style().unpolish(self.action)
            self.action.style().polish(self.action)
        self.second.setText(_("Discard"))
        self.second.setVisible(row.situation is rules.Situation.UNSENT)
        said = " ".join(part for part in (status.sentence, status.more) if part)
        self.status.setAccessibleName(said)
        if said != self._said:
            self._said = said
            if self.isVisible():
                announce.announce(self.status, said)

    def _show_reminders(self, sid: str, people) -> None:
        """"<Name> erinnern" for each of ``people`` not reminded in the last
        24 hours (RG-M4-b 11, as the web): a reminded person's goes."""
        for button in self._reminders:
            self._buttons.removeWidget(button)
            discard(button)
        self._reminders = []
        self._buttons.removeWidget(self.second)
        for pubkey in people:
            if self._following.reminded(sid, pubkey):
                continue
            name = self._name_of(pubkey)
            button = QPushButton(_("Remind {name}").format(name=name) if name
                                 else _(rules.ACTION_LABELS[rules.Action.REMIND]))
            button.setObjectName("submission_offered")
            button.setMinimumHeight(MIN_CONTROL_PX)
            button.clicked.connect(lambda _checked=False, pk=pubkey: self._remind(pk))
            self._buttons.addWidget(button)
            self._reminders.append(button)
        self._buttons.addWidget(self.second)

    def reminder_buttons(self) -> list:
        return list(self._reminders)

    def _remind(self, pubkey: str) -> None:
        if self._line:
            self._line = ""
            self.note.hide()
        self.remind_requested.emit(self._sid, pubkey)

    def _show_readers(self, detail) -> None:
        view = detail.row.view
        editor = view.responsible if view is not None else None
        name = self._name_of(editor) if editor else ""
        self.readers.setText(
            _("{name} and the editorial team of {publication}").format(
                name=name, publication=detail.publication) if name
            else _("The editorial team of {publication}").format(
                publication=detail.publication))
        if name:
            self.faces.setPixmap(face(editor, name, self._avatar_of(editor), 22))
        else:
            # Nobody named: the Redaktion has the publication's face.
            self.faces.setPixmap(self._logo(detail.row.publication, detail.publication, 22))

    def _fill_info(self, detail) -> None:
        """Info: the facts, the privacy lines, the history (plan 10.4)."""
        row, view = detail.row, detail.row.view
        mine = row.role == "writer"
        by = (_("you") if mine else (self._name_of(row.writer) or _("the writer"))) \
            if view is not None and view.signer == "writer" else _("the publication")
        responsible = self._name_of(view.responsible) if view is not None \
            and view.responsible else ""
        facts = [(_("Publication"), detail.publication), (_("Published by"), by),
                 (_("Version"), str(view.revision) if view is not None else ""),
                 (_("Responsible"), responsible or _("Editorial team"))]
        people = [self._name_of(p) or _("Someone")
                  for p in (view.participants if view is not None else ())]
        if people:
            facts.append((_("Taking part"), ", ".join(people)))
        lines = [f"<tr><td style='padding-right:10px'>{escape(label)}</td>"
                 f"<td><b>{escape(value)}</b></td></tr>" for label, value in facts if value]
        privacy = "".join(f"<li>{escape(_(line).format(publication=detail.publication))}</li>"
                          for line in PRIVACY)
        locale = display_locale()
        history = "".join(
            f"<li>{escape(threads.caption_text(c, me=self._following.me, name_of=self._name_of))}"
            f" · {escape(_when(c.at, locale))}</li>" for c in detail.history)
        self.info.setText(
            f"<table>{''.join(lines)}</table>"
            f"<p><b>{escape(_('Encryption'))}</b></p><ul>{privacy}</ul>"
            f"<p><b>{escape(_('History'))}</b></p><ul>{history}</ul>")

    def _show_info(self, shown: bool) -> None:
        self.info.setVisible(shown)
        if shown:
            self._stick = False
            self._scroll.verticalScrollBar().setValue(0)        # Info is at the top

    def _on_scrolled(self, value: int) -> None:
        """Scrolled up to read: it stays there; back at the end: it
        follows the end again."""
        self._stick = value >= self._scroll.verticalScrollBar().maximum() - 4

    def _on_range(self, _minimum: int, maximum: int) -> None:
        # The conversation grew or got its height: still at the end.
        if self._stick:
            self._scroll.verticalScrollBar().setValue(maximum)

    def _to_the_end(self) -> None:
        self._stick = True
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- what the person does --------------------------------------------------------

    def _on_action(self) -> None:
        detail = self.detail()
        if detail is None:
            return
        if self._line:
            self._line = ""
            self.note.hide()
        if detail.problem:
            self._following.open(self._sid)         # Erneut versuchen
        elif self._status.action is rules.Action.RELOAD:
            self._following.reload()
        elif self._status.action is not None:
            self.action_requested.emit(self._sid, self._status.action)

    def _on_second(self) -> None:
        """Verwerfen: the entry the server has nothing of leaves the list."""
        if self._following is None:
            return
        self.second.setEnabled(False)
        following = self._following

        def done(stored: bool) -> None:
            self.second.setEnabled(True)
            if not stored and self._following is following:
                self.note.setText(markup(_("The earlier try wasn’t discarded"), _(
                    "Your list of submissions couldn’t be saved: check your internet "
                    "connection and try again.")))
                self.note.show()

        following.discard(self._sid, done)

    def _on_reacted(self, target: str, author: str, emoji: str) -> None:
        detail = self.detail()
        if detail is not None:
            self._following.talk.react(
                self._sid, target, author, emoji,
                threads.own_reaction(detail.opened, target, self._following.me))

    def _fit_composer(self) -> None:
        """One line, growing with the text up to COMPOSER_LINES."""
        composer = self.composer
        line = composer.fontMetrics().lineSpacing()
        # A plain text's layout counts its height in lines, wrapped ones too.
        counted = round(composer.document().size().height())
        lines = max(1, min(COMPOSER_LINES, counted))
        # A scroll bar only once it stops growing.
        composer.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if counted > COMPOSER_LINES
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # What the frame and the style take around the text, as drawn.
        margins = composer.contentsMargins()
        chrome = margins.top() + margins.bottom()
        margin = 2 * int(composer.document().documentMargin())
        composer.setFixedHeight(max(MIN_CONTROL_PX, lines * line + margin + chrome + 2))

    def _on_typed(self) -> None:
        self.send.setEnabled(bool(self.composer.toPlainText().strip()))
        self.problem.hide()

    def _send(self) -> None:
        text = self.composer.toPlainText()
        if self._following is None or not text.strip():
            return
        problem = self._following.talk.send(self._sid, text)
        if problem:
            self.problem.setText(_(problem))        # the text is kept
            self.problem.show()
            announce.announce(self.problem, self.problem.text())
            return
        self.composer.clear()
        self.composer.setFocus()                    # focus returns to the same control
        self._to_the_end()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if watched is self.composer and event.type() in (QEvent.Type.FocusIn,
                                                          QEvent.Type.FocusOut):
            self.hint.setVisible(event.type() == QEvent.Type.FocusIn)
        if watched is self.composer and event.type() == QEvent.Type.KeyPress \
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._send()
            return True
        return super().eventFilter(watched, event)

    def _ask_withdraw(self) -> None:
        detail, following, sid = self.detail(), self._following, self._sid
        if detail is None or not detail.row.open:
            return
        if not confirm_destructive(
                self, title=_("Withdraw this submission?"),
                message=_("{publication} no longer sees it in its inbox then. What the "
                          "editorial team already has stays there.").format(
                              publication=detail.publication),
                action=_("Withdraw"), is_dark=self._is_dark, nostr=True):
            self.more_button.setFocus()
            return

        def answered(error) -> None:
            if error is None or self._following is not following:
                return
            if error.code in (Code.INVALID_TRANSITION, Code.CONFLICT):
                # The Redaktion decided meanwhile: where it stands now is
                # read again and shown, with nothing to try again.
                following.open(sid)
                self.say(_(DECIDED_MEANWHILE).format(publication=detail.publication))
                return
            # Said once, with its real cause and one way on (lessons 36).
            cause = _WITHDRAW_CAUSES.get(error.code) or cause_words(error.code)
            if ask_try_again(self, title=_(NOT_WITHDRAWN), message=_(cause),
                             is_dark=self._is_dark, nostr=True):
                following.withdraw(sid, answered)

        following.withdraw(sid, answered)

    # -- appearance ------------------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self.conversation.is_dark = self._is_dark
        r = appearance.roles(self._is_dark)
        self.info_button.setIcon(QIcon(symbol_pixmap("info", 16, QColor(r.icon))))
        self.more_button.setIcon(QIcon(symbol_pixmap("ellipsis", 16, QColor(r.icon))))
        title = QFont(self.font())
        title.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(title)
        self.sentence.setFont(title)
        self.setStyleSheet(appearance.controls(self._is_dark) + f"""
            #submission_detail, #submission_detail_content {{ background: {r.panel}; }}
            #submission_more, #submission_readers, #submission_info {{ color: {r.muted}; }}
            #submission_problem {{ color: {r.error}; }}
            #submission_composer {{ border-top: 1px solid {r.line}; background: {r.panel}; }}
            #submission_pill {{ background: {r.control}; color: {r.text};
                border: 1px solid {r.line}; border-radius: 10px; padding: 1px 8px 1px 4px; }}
            {appearance.accent_button_css('#submission_action', self._is_dark)}
            #submission_send {{ background: {r.button}; color: {r.on_accent};
                border: none; border-radius: {MIN_CONTROL_PX // 2}px; font-weight: 700; }}
            #submission_send:disabled {{ background: {r.soft}; color: {r.disabled}; }}
            #submission_send:focus {{ border: 2px solid {r.focus}; }}
            QToolButton::menu-indicator {{ image: none; }}
        """)
        self._fit_composer()                        # the style's padding counts
        if self._sid:
            self.refresh()


def _when(at: int, locale) -> str:
    return locale.toString(QDateTime.fromSecsSinceEpoch(int(at)), QLocale.FormatType.ShortFormat)


__all__ = ["NOT_LOADED", "NOT_OPENED", "NOT_WITHDRAWN", "PRIVACY", "SubmissionDetail"]
