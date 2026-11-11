# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The conversation about a submission, drawn (plan 7.1, 7.3, 10.7,
10.8; the prototype's screens 16 to 19 and 23).

    ─────────────  Heute  ─────────────
              Eingereicht · Fassung 1
    Anna
    (A) ╭──────────────────────────╮ ☺
        │ Kannst du einen Absatz … │❤️
        ╰──────────────────────────╯
              Anna hat Änderungen erbeten
                      ╭──────────────╮
                    ☺ │ Mach ich!    │
                      ╰──────────────╯
                        Nicht gesendet. … [Erneut senden]

:class:`ThreadView` shows what ``nostr/standup/thread.py`` built and
forwards the person's choices; it sends nothing itself. Bubbles are
grouped by sender, the name on the first of a group and the picture on
the last; decisions are quiet captions (one that carries no verified
signature has "Mehr dazu" beside it, contract 10); a new version is a
card. A reaction works like Tapback: a small button beside each bubble
("Reagieren", in the tab order) and the context menu open six reactions
and Kopieren, moved through with the arrow keys; the chip sits on the
bubble's corner, tinted when it is one's own, dimmed until the server
stored it, and says who reacted to a screen reader. Every text a
stranger wrote is shown as plain text.
"""

from __future__ import annotations

import datetime
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QDate, QLocale, QPoint, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy,
    QToolButton, QToolTip, QVBoxLayout, QWidget, QWidgetAction,
)

from i18n import N_, _

from ..standup import conversation, following as rules, thread as threads
from . import appearance
from .avatar import color_for_pubkey, initials_for, make_avatar_pixmap_from_image, \
    make_initials_pixmap
from .drafts_common import display_locale, plain_tip, secondary_font
from .standup_talk import Outgoing, Sending, footer_words, shown_reactions

_AVATAR = 26
REACTION_NAMES = dict(zip(conversation.REACTIONS, (
    N_("Heart"), N_("Thumbs up"), N_("Thumbs down"), N_("Laughing"), N_("Exclamation marks"),
    N_("Question mark"))))


def face(pubkey: str, name: str, picture: Optional[QPixmap], size: int = _AVATAR) -> QPixmap:
    """A person's picture as a circle, or their monogram (plan 10.1)."""
    if picture is not None and not picture.isNull():
        return make_avatar_pixmap_from_image(picture, size)
    return make_initials_pixmap(initials_for(name or "?"), color_for_pubkey(pubkey), size)


def day_words(at: int, now: Optional[datetime.date] = None) -> str:
    day = datetime.datetime.fromtimestamp(at).date()
    today = now or datetime.date.today()
    if day == today:
        return _("Today")
    if day == today - datetime.timedelta(days=1):
        return _("Yesterday")
    return display_locale().toString(QDate(day.year, day.month, day.day),
                                     QLocale.FormatType.LongFormat)


def reactions_spoken(reactions: Dict[str, str], me: str, name_of: Callable[[str], str]) -> str:
    """"Reaktionen: Anna: Daumen hoch" (plan 10.8)."""
    said = []
    for who, emoji in reactions.items():
        person = _("You") if who == me else (name_of(who) or _("Someone"))
        named = REACTION_NAMES.get(emoji)
        said.append(person + ": " + (_(named) if named else emoji))
    return _("Reactions: {list}").format(list=", ".join(said))


class _ReactionBar(QWidget):
    """The six reactions in a row; the arrow keys move between them."""

    chosen = Signal(str)

    def __init__(self, mine: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)
        self.buttons: List[QToolButton] = []
        for emoji in conversation.REACTIONS:
            button = QToolButton()
            button.setText(emoji)
            button.setCheckable(True)
            button.setChecked(emoji == mine)
            button.setAutoRaise(True)
            button.setAccessibleName(_(REACTION_NAMES[emoji]))
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.clicked.connect(lambda _checked=False, e=emoji: self.chosen.emit(e))
            row.addWidget(button)
            self.buttons.append(button)

    def keyPressEvent(self, event) -> None:  # noqa: N802, Qt's name
        step = {Qt.Key.Key_Right: 1, Qt.Key.Key_Left: -1}.get(event.key())
        focused = next((i for i, b in enumerate(self.buttons) if b.hasFocus()), None)
        if step is None or focused is None:
            super().keyPressEvent(event)
            return
        self.buttons[(focused + step) % len(self.buttons)].setFocus()


