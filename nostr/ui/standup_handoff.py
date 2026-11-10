# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The handoff of a submission, in motion (plan 10.3; the prototype's
five phases, failure, no answer, Reduce Motion).

    ┌────────────┐                    ┌ ─ ─ ─ ─ ─ ─ ┐
    │  [ cover ] │ · · · · · · · · ·    [21] Einundzwanzig
    │▔▔▔▔▔▔▔▔▔▔▔▔│                      Eingang der Redaktion
    │ Ein Jahr … │                    └ ─ ─ ─ ─ ─ ─ ┘
    └────────────┘
         Wird an Einundzwanzig Magazin gesendet …

The work card moves only as far as the protocol got
(:class:`~nostr.standup.submitting.Progress`): it lifts while the
pictures are encrypted and uploaded (a determinate bar along the
cover's lower edge) and while the signer app is asked; it travels 60
percent of the way and **holds** while the request is on its way and
while no answer has come; it settles into the publication's inbox only
when the phase is DONE, which only a matching receipt reaches
(submitting.py), and glides back when nothing was sent. Every state's
words are the plan's, said to screen readers as they change.

With Reduce Motion (motion.py) nothing travels or lifts: the card stays
where it is, and settling is a 120 ms cross-fade. The words are the
same.

:class:`HandoffView` is what the sheet shows: the track, the words and
the buttons each state has (Abbrechen while it can still be taken back,
dimmed once the request is on its way, and beside it Im Hintergrund
fortsetzen; Abbrechen and Erneut versuchen after a failure, Schließen
alone when trying again would not help; Schließen while it is being
checked; Fertig once it is in). It only shows the state and forwards the
person's choice. After a refusal for a server's limit, Erneut versuchen
(and Erneut senden for the first message) stays dimmed until the limit's
wait is over, and the words say when that is.

A failure is said once (lessons 36): its title, what did not happen,
and one sentence, the likely cause and what to do. What an upload's
servers answered is technical: only behind Details.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from PySide6.QtCore import (
    Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
    QWidget,
)

import announce
from i18n import _, pgettext

from ..failures import spoken
from ..standup import words
from ..standup.submitting import HELD_WORDS, MessageStatus, Phase, Progress, Step
from . import appearance, motion
from .standup_waits import WaitTimer, wait_words

_CARD_WIDTH = 200
_COVER_HEIGHT = 112
_INBOX_WIDTH = 190
_LOGO = 40
_HOLD = 0.6            # of the way, while the request is on its way
_LIFT = 0.03           # the card's scale when lifted (1.00 to 1.03)


def _elide(painter: QPainter, text: str, width: float) -> str:
    return painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width))


