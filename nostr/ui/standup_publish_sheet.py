# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Als Artikel veröffentlichen: the sheet of an accepted submission that
the writer publishes (M-4 M13; the prototype's publish sheet).

    Als Artikel veröffentlichen
    Von Einundzwanzig Magazin angenommen, Fassung 2.
    ┌ Ein Jahr mit Lightning im Café ───────────────────┐
    │ Zusammenfassung · #bitcoin #lightning · Nur du    │
    └───────────────────────────────────────────────────┘
    Text und Bild wie abgegeben.
    Bisher privat. Danach für alle lesbar, Text und Bilder
    unverschlüsselt. Zurückholen geht dann nicht mehr.
    Veröffentlichen als Lena            [Abbrechen] [Veröffentlichen]

Everything shown is the accepted version's, read-only: the sheet is the
deliberate step (no hold after it). When the text in the editor differs
from the accepted version it says, in the prototype's words, that the
accepted version is published and that the Redaktion sees a later
change as changed since acceptance (RG-M4-b 1: no version can be handed
in after acceptance, contract 11.3). The run is the account's
(:class:`~.standup_publish.AcceptedPublish`): closing the sheet while it
works lets it go on, and opening it again shows where it is.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import N_, _

from . import appearance
from .publish_sheet import StatusLine, fit_margins, fit_to_window, make_sheet, sheet_stylesheet
from .standup_publish import AcceptedPublish, Stage

ACCEPTED_LINE = N_("Accepted by {publication}, version {number}.")
PLACE_LINE = N_("It then gets its place in “{section}”.")
AS_HANDED_IN = N_("Text and pictures as handed in.")
PUBLIC_AFTER = N_("Until now it’s private. Once published, everyone can read it, text and "
                  "pictures unencrypted. It can’t be taken back.")
DIFFERS = N_("The accepted version is published. If you change anything afterwards, the "
             "editorial team sees “changed since acceptance”.")
ONLY_YOU = N_("Only you")
ASK_PREFIX = N_("A short question about publishing: ")
SIGN_HERE = N_("Sign with the Key on This Computer")


class PublishSheet(QDialog):
    """See the module docstring. ``people`` are the names of everyone
    credited besides the writer; ``differs``: the editor's text is not
    the accepted version's."""

    view_requested = Signal(str)             # naddr
    ask_requested = Signal(str)              # the question's first words

    def __init__(self, *, run: AcceptedPublish, publication: str, number: int,
                 section: str = "", account_name: str = "", people: Sequence[str] = (),
                 differs: bool = False, is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Publish as an Article"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        make_sheet(self)
        self.setMinimumSize(420, 300)
        self.run, self._is_dark = run, bool(is_dark)
        self._build(publication, number, section, account_name, people, differs)
        run.changed.connect(self._show)
        self._show()
        fit_to_window(self, QSize(560, min(520, max(360, self.sizeHint().height()))))

    def _label(self, text: str, name: str = "help") -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def _build(self, publication, number, section, account_name, people, differs) -> None:
        self._style()
        appearance.bind(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)
        root.addWidget(self._label(_("Publish as an Article"), "title"))
        line = _(ACCEPTED_LINE).format(publication=publication, number=number)
        if section:
            line += " " + _(PLACE_LINE).format(section=section)
        self.accepted_line = self._label(line, "")
        root.addWidget(self.accepted_line)
        work = self.run.revision.get("work") or {}
        card = QFrame()
        card.setObjectName("publish_card")
        column = QVBoxLayout(card)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(2)
        title = self._label(str(work.get("title") or ""), "publish_title")
        column.addWidget(title)
        summary = str(work.get("summary") or "").strip()
        if summary:
            column.addWidget(self._label(summary))
        tags = " ".join("#" + str(t).lstrip("#") for t in work.get("tags") or () if t)
        if tags:
            column.addWidget(self._label(tags))
        self.people = self._label("")
        column.addWidget(self.people)
        self.show_people(people)
        card.setAccessibleName(str(work.get("title") or ""))
        root.addWidget(card)
        self.differs = self._label(_(DIFFERS))
        self.differs.setVisible(bool(differs))
        root.addWidget(self.differs)
        self.hint = self._label(_(AS_HANDED_IN))
        self.hint.setVisible(not differs)
        root.addWidget(self.hint)
        self.privacy = self._label(_(PUBLIC_AFTER))
        root.addWidget(self.privacy)
        root.addStretch(1)
        self.status = StatusLine(self._is_dark)
        root.addWidget(self.status)
        self.signing_as = self._label(
            _("Publishing as {name}").format(name=account_name) if account_name else "")
        self.signing_as.setVisible(bool(account_name))
        root.addWidget(self.signing_as)
        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.clicked.connect(self.reject)
        self.view = buttons.addButton(_("View"), QDialogButtonBox.ButtonRole.ActionRole)
        self.view.setAutoDefault(False)
        self.view.clicked.connect(lambda: self.view_requested.emit(self.run.naddr))
        self.publish = buttons.addButton(_("Publish"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.publish.setDefault(True)
        self.publish.clicked.connect(self._primary)
        root.addWidget(buttons)
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def show_people(self, people: Sequence[str]) -> None:
        """The names of everyone credited besides the writer (again when
        a profile's name came in late)."""
        self.people.setText(", ".join(people) if people else _(ONLY_YOU))

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    # -- what the run says ---------------------------------------------------------

    def _show(self) -> None:
        run, stage = self.run, self.run.stage
        working = stage is Stage.WORKING
        done = stage is Stage.DONE
        failed = stage in (Stage.FAILED, Stage.NOT_STORED, Stage.NOT_REPORTED,
                           Stage.NOT_CHECKED)
        ask = stage is Stage.SOMEONE_ELSES      # nothing to try again: ask the Redaktion
        if done:
            self.status.say(run.title)
        elif failed or ask or stage is Stage.OFFER_HERE:
            self.status.say(run.words, error=True, title=run.title)
        elif working:
            self.status.say(" ".join(part for part in (run.title, run.words) if part),
                            quietly=True)
        else:
            self.status.say("")
        self.publish.setText(_("Done") if done else _(SIGN_HERE) if stage is Stage.OFFER_HERE
                             else _("Ask a Question") if ask
                             else _("Try Again") if failed else _("Publish"))
        self.publish.setEnabled(not working)
        self.view.setVisible(done or stage is Stage.NOT_REPORTED)
        self.cancel_button.setVisible(not done)
        # Once it works, Cancel only closes: the run goes on (it is the
        # account's), and the detail shows where it is.
        # Not checked: Erneut versuchen or Abbrechen (12.4, the words both apps say).
        self.cancel_button.setText(_("Cancel") if stage is Stage.NOT_CHECKED else _(
            "Close") if working or failed or ask else _("Cancel"))

    def _primary(self) -> None:
        stage = self.run.stage
        if stage is Stage.DONE:
            self.accept()
        elif stage is Stage.SOMEONE_ELSES:
            self.ask_requested.emit(_(ASK_PREFIX))
            self.reject()
        elif stage is Stage.OFFER_HERE:
            self.run.start(here=True)           # explicit, this article only (Q1)
        elif stage in (Stage.NOT_STORED, Stage.NOT_REPORTED, Stage.FAILED, Stage.NOT_CHECKED):
            self.run.retry()
        else:
            self.run.start()

    # -- the look ----------------------------------------------------------------------

    def _style(self) -> None:
        r = appearance.roles(self._is_dark)
        self.setStyleSheet(sheet_stylesheet(self._is_dark) + f"""
            #publish_card {{ background: {r.field}; border: 1px solid {r.line};
                border-radius: 10px; }}
            #publish_title {{ font-weight: 600; }}""")
        appearance.surface(self, self._is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self._style()
        self.status.set_dark(is_dark)


__all__ = ["ACCEPTED_LINE", "DIFFERS", "PUBLIC_AFTER", "PublishSheet", "SIGN_HERE"]
