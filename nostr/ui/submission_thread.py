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
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QDate, QEvent, QLocale, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSizePolicy, QToolButton, QToolTip, QVBoxLayout, QWidget, QWidgetAction,
)

from i18n import N_, _

from ..standup import conversation, following as rules, thread as threads
from . import appearance
from .avatar import color_for_pubkey, initials_for, make_avatar_pixmap_from_image, \
    make_initials_pixmap
from .drafts_common import MIN_CONTROL_PX, display_locale, plain_tip, secondary_font
from .eliding_label import ElidingLabel
from .flow_layout import FlowLayout
from .standup_talk import Outgoing, Sending, footer_words, shown_reactions

_AVATAR = 26
# A run without a space this long (a link, a key, a compound) may break
# anywhere: a label breaks only between words, and one word wider than
# the panel would push the whole conversation out of it.
_LONG_RUN = re.compile(r"\S{20,}")
# Where a link or a path breaks first: after one of its own separators.
_AFTER_SEPARATOR = re.compile(r"(?<=[/\\\-_.?&=#:;,+~%@|])(?=[^/\\\-_.?&=#:;,+~%@|])")
_BREAK = "\u200b"
REACTION_NAMES = dict(zip(conversation.REACTIONS, (
    N_("Heart"), N_("Thumbs up"), N_("Thumbs down"), N_("Laughing"), N_("Exclamation marks"),
    N_("Question mark"))))


def face(pubkey: str, name: str, picture: Optional[QPixmap], size: int = _AVATAR) -> QPixmap:
    """A person's picture as a circle, or their monogram (plan 10.1)."""
    if picture is not None and not picture.isNull():
        return make_avatar_pixmap_from_image(picture, size)
    return make_initials_pixmap(initials_for(name or "?"), color_for_pubkey(pubkey), size)


def _splits(char: str) -> bool:
    """Whether a break may come beside ``char``: a letter, a digit, a
    mark of punctuation or a plain symbol; never an emoji or a combining
    mark, which a break would take apart."""
    return unicodedata.category(char)[0] in "LNP" or char.isascii()


def breakable(text: str) -> str:
    """``text`` as a wrapping label shows it: its long runs (see
    ``_LONG_RUN``) may break after their own separators (a link's
    slashes and hyphens), and a stretch between two of them that is
    still that long between any two of its characters. For what is shown
    only, never for what is copied or sent."""
    def anywhere(piece: str) -> str:
        out = [piece[0]]
        for before, char in zip(piece, piece[1:]):
            # After a letter and its combining mark too, never before the mark.
            after = _splits(before) or unicodedata.category(before)[0] == "M"
            out.append(_BREAK + char if after and _splits(char) else char)
        return "".join(out)

    def run(match) -> str:
        pieces = _AFTER_SEPARATOR.split(match.group(0))
        return _BREAK.join(anywhere(piece) if _LONG_RUN.fullmatch(piece) else piece
                           for piece in pieces if piece)
    return _LONG_RUN.sub(run, text)