class HandoffTrack(QWidget):
    """The custom-painted track. ``lift``, ``travel``, ``settle``,
    ``fade`` and ``glow`` are animated properties (0 to 1)."""

    def __init__(self, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self._values = {"lift": 0.0, "travel": 0.0, "settle": 0.0, "fade": 1.0, "glow": 0.0}
        self._animations: Dict[str, QPropertyAnimation] = {}
        self._bar: Optional[float] = None
        self._got = False
        self.title = self.summary = self.byline = self.publication = ""
        self.cover = QPixmap()
        self.logo = QPixmap()
        self.setMinimumHeight(_COVER_HEIGHT + 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # -- the animated values -------------------------------------------------

    def _get(self, name: str) -> float:
        return self._values[name]

    def _set(self, name: str, value: float) -> None:
        self._values[name] = float(value)
        self.update()

    lift = Property(float, lambda self: self._get("lift"), lambda self, v: self._set("lift", v))
    travel = Property(float, lambda self: self._get("travel"),
                      lambda self, v: self._set("travel", v))
    settle = Property(float, lambda self: self._get("settle"),
                      lambda self, v: self._set("settle", v))
    fade = Property(float, lambda self: self._get("fade"), lambda self, v: self._set("fade", v))
    glow = Property(float, lambda self: self._get("glow"), lambda self, v: self._set("glow", v))

    def value(self, name: str) -> float:
        return self._values[name]

    def target(self, name: str) -> float:
        """Where ``name`` is going (where it is, when it is not moving)."""
        if self.animating(name):
            return float(self._animations[name].endValue())
        return self._values[name]

    def animating(self, name: str) -> bool:
        animation = self._animations.get(name)
        return animation is not None and animation.state() == QPropertyAnimation.State.Running

    def animate(self, name: str, end: float, ms: int, *, cross_fade: bool = False,
                curve: QEasingCurve.Type = QEasingCurve.Type.OutCubic) -> None:
        """``name`` to ``end`` in ``ms`` (motion.duration: none at all, or
        the 120 ms cross-fade, when motion is reduced)."""
        running = self._animations.pop(name, None)
        if running is not None:
            running.stop()
        duration = motion.duration(ms, cross_fade=cross_fade)
        if duration <= 0 or self._values[name] == end:
            self._set(name, end)
            return
        animation = QPropertyAnimation(self, name.encode("ascii"), self)
        animation.setDuration(duration)
        animation.setStartValue(self._values[name])
        animation.setEndValue(float(end))
        animation.setEasingCurve(curve)
        self._animations[name] = animation
        animation.start()

    def breathe(self, on: bool) -> None:
        """The held card's shadow breathing (never with Reduce Motion)."""
        running = self._animations.pop("glow", None)
        if running is not None:
            running.stop()
        self._set("glow", 0.0)
        if not on or motion.reduce_motion():
            return
        animation = QPropertyAnimation(self, b"glow", self)
        animation.setDuration(1600)
        animation.setKeyValueAt(0.0, 0.0)
        animation.setKeyValueAt(0.5, 1.0)
        animation.setKeyValueAt(1.0, 0.0)
        animation.setLoopCount(-1)
        self._animations["glow"] = animation
        animation.start()

    def set_bar(self, fraction: Optional[float]) -> None:
        self._bar = fraction
        self.update()

    def set_got(self, got: bool) -> None:
        self._got = got
        self.update()

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.update()

    def sizeHint(self):  # noqa: N802, Qt's name
        hint = super().sizeHint()
        hint.setHeight(self.minimumHeight())
        return hint

    # -- painting ------------------------------------------------------------

    def _card_height(self) -> float:
        return _COVER_HEIGHT + 74

    def paintEvent(self, _event) -> None:  # noqa: N802, Qt's name
        r = appearance.roles(self._is_dark)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        card_h = self._card_height()
        top = (height - card_h) / 2
        inbox = QRectF(width - _INBOX_WIDTH, top + 24, _INBOX_WIDTH, card_h - 48)
        if not motion.reduce_motion():
            pen = QPen(QColor(r.line), 1.5, Qt.PenStyle.DotLine)
            painter.setPen(pen)
            y = top + card_h / 2
            painter.drawLine(QPointF(_CARD_WIDTH + 12, y), QPointF(inbox.left() - 12, y))
        self._paint_inbox(painter, r, inbox)
        # The card: centred on the inbox at the end of its way.
        end = inbox.center().x() - _CARD_WIDTH / 2
        x = end * self._values["travel"]
        scale = 1 + _LIFT * self._values["lift"] - 0.45 * self._values["settle"]
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, self._values["fade"])))
        center = QPointF(x + _CARD_WIDTH / 2, top + card_h / 2)
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)
        self._paint_card(painter, r, QRectF(x, top, _CARD_WIDTH, card_h))
        painter.restore()

    def _paint_card(self, painter: QPainter, r, rect: QRectF) -> None:
        lifted = self._values["lift"] + 0.4 * self._values["glow"]
        if lifted > 0 and r.glow_alpha:
            for step in range(1, 7):
                shadow = QColor(60, 20, 120, int(10 * lifted))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(shadow)
                painter.drawRoundedRect(rect.adjusted(-step, -step + 6, step, step + 6),
                                        12 + step, 12 + step)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.setPen(QPen(QColor(r.line), 1))
        painter.setBrush(QColor(r.field))
        painter.drawPath(path)
        cover = QRectF(rect.left(), rect.top(), rect.width(), _COVER_HEIGHT)
        painter.save()
        painter.setClipPath(path)
        if self.cover.isNull():
            painter.fillRect(cover, QColor(r.soft))
        else:
            scaled = self.cover.scaled(int(cover.width()), int(cover.height()),
                                       Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(cover.topLeft(), scaled,
                               QRectF((scaled.width() - cover.width()) / 2,
                                      (scaled.height() - cover.height()) / 2,
                                      cover.width(), cover.height()))
        if self._bar is not None:
            track = QRectF(cover.left(), cover.bottom() - 3, cover.width(), 3)
            soft = QColor(r.selection)
            soft.setAlpha(46)
            painter.fillRect(track, soft)
            painter.fillRect(QRectF(track.left(), track.top(),
                                    track.width() * max(0.0, min(1.0, self._bar)), 3),
                             QColor(r.selection))
        painter.restore()
        text_left, room = rect.left() + 12, rect.width() - 24
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(r.text))
        y = cover.bottom() + 22
        painter.drawText(QPointF(text_left, y), _elide(painter, self.title, room))
        painter.setFont(self.font())
        painter.setPen(QColor(r.muted))
        if self.summary:
            painter.drawText(QPointF(text_left, y + 18), _elide(painter, self.summary, room))
        small = QFont(self.font())
        small.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
        painter.setFont(small)
        painter.drawText(QPointF(text_left, y + 38), _elide(painter, self.byline, room))

    def _paint_inbox(self, painter: QPainter, r, rect: QRectF) -> None:
        accent = QColor(r.selection)
        if self._got:
            fill = QColor(accent)
            fill.setAlpha(20)
            painter.setBrush(fill)
            painter.setPen(QPen(accent, 1.5))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(r.line), 1.5, Qt.PenStyle.DashLine))
        painter.drawRoundedRect(rect, 14, 14)
        logo = QRectF(rect.center().x() - _LOGO / 2, rect.top() + 14, _LOGO, _LOGO)
        if not self.logo.isNull():
            painter.drawPixmap(logo.toRect(), self.logo)
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(r.text))
        name = QRectF(rect.left() + 8, logo.bottom() + 6, rect.width() - 16, 20)
        painter.drawText(name, Qt.AlignmentFlag.AlignHCenter,
                         _elide(painter, self.publication, name.width()))
        painter.setFont(self.font())
        under = QRectF(rect.left() + 8, name.bottom() + 2, rect.width() - 16, 20)
        if self._got:
            painter.setPen(QColor(r.success))
            painter.drawText(under, Qt.AlignmentFlag.AlignHCenter, "✓ " + _("In the inbox"))
        else:
            painter.setPen(QColor(r.muted))
            painter.drawText(under, Qt.AlignmentFlag.AlignHCenter, _("Editorial inbox"))


