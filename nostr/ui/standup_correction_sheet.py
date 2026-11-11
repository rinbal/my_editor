# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Korrektur von <Publikation>: the writer compares a correction the
editorial team suggests for a published article, and takes it over
(M-4 M15; plan 8.6; the prototype's correction sheet).

    Korrektur von Einundzwanzig Magazin
    „Zwei Zahlen korrigiert."                    (the correction's note)
    Der Titel wird zu »…«. Das Titelbild ist ein anderes.
    ┌ the published text beside the correction, word by word ┐
    Übernimmst du sie, ersetzt die Korrektur den Artikel unter
    derselben Adresse. Das Datum der Veröffentlichung bleibt.
    [Rückfrage stellen]          [Später] [Korrektur übernehmen]

The text is compared with the article as it stands in public, never
with an earlier correction (RG-M4-a 6); every other public field the
correction changes (title, summary, cover, topics, language) is said in
plain words above it (RG-M4-a 1). Nothing else of the published article
changes: its zaps, credits and carried tags stay.

The run (:class:`~.standup_publish.CorrectionPublish`) is the account's
and bound to the correction's verified revision: it confirms the
correction alone, then publishes it again under the writer's key. Once
it is taken the comparison closes and the sheet says so. The writer
does not edit a correction here, so closing it never discards anything.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtCore import QLocale, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import N_, _

from . import appearance
from .publish_sheet import StatusLine, fit_margins, fit_to_window, make_sheet, sheet_stylesheet
from .standup_compare import CompareView, Comparer
from .standup_publish import CorrectionPublish, Stage

REPLACES = N_("If you take it, the correction replaces the article at the same address. "
              "The date of publication stays.")
ASK_PREFIX = N_("A short question about the correction: ")
# What else the correction changes, one plain sentence each (RG-M4-a 1).
TITLE_BECOMES = N_("The title becomes “{title}”.")
SUMMARY_BECOMES = N_("The summary becomes “{summary}”.")
SUMMARY_GOES = N_("The summary is removed.")
COVER_ADDED = N_("It gets a cover picture.")
COVER_CHANGED = N_("The cover picture is a different one.")
COVER_GOES = N_("The cover picture is removed.")
TOPICS_BECOME = N_("The topics become {topics}.")
TOPICS_GO = N_("The topics are removed.")
LANGUAGE_BECOMES = N_("The language becomes {language}.")


def change_words(changes: Sequence[Tuple[str, str]]) -> str:
    """The sentences (translated) for ``publishing.correction_changes``."""
    out = []
    for field, value in changes:
        if field == "title":
            out.append(_(TITLE_BECOMES).format(title=value))
        elif field == "summary":
            out.append(_(SUMMARY_BECOMES).format(summary=value) if value else _(SUMMARY_GOES))
        elif field == "cover":
            out.append(_(COVER_ADDED) if value == "added" else _(COVER_GOES)
                       if value == "removed" else _(COVER_CHANGED))
        elif field == "topics":
            out.append(_(TOPICS_BECOME).format(topics=value) if value else _(TOPICS_GO))
        elif field == "language":
            name = QLocale(value).nativeLanguageName() if value else ""
            out.append(_(LANGUAGE_BECOMES).format(language=name or value))
    return " ".join(out)


class CorrectionSheet(QDialog):
    """See the module docstring. ``old`` and ``new`` are the texts to
    compare (comparable already), ``note`` the correction's own line,
    ``changes`` what else it changes (``publishing.correction_changes``)."""

    ask_requested = Signal(str)              # the question's first words

    def __init__(self, *, run: CorrectionPublish, publication: str, old: str, new: str,
                 note: str, comparer: Comparer, is_dark: bool,
                 changes: Sequence[Tuple[str, str]] = (),
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        heading = _("Correction from {publication}").format(publication=publication)
        self.setWindowTitle(heading)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        make_sheet(self)
        self.setMinimumSize(460, 360)
        self.run, self._is_dark = run, bool(is_dark)
        self._style()
        appearance.bind(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)
        title = QLabel(heading)
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        root.addWidget(title)
        self.note = QLabel(f"“{note}”" if note else "")
        self.note.setObjectName("help")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        self.note.setVisible(bool(note))
        root.addWidget(self.note)
        self.fields = QLabel(change_words(changes))
        self.fields.setTextFormat(Qt.TextFormat.PlainText)
        self.fields.setWordWrap(True)
        self.fields.setVisible(bool(changes))
        root.addWidget(self.fields)
        self.changes = CompareView(is_dark=self._is_dark)
        root.addWidget(self.changes, 1)
        self.changes.show_pair(comparer, old, new)
        self.replaces = QLabel(_(REPLACES))
        self.replaces.setObjectName("help")
        self.replaces.setWordWrap(True)
        root.addWidget(self.replaces)
        self.status = StatusLine(self._is_dark)
        root.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.ask = buttons.addButton(_("Ask a Question"), QDialogButtonBox.ButtonRole.ActionRole)
        self.ask.setAutoDefault(False)
        self.ask.clicked.connect(self._ask)
        self.later = buttons.addButton(_("Later"), QDialogButtonBox.ButtonRole.RejectRole)
        self.later.clicked.connect(self.reject)
        self.take = buttons.addButton(_("Take the Correction"),
                                      QDialogButtonBox.ButtonRole.AcceptRole)
        self.take.setDefault(True)
        self.take.clicked.connect(self._primary)
        root.addWidget(buttons)
        answer_return_as_the_system_does(self.findChildren(QPushButton))
        run.changed.connect(self._show)
        self._show()
        fit_to_window(self, QSize(640, 560))

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    def _show(self) -> None:
        run, stage = self.run, self.run.stage
        working, done = stage is Stage.WORKING, stage is Stage.DONE
        failed = stage in (Stage.FAILED, Stage.NOT_STORED, Stage.NOT_REPORTED)
        if done:
            self.status.say(run.title)
        elif failed or stage is Stage.OFFER_HERE:
            self.status.say(run.words, error=True, title=run.title)
        elif working:
            self.status.say(" ".join(part for part in (run.title, run.words) if part),
                            quietly=True)
        else:
            self.status.say("")
        # Taken, the comparison closes (RG-F5a-b 7).
        self.changes.setVisible(not done)
        self.replaces.setVisible(not done)
        self.ask.setVisible(not done)
        self.take.setText(_("Done") if done else _("Try Again") if failed else _(
            "Sign with the Key on This Computer") if stage is Stage.OFFER_HERE
            else _("Take the Correction"))
        self.take.setEnabled(not working)
        self.later.setText(_("Close") if working or failed else _("Later"))
        self.later.setVisible(not done)

    def _primary(self) -> None:
        stage = self.run.stage
        if stage is Stage.DONE:
            self.accept()
        elif stage is Stage.OFFER_HERE:
            self.run.start(here=True)
        elif stage in (Stage.NOT_STORED, Stage.NOT_REPORTED, Stage.FAILED):
            self.run.retry()
        else:
            self.run.start()

    def _ask(self) -> None:
        self.ask_requested.emit(_(ASK_PREFIX))
        self.reject()

    def done(self, result: int) -> None:
        super().done(result)
        self.changes.forget()

    def _style(self) -> None:
        self.setStyleSheet(sheet_stylesheet(self._is_dark))
        appearance.surface(self, self._is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self._style()
        self.status.set_dark(is_dark)
        self.changes.set_dark(is_dark)


__all__ = ["CorrectionSheet", "REPLACES"]