def _face_icon(color: QColor, size: int = 16, dpr: float = 2.0) -> QIcon:
    """A smiling face, drawn: the letter for it is a speck in most fonts.
    Reagieren."""
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    unit = size / 16.0
    pen = QPen(color, 1.4 * unit)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(1.5 * unit, 1.5 * unit, 13 * unit, 13 * unit))
    painter.drawArc(QRectF(4.6 * unit, 4.4 * unit, 6.8 * unit, 7.4 * unit), 215 * 16, 110 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for x in (5.8, 10.2):
        painter.drawEllipse(QPointF(x * unit, 6.6 * unit), 0.95 * unit, 0.95 * unit)
    painter.end()
    return QIcon(pixmap)


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
            # Each large enough to hit: the faces a size up, in a box no
            # smaller than the app's other controls.
            font = QFont(button.font())
            font.setPointSizeF(font.pointSizeF() + 3)
            button.setFont(font)
            button.setMinimumSize(MIN_CONTROL_PX + 8, MIN_CONTROL_PX + 4)
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
            # One line, cut with an ellipsis: a long name never widens the
            # conversation (the whole name is its tooltip then).
            sender = ElidingLabel(label)
            sender.setFont(secondary_font())
            sender.setStyleSheet(f"color: {r.muted}; margin-left: {_AVATAR + 8}px;")
            sender.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(sender)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.text = QLabel(breakable(bubble.text))
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
        # Reagieren keeps its place beside the bubble and shows while the
        # pointer is on the message or the keyboard is on the button, as
        # in the web app: a conversation is not a column of buttons.
        self.react_button = QToolButton()
        self._face = _face_icon(QColor(r.icon))
        self._hovered = self._menu_open = False
        self.react_button.setIconSize(QSize(16, 16))
        self.react_button.setAutoRaise(True)
        self.react_button.setFixedSize(MIN_CONTROL_PX, MIN_CONTROL_PX)
        self.react_button.installEventFilter(self)
        # Named by whose message it is (RG-M3-b 11): a list of "React"
        # says nothing to a screen reader.
        spoken = _("React to your message") if message.mine else _(
            "React to the message from {name}").format(name=label) if label else _("React")
        self.react_button.setAccessibleName(spoken)
        self.react_button.setToolTip(plain_tip(spoken))
        self.react_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.react_button.setProperty("bubble", bubble.id)
        self.react_button.setEnabled(view.can_react)
        # With the keyboard on it (a click does not move the focus here).
        self.react_button.clicked.connect(lambda: self._open_menu(
            self.react_button.mapToGlobal(QPoint(0, self.react_button.height())),
            from_button=self.react_button.hasFocus()))
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
        self.footer = None
        if waiting is not None and waiting.state in (Sending.FAILED, Sending.UNCONFIRMED):
            self.footer = view.footer(waiting, mine=message.mine)
            column.addWidget(self.footer)

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

    def react_shown(self) -> bool:
        """Whether Reagieren is in sight: the pointer on the message, the
        keyboard on the button, or its menu open; never while nobody can
        react (a submission that is closed)."""
        return self._view.can_react and (self._hovered or self._menu_open
                                         or self.react_button.hasFocus())

    def _show_react(self) -> None:
        self.react_button.setIcon(self._face if self.react_shown() else QIcon())

    def enterEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().enterEvent(event)
        self._hovered = True
        self._show_react()

    def leaveEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().leaveEvent(event)
        self._hovered = False
        self._show_react()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if watched is self.react_button and event.type() in (QEvent.Type.FocusIn,
                                                              QEvent.Type.FocusOut):
            # After the event: the button knows then whether it has the focus.
            shown = self._view.can_react and (
                event.type() == QEvent.Type.FocusIn or self._hovered or self._menu_open)
            self.react_button.setIcon(self._face if shown else QIcon())
        return super().eventFilter(watched, event)

    def _menu_closed(self, to_button: bool) -> None:
        self._menu_open = False
        if to_button:
            self.react_button.setFocus()                # focus returns (10.8)
        self._show_react()

    def _open_menu(self, where: QPoint, *, from_button: bool = False) -> None:
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
        # Opened with the keyboard on the button, the focus returns to it;
        # opened with the pointer, it stays where it was.
        self._menu_open = True
        self._show_react()
        menu.aboutToHide.connect(lambda: self._menu_closed(from_button))
        menu.popup(where)


@dataclass
class _Shown:
    """One item of the conversation as drawn: what it shows (``key``),
    its widgets, and what it is."""

    key: tuple
    widgets: List[QWidget]
    bubble: Optional[_Bubble] = None
    caption: str = ""
    card: Optional[QFrame] = None


class ThreadView(QWidget):
    """See the module docstring. ``name_of(pubkey)`` is a person's name
    (``""`` when not known), ``avatar_of(pubkey)`` their picture or None;
    ``publication_face(size)`` is the logo of the publication whose
    conversation is shown (the detail sets it). Drawn again, it keeps
    every item up to the first that shows something else (RG-M3-a 4):
    a new message rebuilds nothing before it.

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
        self._shown: List[_Shown] = []
        self._tail: List[QWidget] = []          # messages without a seq, and their lines
        self._tail_footers: List[QWidget] = []
        self._context: tuple = ()
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(2)

    @property
    def bubbles(self) -> List[_Bubble]:
        return [shown.bubble for shown in self._shown if shown.bubble is not None]

    @property
    def captions(self) -> List[str]:
        return [shown.caption for shown in self._shown if shown.caption]

    @property
    def cards(self) -> List[QFrame]:
        return [shown.card for shown in self._shown if shown.card is not None]

    def focusables(self) -> List[QWidget]:
        """What Tab reaches in the conversation, top to bottom."""
        found: List[QWidget] = []
        for index in range(self._column.count()):
            widget = self._column.itemAt(index).widget()
            if widget is None:
                continue
            for button in [widget] + widget.findChildren(QAbstractButton):
                if isinstance(button, QAbstractButton) \
                        and button.focusPolicy() & Qt.FocusPolicy.TabFocus:
                    found.append(button)
        return found

    @property
    def footers(self) -> List[QWidget]:
        """The lines under what did not go, with their one action."""
        return [b.footer for b in self.bubbles if b.footer is not None] + self._tail_footers

    def show_thread(self, detail, me: str) -> None:
        """Draw ``detail`` (a ``standup_following.Detail``) again, from
        the first item that shows something else; the focus stays on the
        same bubble's button."""
        focused = self.focusWidget()
        keep = focused.property("bubble") if focused is not None else None
        can_react = detail.can_write and detail.row.view is not None and detail.row.view.is_open
        context = (me, can_react, self.is_dark)
        self.me = me
        waiting = {out.target: out for out in detail.outgoing if out.target}
        keys = [self._key(item, detail, waiting) for item in detail.thread]
        same = 0
        if context == self._context:
            while same < min(len(keys), len(self._shown)) and self._shown[same].key == keys[same]:
                same += 1
        for widget in [w for shown in self._shown[same:] for w in shown.widgets] + self._tail:
            self._column.removeWidget(widget)
            discard(widget)
        while self._column.count() and self._column.itemAt(
                self._column.count() - 1).spacerItem() is not None:
            self._column.takeAt(self._column.count() - 1)     # the stretch at the end
        self._shown, self._tail, self._tail_footers = self._shown[:same], [], []
        self.can_react, self._context = can_react, context
        r = appearance.roles(self.is_dark)
        for item, key in zip(detail.thread[same:], keys[same:]):
            start = self._column.count()
            shown = _Shown(key, [])
            if isinstance(item, threads.Day):
                self._quiet(day_words(item.at), r, strong=True)
            elif isinstance(item, threads.Caption):
                shown.caption = self._caption(item, r)
            elif isinstance(item, threads.VersionCard):
                shown.card = self._card(item, detail, r)
            elif isinstance(item, threads.SplitCard):
                text = threads.split_text(item, me=self.me, name_of=self.name_of)
                self._quiet(text, r)
                shown.caption = text
            else:
                shown.bubble = _Bubble(self, item, waiting.get(item.bubble.id))
                self._column.addWidget(shown.bubble)
                if keep == item.bubble.id:
                    shown.bubble.react_button.setFocus()
            shown.widgets = [self._column.itemAt(i).widget()
                             for i in range(start, self._column.count())]
            self._shown.append(shown)
        start = self._column.count()
        for out in detail.outgoing:
            if out.is_message:
                self._outgoing(out, r)
        self._tail = [self._column.itemAt(i).widget() for i in range(start, self._column.count())]
        self._column.addStretch(1)

    def _key(self, item: threads.ThreadItem, detail, waiting: Dict[str, Outgoing]) -> tuple:
        """What ``item`` shows, in words: drawn again only when it differs."""
        if isinstance(item, threads.Day):
            return ("day", day_words(item.at))
        if isinstance(item, threads.Caption):
            return ("caption", item, threads.caption_text(item, me=self.me,
                                                          name_of=self.name_of))
        if isinstance(item, threads.SplitCard):
            return ("split", item, threads.split_text(item, me=self.me, name_of=self.name_of))
        if isinstance(item, threads.VersionCard):
            version = detail.versions.get(item.revision)
            return ("card", item, threads.card_title(item, me=self.me, name_of=self.name_of),
                    version.change_summary if version is not None else "")
        out = waiting.get(item.bubble.id)
        return ("message", item, self.name_of(item.bubble.author),
                None if out is None else (out.key, out.state, out.text, out.sentence))

    def _quiet(self, text: str, r, *, strong: bool = False) -> QLabel:
        label = QLabel(breakable(text))
        label.setFont(secondary_font())
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet(f"color: {r.muted}; margin: {8 if strong else 4}px 0 2px 0;"
                            + ("font-weight: 600;" if strong else ""))
        self._column.addWidget(label)
        return label

    def _caption(self, caption: threads.Caption, r) -> str:
        text = threads.caption_text(caption, me=self.me, name_of=self.name_of)
        label = self._quiet(text, r)
        if threads.unsigned(caption):
            more = more_about_button(_(rules.NOT_SIGNED), r)
            self._column.addWidget(more, 0, Qt.AlignmentFlag.AlignHCenter)
            label.setAccessibleDescription(_(rules.NOT_SIGNED))
        return text

    def _card(self, card: threads.VersionCard, detail, r) -> QFrame:
        frame = QFrame()
        frame.setObjectName("version_card")
        frame.setStyleSheet(f"#version_card {{ background: {r.field}; border: 1px solid "
                            f"{r.line}; border-radius: 10px; }}")
        column = QVBoxLayout(frame)
        column.setContentsMargins(10, 8, 10, 8)
        column.setSpacing(4)
        words = threads.card_title(card, me=self.me, name_of=self.name_of)
        title = QLabel(breakable(words))
        font = QFont(title.font())
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)                 # a long name wraps, never widens
        column.addWidget(title)
        version = detail.versions.get(card.revision)
        for text in (version.change_summary if version is not None else "", card.note):
            if text:
                line = QLabel(breakable(text))
                line.setWordWrap(True)
                line.setTextFormat(Qt.TextFormat.PlainText)
                line.setFont(secondary_font())
                line.setStyleSheet(f"color: {r.muted};")
                column.addWidget(line)
        # They wrap into rows in a narrow pane (RG-M4-c 4), never clipped.
        row = QWidget()
        buttons = FlowLayout(row, spacing=6)
        opener = QPushButton(_("Open in Editor"))
        opener.clicked.connect(lambda: self.open_version.emit(card.revision))
        buttons.addWidget(opener)
        if card.revision > 1:
            changes = QPushButton(_("Show Changes"))
            changes.clicked.connect(lambda: self.compare_version.emit(card.revision))
            buttons.addWidget(changes)
        column.addWidget(row)
        frame.setAccessibleName(words)
        self._column.addWidget(frame)
        return frame

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
        return holder

    def _outgoing(self, out: Outgoing, r) -> None:
        """A message of this person that has no ``seq`` yet: its text,
        lighter, and what stands under it."""
        row = QHBoxLayout()
        row.addStretch(1)
        text = QLabel(breakable(out.text))
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        text.setStyleSheet(f"background: {r.soft}; color: {r.text}; border: 1px dashed "
                           f"{r.edge}; border-radius: 12px; padding: 6px 10px;")
        row.addWidget(text)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 8, 0, 0)
        self._column.addWidget(holder)
        footer = self.footer(out)
        self._tail_footers.append(footer)
        self._column.addWidget(footer)


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