def phase_words(progress: Progress, publication: str, now: Optional[float] = None) -> tuple:
    """The two lines a state says (translated), the plan's words. While
    the request is on its way and while it is checked, the words are
    what the server holds (HELD_WORDS, lesson 32): a run whose receipt
    came never says "Not confirmed yet" (RG-M2r2a 3). A failure: its
    title, from what the server holds, and its one sentence, with when
    trying again helps while a limit's wait is not over (lessons 36)."""
    phase = progress.phase
    if phase is Phase.FAILED and progress.failure is not None:
        failure = progress.failure
        sentence = failure.said or wait_words(
            failure.retry_at, _now() if now is None else now, words.LIMIT_AT,
            words.LIMIT_TOMORROW_AT, publication=publication) or _(
            failure.sentence).format(publication=publication)
        return _(failure.title).format(publication=publication), sentence
    held = HELD_WORDS[progress.held]
    if progress.step is Step.RELOADING:
        lines = (words.SUBMITTED_ELSEWHERE, words.READING_LIST)
    else:
        lines = {
            Phase.ENCRYPT: (words.ENCRYPTING, ""),
            Phase.UPLOAD: (words.UPLOADING_PICTURE if progress.pictures == 1
                           else words.UPLOADING_PICTURES, words.UPLOADING_DETAIL),
            Phase.SIGNER: (words.WAITING_FOR_SIGNER, words.WAITING_DETAIL),
            Phase.SEND: (held.sending, ""),
            Phase.UNCONFIRMED: (held.checking, words.UNCONFIRMED_DETAIL),
            Phase.DONE: (words.SUBMITTED, words.SUBMITTED_DETAIL),
        }.get(phase, ("", ""))
    return (_(lines[0]).format(publication=publication) if lines[0] else "",
            _(lines[1]) if lines[1] else "")


def _now() -> float:
    return time.time()


_MESSAGE_WORDS = {MessageStatus.SIGNING: words.MESSAGE_SIGNING,
                  MessageStatus.SENDING: words.MESSAGE_SENDING,
                  MessageStatus.SENT: words.MESSAGE_SENT,
                  MessageStatus.FAILED: words.MESSAGE_NOT_SENT}