class _Bubble(QFrame):
    """One message: the bubble, its chip, and the button that reacts."""

    def __init__(self, view: "ThreadView", message: threads.Message,
                 waiting: Optional[Outgoing]) -> None:
        super().__init__()
        self._view = view
        self.message = message
        bubble = message.bubble
        r = appearance.roles(view.is_dark)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 2 if not message.first else 8, 0, 0)
        column.setSpacing(2)
        name = view.name_of(bubble.author)
        # Who the sender is decides what they are called (thread.role_of).
        label = threads.sender_label(message.role, name)
        if message.first and not message.mine and label:
            sender = QLabel(label)
            sender.setFont(secondary_font())
            sender.setStyleSheet(f"color: {r.muted}; margin-left: {_AVATAR + 8}px;")
            sender.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(sender)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.text = QLabel(bubble.text)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setWordWrap(True)
        self.text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        fill, ink = (r.selection, r.on_accent) if message.mine else (r.control, r.text)
        self.text.setStyleSheet(
            f"background: {fill}; color: {ink}; border: 1px solid "
            f"{fill if message.mine else r.line}; border-radius: 12px; padding: 6px 10px;")
        self.text.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.text.customContextMenuRequested.connect(
            lambda pos: self._open_menu(self.text.mapToGlobal(pos)))
        self.react_button = QToolButton()
        self.react_button.setText("☺")
        self.react_button.setAutoRaise(True)
        self.react_button.setAccessibleName(_("React"))
        self.react_button.setToolTip(_("React"))
        self.react_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.react_button.setProperty("bubble", bubble.id)
        self.react_button.setEnabled(view.can_react)
        self.react_button.clicked.connect(lambda: self._open_menu(
            self.react_button.mapToGlobal(QPoint(0, self.react_button.height()))))
        picture = QLabel()
        picture.setFixedSize(_AVATAR, _AVATAR)
        if message.last and not message.mine:
            if message.role is threads.Role.MEMBER and not name:
                # An editor without a known name is the Redaktion: the
                # publication's face, never a question mark.
                picture.setPixmap(view.publication_face(_AVATAR))
            elif label:
                picture.setPixmap(face(bubble.author, label, view.avatar_of(bubble.author)))
        shown, dimmed = shown_reactions(bubble.reactions, view.me, waiting)
        self.shown_reactions, self.dimmed = shown, dimmed
        self.chip = self._chip(shown, dimmed, r) if shown else None
        parts = [self.react_button, self.text] if message.mine else [picture, self.text,
                                                                     self.react_button]
        if message.mine:
            row.addStretch(1)
        for part in parts:
            row.addWidget(part, 0, Qt.AlignmentFlag.AlignBottom)
        if self.chip is not None:
            row.insertWidget(row.indexOf(self.text) + (0 if message.mine else 1), self.chip, 0,
                             Qt.AlignmentFlag.AlignTop)
        if not message.mine:
            row.addStretch(1)
        column.addLayout(row)
        if bubble.edited:
            edited = QLabel(_("edited"))
            edited.setFont(secondary_font())
            edited.setStyleSheet(f"color: {r.muted};")
            column.addWidget(edited, 0, Qt.AlignmentFlag.AlignRight if message.mine
                             else Qt.AlignmentFlag.AlignLeft)
        if waiting is not None and waiting.state in (Sending.FAILED, Sending.UNCONFIRMED):
            column.addWidget(view.footer(waiting, mine=message.mine))

    def _chip(self, shown: Dict[str, str], dimmed: bool, r) -> QToolButton:
        view = self._view
        chip = QToolButton()
        chip.setText("".join(dict.fromkeys(shown.values())))
        chip.setAutoRaise(True)
        own = view.me in shown
        chip.setStyleSheet(
            f"QToolButton {{ background: {r.soft if own else r.control}; color: {r.text}; "
            f"border: 1px solid {r.focus if own else r.line}; border-radius: 9px; "
            f"padding: 0px 5px; }}")
        spoken = reactions_spoken(shown, view.me, view.name_of)
        chip.setAccessibleName(spoken)
        chip.setToolTip(plain_tip(spoken))
        chip.clicked.connect(lambda: QToolTip.showText(
            chip.mapToGlobal(QPoint(0, chip.height())), plain_tip(spoken), chip))
        if dimmed:
            effect = QGraphicsOpacityEffect(chip)
            effect.setOpacity(0.45)
            chip.setGraphicsEffect(effect)
        return chip

    def _open_menu(self, where: QPoint) -> None:
        view, bubble = self._view, self.message.bubble
        menu = QMenu(self)
        menu.setAccessibleName(_("React"))
        bar = None
        if view.can_react:
            bar = _ReactionBar(self.shown_reactions.get(view.me, ""), menu)
            bar.chosen.connect(lambda emoji: (menu.close(), view.reacted.emit(
                bubble.id, bubble.author, emoji)))
            action = QWidgetAction(menu)
            action.setDefaultWidget(bar)
            menu.addAction(action)
            menu.addSeparator()
        menu.addAction(_("Copy"), lambda: QGuiApplication.clipboard().setText(bubble.text))
        view.menu = menu
        if bar is not None:
            bar.buttons[0].setFocus()
        menu.aboutToHide.connect(self.react_button.setFocus)    # focus returns (10.8)
        menu.popup(where)


