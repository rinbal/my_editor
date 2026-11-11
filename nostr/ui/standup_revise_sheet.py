# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Neue Fassung abgeben: a sheet that hands in the next version of an
open submission (plan 8.1, 10.4; the prototype's screen 22).

    Fassung 2 an Einundzwanzig Magazin
    ┌ Ein Jahr mit Lightning im Café ────────────────┐
    │ Geändert: 1 Absatz neu        [Änderungen zeigen]│
    └─────────────────────────────────────────────────┘
    Notiz an die Redaktion (optional)
    [ Was hat sich geändert?                        ]
    🔒 Ende-zu-Ende verschlüsselt, wie Fassung 1.
                                 [Abbrechen] [Abgeben]

It says what changed since the version handed in (one line, from the
paragraphs; ``nostr/standup/compare.py``) and shows the changes word by
word on a page of its own (never a second sheet). Abgeben is the
default, dimmed only by a blocker said above it: the same text again, a
title missing, a secret key, a note too long. It then turns into the
handoff, the same as for a first submission (standup_handoff.py): the
version is built with the keys its submission has, recorded, signed and
sent, and counts only with its receipt. The note goes with the version
as its note (the contract's ``revision-note``).

The run is the writer's (``StandupWriter.revise``): closing the sheet
once the request is on its way lets it go on in the background.

``again``: the version stored last could not be shown to be the
writer's ("Nicht eingereicht", 12.4), so the text goes once more as the
next version (RG-M3-a 6). Then the same text is no blocker, the card
says so, and when no earlier version of the writer could be opened, the
rights are asked here as in the first submission.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import N_, _

from ..signer_limits import signable
from ..standup import compare, drafting
from ..standup.drafting import ArticleDraft, Choice
from ..standup.submitting import MessageStatus
from . import appearance
from .publish_sheet import StatusLine, fit_margins, fit_to_window, make_sheet, sheet_stylesheet
from .standup_compare import NO_CHANGES, CompareView, Comparer
from .standup_handoff import HandoffView
from .standup_submit import StandupWriter, SubmissionRun

SAME_TEXT = N_("This text is the same as version {number}. Change it to hand in a new one.")
SENT_AGAIN = N_("Will be sent again as version {number}.")
RIGHTS = N_("I have the rights to the text and the pictures.")
DETAILS_CHANGED = N_("Details changed")
# A picture named by a key of its own in each version: the same picture.
_PICTURE = re.compile(r"(?:standup-media|myeditor-asset):[0-9a-f]{64}")


def comparable(text: str) -> str:
    """A version's text with every picture named alike: a local picture
    has another name in each version (its encrypted file's), which is no
    change of the text."""
    return _PICTURE.sub("standup-media", text or "").rstrip()


def change_line(draft: ArticleDraft, work) -> str:
    """What changed between the version handed in (its ``work``) and
    ``draft``, in one line (translated); ``""`` when nothing did."""
    old, new = comparable(str(work.get("content") or "")), comparable(draft.content)
    if old != new:
        return compare.change_summary(old, new)
    same = (draft.title == work.get("title") and draft.summary == (work.get("summary") or "")
            and list(draft.tags) == list(work.get("tags") or ()))
    return "" if same else _(DETAILS_CHANGED)


class ReviseSheet(QDialog):
    """See the module docstring. ``previous`` is the version handed in
    (``standup_following.Version``), ``row`` its submission's row.
    ``view_submission(sid)`` when Einreichung ansehen is chosen."""

    view_submission = Signal(str)

    def __init__(self, *, writer: StandupWriter, profile, draft: ArticleDraft, cover: QPixmap,
                 byline: str, row, publication: str, logo: QPixmap, previous,
                 comparer: Comparer, private_names: Sequence[str] = (), is_dark: bool,
                 again: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._again = bool(again)
        self.setWindowTitle(_("Send Again") if again else _("Hand In New Version"))
        # It holds the version handed in, opened: gone with the sheet.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        make_sheet(self)
        self.setMinimumSize(420, 300)
        self._writer, self._profile, self._draft = writer, profile, draft
        self._cover, self._byline, self._row = cover, byline, row
        self._publication, self._logo, self._previous = publication, logo, previous
        self._comparer, self._private = comparer, list(private_names)
        self._is_dark = bool(is_dark)
        self._number = row.view.revision + 1
        self._summary = change_line(draft, previous.work) if previous is not None else ""
        self.run: Optional[SubmissionRun] = None
        self._build()
        writer.stopped.connect(self._on_account_stopped)
        running = writer.running(profile, draft.d, row.publication)
        if running is not None:
            self._show_run(running)                 # one run per work
        self._refresh()
        self.note.setFocus()                        # the one thing to fill in
        fit_to_window(self, QSize(600, min(560, max(380, self.sizeHint().height()))))

    def _build(self) -> None:
        self._style()
        appearance.bind(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)
        self.heading = QLabel(_("Version {number} to {publication}").format(
            number=self._number, publication=self._publication))
        self.heading.setObjectName("title")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        root.addWidget(self.heading)
        self._pages = QStackedWidget()
        root.addWidget(self._pages, 1)
        self._pages.addWidget(self._form_page())
        self.handoff = HandoffView(is_dark=self._is_dark)
        self.handoff.cancel_requested.connect(self._cancel_run)
        self.handoff.retry_requested.connect(lambda: self.run and self.run.retry())
        self.handoff.retry_message_requested.connect(
            lambda: self.run and self.run.retry_message())
        self.handoff.close_requested.connect(self.accept)
        self.handoff.background_requested.connect(self.accept)
        self.handoff.view_requested.connect(self._view)
        handoff_page = QWidget()
        column = QVBoxLayout(handoff_page)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.handoff, 1)
        column.addWidget(self.handoff.buttons)
        self._pages.addWidget(handoff_page)
        self._pages.addWidget(self._changes_page())
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def _form_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        card = QFrame()
        card.setObjectName("revise_card")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 12, 10)
        words = QVBoxLayout()
        words.setSpacing(2)
        title = QLabel(self._draft.title or _("Untitled"))
        title.setObjectName("revise_title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        self.changed = QLabel(
            _(SENT_AGAIN).format(number=self._number) if self._again
            else _("Changed: {summary}").format(summary=self._summary) if self._summary
            else _(NO_CHANGES))
        self.changed.setObjectName("help")
        self.changed.setTextFormat(Qt.TextFormat.PlainText)
        self.changed.setWordWrap(True)
        words.addWidget(title)
        words.addWidget(self.changed)
        row.addLayout(words, 1)
        self.show_changes = QPushButton(_("Show Changes"))
        self.show_changes.setAutoDefault(False)
        self.show_changes.clicked.connect(self._show_changes)
        row.addWidget(self.show_changes, 0, Qt.AlignmentFlag.AlignVCenter)
        self.show_changes.setVisible(self._previous is not None)
        column.addWidget(card)
        # Asked only when no earlier version of the writer could be read.
        self.rights = QCheckBox(_(RIGHTS))
        self.rights.toggled.connect(self._refresh)
        self.rights.setVisible(self._previous is None)
        column.addWidget(self.rights)
        label = QLabel(_("Note to the editorial team (optional)"))
        self.note = QPlainTextEdit()
        # Sent again, nothing changed: the note is only a note.
        self.note.setPlaceholderText("" if self._again else _("What changed?"))
        self.note.setAccessibleName(_("Note to the editorial team (optional)"))
        self.note.setTabChangesFocus(True)
        self.note.setFixedHeight(84)
        self.note.textChanged.connect(self._refresh)
        label.setBuddy(self.note)
        column.addWidget(label)
        column.addWidget(self.note)
        self.privacy = QLabel(_("End-to-end encrypted, like version {number}.").format(
            number=self._number - 1))
        self.privacy.setObjectName("help")
        self.privacy.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.privacy)
        column.addStretch(1)
        self.status = StatusLine(self._is_dark)
        column.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.hand_in = buttons.addButton(_("Hand In"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.hand_in.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.hand_in.clicked.connect(self._on_hand_in)
        column.addWidget(buttons)
        return page

    def _changes_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        self.changes = CompareView(is_dark=self._is_dark)
        column.addWidget(self.changes, 1)
        buttons = QDialogButtonBox()
        self.back = buttons.addButton(_("Back"), QDialogButtonBox.ButtonRole.RejectRole)
        self.back.setAutoDefault(False)
        self.back.clicked.connect(self._hide_changes)
        column.addWidget(buttons)
        return page

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    # -- the changes -----------------------------------------------------------------

    def _show_changes(self) -> None:
        self._pages.setCurrentIndex(2)
        self.changes.show_pair(self._comparer,
                               comparable(str(self._previous.work.get("content") or "")),
                               comparable(self._draft.content))
        self.back.setFocus()

    def _hide_changes(self) -> None:
        self.changes.forget()                       # a compare still on its way stops
        self._pages.setCurrentIndex(0)
        self.show_changes.setFocus()                # focus returns to the opening control

    # -- handing in ------------------------------------------------------------------

    def blockers(self) -> List[str]:
        """Why Abgeben is dimmed, most pressing first."""
        if not self._summary and not self._again:
            return [_(SAME_TEXT).format(number=self._number - 1)]
        return drafting.blockers(
            self._draft, writer=self._profile.user_pubkey, rights=self._rights(),
            rights_required=self._previous is None,
            private_names=self._private, message=self.note.toPlainText(),
            publication=self._publication,
            signable=lambda unsigned: signable(self._profile, unsigned))

    def _refresh(self) -> None:
        blockers = self.blockers()
        self.hand_in.setEnabled(not blockers)
        first = next(iter(blockers), "")
        first = first if isinstance(first, drafting.Blocker) else drafting.Blocker(first)
        self.status.say(first, title=first.title, error=first.error, detail=first.detail)

    def _on_hand_in(self) -> None:
        if self.blockers():
            return
        view = self._row.view
        choice = Choice(publication=self._row.publication, name=self._publication,
                        signer=view.signer, rights=self._rights(),
                        message=self.note.toPlainText())
        run = self._writer.revise(self._profile, self._draft, choice, round_=view.round,
                                  revision=self._number, change_summary=self._summary)
        self._show_run(run)
        run.start()

    def _rights(self) -> bool:
        return self._previous.rights if self._previous is not None else self.rights.isChecked()

    def _show_run(self, run: SubmissionRun) -> None:
        self.run = run
        self.handoff.set_work(title=self._draft.title, summary=self._draft.summary,
                              byline=self._byline, cover=self._cover,
                              publication=self._publication, logo=self._logo,
                              number=self._number)
        run.behind_the_sheet(None)
        run.changed.connect(self.handoff.show_progress)
        self._pages.setCurrentIndex(1)
        self.handoff.show_progress(run.progress)
        self.handoff.setFocus()

    def _view(self) -> None:
        sid = self._row.sid
        self.accept()
        self.view_submission.emit(sid)

    def _cancel_run(self) -> None:
        if self.run is not None and self.run.progress.cancellable:
            self.reject()

    def reject(self) -> None:  # noqa: N802, Qt's name
        """Cancel and Escape: on the changes, back to the form; a run
        that may still be cancelled is cancelled; once its request went
        out, the sheet closes and it goes on."""
        if self._pages.currentIndex() == 2:
            self._hide_changes()
            return
        if self.run is not None and self.run.progress.cancellable:
            self.run.cancel()
        super().reject()

    def _on_account_stopped(self, writer: str) -> None:
        """The account was let go: what the sheet shows belongs to nobody
        now, so it closes (the status bar says what stopped)."""
        if self._profile.user_pubkey != writer:
            return
        if self.run is not None:
            self.run.changed.disconnect(self.handoff.show_progress)
            self.run = None
        self._pages.setCurrentIndex(0)
        super().reject()

    def done(self, result: int) -> None:  # noqa: N802, Qt's name
        if self.run is not None:
            self.run.changed.disconnect(self.handoff.show_progress)
            if self.run.progress.message is MessageStatus.FAILED:
                self.run.let_go()
            self._writer.in_background(self.run)
        super().done(result)
        # What was opened of the version before goes now, not with the
        # last reference to this sheet (criterion 19).
        self._previous = None
        self.changes.forget()

    def _style(self) -> None:
        r = appearance.roles(self._is_dark)
        self.setStyleSheet(sheet_stylesheet(self._is_dark) + f"""
            #revise_card {{ background: {r.field}; border: 1px solid {r.line};
                border-radius: 10px; }}
            #revise_title {{ font-weight: 600; }}""")
        appearance.surface(self, self._is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self._style()
        self.status.set_dark(is_dark)
        self.handoff.set_dark(is_dark)
        self.changes.set_dark(is_dark)


__all__ = ["DETAILS_CHANGED", "ReviseSheet", "SAME_TEXT", "change_line", "comparable"]