class HandoffView(QWidget):
    """The track, the words and the buttons (see the module docstring)."""

    cancel_requested = Signal()
    retry_requested = Signal()
    retry_message_requested = Signal()
    close_requested = Signal()
    # The sheet closes and the submission goes on (RG-M2b 10).
    background_requested = Signal()

    def __init__(self, *, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self._phase: Optional[Phase] = None
        self._said = ""
        self._progress: Optional[Progress] = None
        self._error = False                 # the headline in the error colour
        # Shows the state again once a limit's wait is over (RG-M2r 7).
        self._wait = WaitTimer(self._wait_over, self)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.track = HandoffTrack(is_dark)
        column.addWidget(self.track)
        self.headline = QLabel()
        self.headline.setObjectName("handoff_headline")
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        self.message = QLabel()
        self.message.setObjectName("muted")
        # What an upload's servers answered: behind Details (lessons 36).
        self.details_toggle = QToolButton()
        self.details_toggle.setText(_("Details"))
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setAutoRaise(True)
        self.details_toggle.toggled.connect(self._show_details)
        self.details_toggle.hide()
        self.details = QLabel()
        self.details.setObjectName("muted")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.hide()
        for label in (self.headline, self.detail, self.message):
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(label)
        column.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignHCenter)
        self.details.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.details)
        self.send_again = QPushButton(_("Send Again"))
        self.send_again.setAutoDefault(False)
        self.send_again.clicked.connect(self.retry_message_requested)
        again = QHBoxLayout()
        again.addStretch(1)
        again.addWidget(self.send_again)
        again.addStretch(1)
        column.addLayout(again)
        column.addStretch(1)
        self.buttons = QDialogButtonBox()
        self.background_button = self.buttons.addButton(
            _("Continue in Background"), QDialogButtonBox.ButtonRole.ActionRole)
        self.background_button.setAutoDefault(False)
        self.cancel_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.close_button = self.buttons.addButton(_("Close"),
                                                   QDialogButtonBox.ButtonRole.RejectRole)
        self.retry_button = self.buttons.addButton(_("Try Again"),
                                            QDialogButtonBox.ButtonRole.AcceptRole)
        self.done_button = self.buttons.addButton(_("Done"),
                                                  QDialogButtonBox.ButtonRole.AcceptRole)
        self.background_button.clicked.connect(self.background_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.close_button.clicked.connect(self.close_requested)
        self.retry_button.clicked.connect(self.retry_requested)
        self.done_button.clicked.connect(self.close_requested)
        self.set_dark(is_dark)

    def set_work(self, *, title: str, summary: str, byline: str, cover: QPixmap,
                 publication: str, logo: QPixmap) -> None:
        track = self.track
        track.title, track.summary, track.byline = title, summary, byline
        track.cover, track.publication, track.logo = cover, publication, logo
        track.update()

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.track.set_dark(is_dark)
        color = appearance.roles(is_dark).error if self._error else appearance.roles(is_dark).text
        self.headline.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 14px;")

    def show_progress(self, progress: Progress) -> None:
        self._progress = progress
        phase = progress.phase
        now = _now()
        first, second = phase_words(progress, self.track.publication, now)
        message = (_(_MESSAGE_WORDS[progress.message])
                   if phase is Phase.DONE and progress.message in _MESSAGE_WORDS else "")
        retry_at, message_at = self._waits(progress)
        message = " ".join(part for part in (message, wait_words(
            message_at, now, words.SEND_AGAIN_AT, words.SEND_AGAIN_TOMORROW_AT)) if part)
        self.send_again.setEnabled(not message_at or message_at <= now)
        self._wait.follow(retry_at, message_at, now=now)
        self.headline.setText(first)
        self.detail.setText(second)
        self.detail.setVisible(bool(second))
        self.message.setText(message)
        self.message.setVisible(bool(message))
        self.send_again.setVisible(phase is Phase.DONE
                                   and progress.message is MessageStatus.FAILED)
        failure = progress.failure if phase is Phase.FAILED else None
        error = failure is not None and failure.error
        details = failure.details if failure is not None else ""
        self.details.setText(details)
        self.details_toggle.setVisible(bool(details))
        if not details:
            self.details_toggle.setChecked(False)
        self.details.setVisible(bool(details) and self.details_toggle.isChecked())
        if phase is not self._phase or error is not self._error:
            moved, self._phase, self._error = phase is not self._phase, phase, error
            self.set_dark(self._is_dark)
            if moved:
                self._move(phase)
        self.track.set_bar(self._bar(progress))
        self._show_buttons(progress)
        said = spoken(first, second, message)
        if said != self._said:
            self._said = said
            announce.announce(self, said)

    @staticmethod
    def _waits(progress: Progress) -> tuple:
        """The limits' waits this state has: Try Again's, Send Again's."""
        failure = progress.failure if progress.phase is Phase.FAILED else None
        message_failed = progress.phase is Phase.DONE and progress.message is MessageStatus.FAILED
        return (failure.retry_at if failure is not None else None,
                progress.message_retry_at if message_failed else None)

    def _wait_over(self) -> None:
        if self._progress is not None:
            self.show_progress(self._progress)

    def _show_details(self, shown: bool) -> None:
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                         else Qt.ArrowType.RightArrow)
        self.details.setVisible(shown and bool(self.details.text()))

    @staticmethod
    def _bar(progress: Progress) -> Optional[float]:
        if not progress.pictures or progress.phase in (Phase.ENCRYPT, Phase.FAILED):
            return None
        return progress.uploaded if progress.phase is Phase.UPLOAD else 1.0

    def _move(self, phase: Phase) -> None:
        """Where the card goes for ``phase``; only DONE settles it."""
        track, still = self.track, motion.reduce_motion()
        if phase is Phase.DONE:
            self._settle()
            return
        track.set_got(False)
        track.animate("settle", 0.0, 0)
        track.animate("fade", 1.0, 300, cross_fade=True)
        holding = phase in (Phase.SEND, Phase.UNCONFIRMED)
        lifted = phase in (Phase.ENCRYPT, Phase.UPLOAD, Phase.SIGNER) or holding
        track.animate("lift", 0.0 if still or not lifted else 1.0, 160)
        travel = _HOLD if holding and not still else 0.0
        back = phase is Phase.FAILED
        track.animate("travel", travel, 500 if back else 600,
                      curve=QEasingCurve.Type.InOutQuad if back else QEasingCurve.Type.OutCubic)
        track.breathe(holding)

    def _settle(self) -> None:
        """Into the publication's inbox. Called for DONE only, which only a
        matching receipt reaches."""
        track = self.track
        track.breathe(False)
        track.set_got(True)
        if not motion.reduce_motion():
            track.animate("travel", 1.0, 450)
            track.animate("settle", 1.0, 450)
        track.animate("fade", 0.0, 450, cross_fade=True)

    def _show_buttons(self, progress: Progress) -> None:
        phase = progress.phase
        retry = phase is Phase.FAILED and progress.failure is not None \
            and progress.failure.retry is not None
        going = phase not in (Phase.DONE, Phase.FAILED, Phase.UNCONFIRMED)
        self.background_button.setVisible(going)
        self.cancel_button.setVisible(going)
        self.cancel_button.setEnabled(progress.cancellable)
        self.close_button.setVisible(phase in (Phase.FAILED, Phase.UNCONFIRMED))
        # Beside Try Again after a failure, Cancel (lessons 36); alone, or
        # after an end that is no error, Close.
        self.close_button.setText(_("Cancel") if retry and progress.failure.error
                                  else _("Close"))
        self.retry_button.setVisible(retry)
        # Another device stored this round: the list is read again (the
        # contract's words: "Erneut laden", so a context of its own).
        self.retry_button.setText(pgettext("submissions list", "Reload") if retry
                                  and progress.failure.retry is Step.RELOADING
                                  else _("Try Again"))
        retry_at = progress.failure.retry_at if retry else None
        waiting = retry_at is not None and retry_at > _now()
        self.retry_button.setEnabled(not waiting)
        self.done_button.setVisible(phase is Phase.DONE)
        default = (self.done_button if phase is Phase.DONE
                   else self.retry_button if retry and not waiting else None)
        for button in (self.cancel_button, self.close_button, self.retry_button,
                       self.done_button):
            button.setDefault(button is default)


__all__ = ["HandoffTrack", "HandoffView", "phase_words", "wait_words"]