class ThreadView(QWidget):
    """See the module docstring. ``name_of(pubkey)`` is a person's name
    (``""`` when not known), ``avatar_of(pubkey)`` their picture or None;
    ``publication_face(size)`` is the logo of the publication whose
    conversation is shown (the detail sets it).

    Signals:
      reacted(str, str, str)   the message's id, its author, the emoji
      retry(str)               Erneut senden, Erneut prüfen, Erneut: an outgoing's key
      open_version(int)        Im Editor öffnen on a version's card
      compare_version(int)     Änderungen zeigen on a version's card
    """

    reacted = Signal(str, str, str)
    retry = Signal(str)
    open_version = Signal(int)
    compare_version = Signal(int)

    def __init__(self, *, name_of: Callable[[str], str],
                 avatar_of: Callable[[str], Optional[QPixmap]], is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.name_of, self.avatar_of, self.is_dark = name_of, avatar_of, is_dark
        self.me = ""
        self.can_react = False
        self.publication_face: Callable[[int], QPixmap] = lambda size: QPixmap()
        self.menu: Optional[QMenu] = None
        self.bubbles: List[_Bubble] = []
        self.captions: List[str] = []
        self.cards: List[QFrame] = []
        self.footers: List[QWidget] = []
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(2)

    def show_thread(self, detail, me: str) -> None:
        """Draw ``detail`` (a ``standup_following.Detail``) again; the
        focus stays on the same bubble's button."""
        focused = self.focusWidget()
        keep = focused.property("bubble") if focused is not None else None
        while self._column.count():
            discard(self._column.takeAt(0).widget())
        self.me, self.can_react = me, detail.can_write and detail.row.view is not None \
            and detail.row.view.is_open
        self.bubbles, self.captions, self.cards, self.footers = [], [], [], []
        waiting = {out.target: out for out in detail.outgoing if out.target}
        r = appearance.roles(self.is_dark)
        for item in detail.thread:
            if isinstance(item, threads.Day):
                self._quiet(day_words(item.at), r, strong=True)
            elif isinstance(item, threads.Caption):
                self._caption(item, r)
            elif isinstance(item, threads.VersionCard):
                self._card(item, detail, r)
            else:
                bubble = _Bubble(self, item, waiting.get(item.bubble.id))
                self.bubbles.append(bubble)
                self._column.addWidget(bubble)
                if keep == item.bubble.id:
                    bubble.react_button.setFocus()
        for out in detail.outgoing:
            if out.is_message:
                self._outgoing(out, r)
        self._column.addStretch(1)

    def _quiet(self, text: str, r, *, strong: bool = False) -> QLabel:
        label = QLabel(text)
        label.setFont(secondary_font())
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet(f"color: {r.muted}; margin: {8 if strong else 4}px 0 2px 0;"
                            + ("font-weight: 600;" if strong else ""))
        self._column.addWidget(label)
        return label

    def _caption(self, caption: threads.Caption, r) -> None:
        text = threads.caption_text(caption, me=self.me, name_of=self.name_of)
        self.captions.append(text)
        label = self._quiet(text, r)
        if threads.unsigned(caption):
            more = more_about_button(_(rules.NOT_SIGNED), r)
            self._column.addWidget(more, 0, Qt.AlignmentFlag.AlignHCenter)
            label.setAccessibleDescription(_(rules.NOT_SIGNED))

    def _card(self, card: threads.VersionCard, detail, r) -> None:
        frame = QFrame()
        frame.setObjectName("version_card")
        frame.setStyleSheet(f"#version_card {{ background: {r.field}; border: 1px solid "
                            f"{r.line}; border-radius: 10px; }}")
        column = QVBoxLayout(frame)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(4)
        title = QLabel(threads.card_title(card, me=self.me, name_of=self.name_of))
        font = QFont(title.font())
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        title.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(title)
        version = detail.versions.get(card.revision)
        for text in (version.change_summary if version is not None else "", card.note):
            if text:
                line = QLabel(text)
                line.setWordWrap(True)
                line.setTextFormat(Qt.TextFormat.PlainText)
                line.setFont(secondary_font())
                line.setStyleSheet(f"color: {r.muted};")
                column.addWidget(line)
        buttons = QHBoxLayout()
        opener = QPushButton(_("Open in Editor"))
        opener.clicked.connect(lambda: self.open_version.emit(card.revision))
        buttons.addWidget(opener)
        if card.revision > 1:
            changes = QPushButton(_("Show Changes"))
            changes.clicked.connect(lambda: self.compare_version.emit(card.revision))
            buttons.addWidget(changes)
        buttons.addStretch(1)
        column.addLayout(buttons)
        frame.setAccessibleName(title.text())
        self.cards.append(frame)
        self._column.addWidget(frame)

    def footer(self, out: Outgoing, *, mine: bool = True) -> QWidget:
        """The line under what did not go, with its one action."""
        r = appearance.roles(self.is_dark)
        words, action = footer_words(out)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 4)
        column.setSpacing(4)
        side = Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft
        label = QLabel(words)
        label.setFont(secondary_font())
        label.setWordWrap(True)
        label.setAlignment(side)
        label.setTextFormat(Qt.TextFormat.PlainText)
        failed = out.state is Sending.FAILED
        label.setStyleSheet(f"color: {r.error if failed else r.muted};")
        column.addWidget(label)
        if action:
            button = QPushButton(action)
            button.setFont(secondary_font())
            button.clicked.connect(lambda: self.retry.emit(out.key))
            column.addWidget(button, 0, side)
            holder.setProperty("action", action)
        holder.setProperty("words", words)
        self.footers.append(holder)
        return holder

    def _outgoing(self, out: Outgoing, r) -> None:
        """A message of this person that has no ``seq`` yet: its text,
        lighter, and what stands under it."""
        row = QHBoxLayout()
        row.addStretch(1)
        text = QLabel(out.text)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        text.setStyleSheet(f"background: {r.soft}; color: {r.text}; border: 1px dashed "
                           f"{r.edge}; border-radius: 12px; padding: 6px 10px;")
        row.addWidget(text)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 8, 0, 0)
        self._column.addWidget(holder)
        self._column.addWidget(self.footer(out))


def discard(widget: Optional[QWidget]) -> None:
    """A widget taken out of its layout goes at once: left with its
    parent until the next turn, it would still be drawn over what took
    its place."""
    if widget is not None:
        widget.hide()
        widget.setParent(None)
        widget.deleteLater()


def more_about_button(words: str, r) -> QToolButton:
    """"Mehr dazu": a quiet, labelled button whose words show on a click
    and on focus (never on hover only, lesson 26)."""
    more = QToolButton()
    more.setText(_("More about this"))
    more.setAutoRaise(True)
    more.setFont(secondary_font())
    more.setStyleSheet(f"QToolButton {{ color: {r.ink}; border: none; }}")
    more.setToolTip(plain_tip(words))
    more.setAccessibleDescription(words)
    more.clicked.connect(lambda: QToolTip.showText(
        more.mapToGlobal(QPoint(0, more.height())), plain_tip(words), more))
    return more


__all__ = ["REACTION_NAMES", "ThreadView", "day_words", "discard", "face", "more_about_button",
           "reactions_spoken"]
