# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Submit to a Publication: a sheet on the document's window that hands
the article to a STANDUP publication, end to end encrypted (plan 10.2;
the prototype MyEditor_Einreichen_Prototyp).

    ┌──────────────┐  An:                 [21] Einundzwanzig Magazin  ▾
    │   [ cover ]  │                      Verlangt Titel … · höchstens 3 …
    │ Ein Jahr mit │  Veröffentlicht von: (•) mir  ( ) der Publikation
    │ Artikel ·    │  Rechte:             [ ] Ich habe die Rechte an Text …
    │ 1 Bild       │                      ▸ Nachricht an die Redaktion
    └──────────────┘
    [lock] Inhalt Ende-zu-Ende verschlüsselt. Den Titel sieht der Server.  Details ▸
    Einreichen als (J) Jana Roth ▾             [Abbrechen] [Einreichen]

It is built from the parts of Publish as Article (publish_sheet.py) and
looks the same, but is separate from it: submitting is private and can
be taken back, publishing is public.

**An** lists the publications open for submissions, the ones submitted
to before first, each with its logo (its profile picture, or a monogram),
and the rules of the chosen one under it; one that takes no articles is
dimmed with the reason. **Veröffentlicht von** shows only when the
publication lets the writer choose; the rights only when it asks for
them. **Einreichen** is the default button, dimmed only by a blocker,
whose reason is said above it (drafting.blockers, the Redaktion's key
not confirmed, the server's address not known yet: "kommt vom Verein").

The publications' logos and rules are :mod:`.standup_publications`'s,
the lock line and its Details :mod:`.standup_privacy`'s.

Einreichen turns the sheet into the handoff (standup_handoff.py), which
shows the submission's progress and forwards the person's choices; the
steps themselves are the table's (nostr/standup/submitting.py). Escape
is Abbrechen while the submission can still be taken back; Im
Hintergrund fortsetzen, or Escape once the request is on its way,
closes the sheet and lets it go on in the background
(StandupWriter.in_background).
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence

import shiboken6
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton, QStackedWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import _

from ..avatar_store import AvatarStore
from ..profiles import Profile, ProfileStore
from ..signer_limits import signable
from ..standup import drafting, formats, words
from ..standup.drafting import ArticleDraft, Choice
from ..standup.errors import Code
from ..standup.submitting import MessageStatus
from ..standup.trust import KEY_NOT_CONFIRMED, Destination
from ..failures import CHECK_CONNECTION
from ..standup.words import LIST_NOT_SAVED, WAITING_FOR_SIGNER
from . import appearance
from .publish_article_dialog import ArticleCard, form_label, form_layout
from .publish_sheet import (
    AccountRow, BottomRow, StatusLine, fit_margins, fit_to_window, make_sheet, scroll_area,
    sheet_stylesheet,
)
from .standup_handoff import HandoffView
from .standup_waits import limit_words, server_limit_words
from .standup_ways_out import WaysOut
from .standup_privacy import PrivacyBox, privacy_words
from .standup_publications import (
    can_choose, fill_picker, publication_logo, publish_by, recent_first, rules_words,
)
from .standup_submit import RoundAnswer, StandupWriter, SubmissionRun

_WIDTH = 780
_LOGO = 20


def _now() -> float:
    return time.time()


class SubmitSheet(QDialog):
    """See the module docstring. ``submitted(d)`` when the work goes to a
    publication under ``d``: the tab keeps it as its identifier."""

    submitted = Signal(str)

    def __init__(self, *, writer: StandupWriter, draft: ArticleDraft, cover: QPixmap,
                 byline: str, private_names: Sequence[str], profile: Profile,
                 store: ProfileStore, avatars: AvatarStore,
                 request_logo: Callable[[str, str], None], is_dark: bool,
                 public_pictures: int = 0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Submit to a Publication"))
        make_sheet(self)
        self.setMinimumSize(420, 320)
        self._writer = writer
        self._draft = draft
        self._cover = cover
        self._byline = byline
        self._private_names = list(private_names)
        # Pictures that are public links already: they stay public.
        self._public_pictures = max(0, int(public_pictures))
        self._store = store
        self._avatars = avatars
        self._request_logo = request_logo
        self._is_dark = is_dark
        self._destinations: List[Destination] = []
        # The round this text would go in at the chosen publication, as
        # StandupWriter.round_for answered: None while asking.
        self._round: Optional[RoundAnswer] = None
        self._asked_for: object = None
        # Every request this sheet sends is answered through _answer: it
        # counts only while the sheet is open on the account it asked for.
        self._generation = 0
        self._operator = ""
        self._load_problem = ""
        # The publications could not be loaded: Try Again loads them.
        self._load_failed = False
        # A limit of the server met by loading them: Try Again is dimmed
        # until then (contract 15, RG-M2r2a 5), as for the round's lookups.
        self._load_limited = False
        self._load_retry_at: Optional[float] = None
        # What the account's signer connection carries is known (or could
        # not be learned): the message's seal is measured by it.
        self._bound_known = False
        self.run: Optional[SubmissionRun] = None
        self._build(profile)
        avatars.avatar_added.connect(self._on_logo)
        writer.stopped.connect(self._on_account_stopped)
        running = writer.running(profile, draft.d)
        if running is not None:
            # One run per work: the one on its way is shown, not a second.
            self._show_run(running)
        else:
            self._load()
        fit_to_window(self, QSize(_WIDTH, self._wanted_height()))

    # -- the sheet -----------------------------------------------------------

    def _build(self, profile: Profile) -> None:
        self.setStyleSheet(sheet_stylesheet(self._is_dark))
        appearance.surface(self, self._is_dark)
        appearance.bind(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)
        heading = QLabel(_("Submit to a Publication"))
        heading.setObjectName("title")
        root.addWidget(heading)
        self._pages = QStackedWidget()
        root.addWidget(self._pages, 1)
        self._pages.addWidget(self._form_page(profile))
        self.handoff = HandoffView(is_dark=self._is_dark)
        self.handoff.cancel_requested.connect(self._cancel_run)
        self.handoff.retry_requested.connect(lambda: self.run and self.run.retry())
        self.handoff.retry_message_requested.connect(lambda: self.run and self.run.retry_message())
        self.handoff.close_requested.connect(self.accept)
        self.handoff.background_requested.connect(self.accept)
        handoff_page = QWidget()
        column = QVBoxLayout(handoff_page)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.handoff, 1)
        column.addWidget(self.handoff.buttons)
        self._pages.addWidget(handoff_page)
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def _form_page(self, profile: Profile) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        content = QWidget()
        inner = QVBoxLayout(content)
        inner.setContentsMargins(0, 0, 4, 0)
        inner.setSpacing(12)
        columns = QHBoxLayout()
        columns.setSpacing(20)
        self.card = ArticleCard(self._is_dark)
        self.card.show_article(self._draft.title, self._draft.summary, self._cover,
                               self._byline)
        columns.addWidget(self.card, 0, Qt.AlignmentFlag.AlignTop)
        columns.addLayout(self._build_form(), 1)
        inner.addLayout(columns)
        self.privacy = PrivacyBox(self.privacy_words, is_dark=self._is_dark)
        self.privacy_toggle, self.privacy_lines = self.privacy.toggle, self.privacy.lines
        inner.addWidget(self.privacy)
        inner.addStretch(1)
        self._scroll = scroll_area(content)
        column.addWidget(self._scroll, 1)
        self.status = StatusLine(self._is_dark)
        column.addWidget(self.status)
        self.account = AccountRow(profile, self._store, self._avatars, is_dark=self._is_dark,
                                  label=_("Submitting as"), accessible=_("Submitting as {name}"))
        # Another account submits: its publications, its list, its round.
        self.account.switched.connect(lambda _profile: self._load())
        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.submit_button = buttons.addButton(_("Submit"),
                                               QDialogButtonBox.ButtonRole.AcceptRole)
        self.submit_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.submit_button.clicked.connect(self._on_submit)
        column.addWidget(BottomRow(self.account, buttons))
        return page

    def _build_form(self):
        form = form_layout()
        self.picker = QComboBox()
        self.picker.setAccessibleName(_("Publication"))
        self.picker.setIconSize(QSize(_LOGO, _LOGO))
        self.picker.currentIndexChanged.connect(self._on_picked)
        self.rules = self._hint()
        # Every answer that leaves the sheet waiting has its way out
        # (lesson 12), and an earlier try not sent its Discard.
        self.ways_out = WaysOut(self._hint(), now=lambda: _now())
        self.ways_out.retry_requested.connect(self._try_again)
        self.ways_out.discard_requested.connect(self._on_discard)
        self.ways_out.wait_over.connect(self._refresh)
        self.retry_load, self.unsent = self.ways_out.retry, self.ways_out.unsent
        self.discard_button, self._wait = self.ways_out.discard, self.ways_out.timer
        to = QWidget()
        to_column = QVBoxLayout(to)
        to_column.setContentsMargins(0, 0, 0, 0)
        to_column.setSpacing(4)
        to_column.addWidget(self.picker)
        to_column.addWidget(self.rules)
        to_column.addWidget(self.ways_out)
        form.addRow(form_label(_("To:"), self.picker), to)

        self.by_me = QRadioButton(_("me"))
        self.by_publication = QRadioButton(_("the publication"))
        self.by_me.setChecked(True)
        group = QButtonGroup(self)
        for button in (self.by_me, self.by_publication):
            group.addButton(button)
            button.toggled.connect(self._show_by_hint)
        self.by_hint = self._hint()
        by = QWidget()
        by_column = QVBoxLayout(by)
        by_column.setContentsMargins(0, 0, 0, 0)
        by_column.setSpacing(4)
        radios = QHBoxLayout()
        radios.addWidget(self.by_me)
        radios.addWidget(self.by_publication)
        radios.addStretch(1)
        by_column.addLayout(radios)
        by_column.addWidget(self.by_hint)
        self._by_label = form_label(_("Published by:"), self.by_me)
        form.addRow(self._by_label, by)
        self._by_row = form.rowCount() - 1

        self.rights = QCheckBox(_("I have the rights to the text and the pictures."))
        self.rights.toggled.connect(self._refresh)
        form.addRow(form_label(_("Rights:"), self.rights), self.rights)
        self._rights_row = form.rowCount() - 1

        self.message_toggle = QToolButton()
        self.message_toggle.setObjectName("disclosure")
        self.message_toggle.setCheckable(True)
        self.message_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.message_toggle.setText(_("Message to the Editorial Team"))
        self.message_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.message_toggle.toggled.connect(self._show_message)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText(_("Optional. For example, why the text suits you."))
        self.message.setAccessibleName(_("Message to the Editorial Team"))
        self.message.setFixedHeight(72)
        self.message.textChanged.connect(self._refresh)
        self.message.hide()
        message = QWidget()
        message_column = QVBoxLayout(message)
        message_column.setContentsMargins(0, 0, 0, 0)
        message_column.setSpacing(6)
        message_column.addWidget(self.message_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        message_column.addWidget(self.message)
        form.addRow("", message)
        self._form = form
        return form

    def _on_discard(self) -> None:
        """Verwerfen: the earlier try the server has no receipt for taken
        off the list (a keyed tombstone, contract 13)."""
        entry = self._round.unsent if self._round is not None else None
        if entry is None:
            return
        self.discard_button.setEnabled(False)

        def done(stored: bool) -> None:
            self.discard_button.setEnabled(True)
            if stored:
                self._on_picked(self.picker.currentIndex())     # ask again
            else:
                # Said once, Discard there to try again (lessons 36).
                self.status.say(_(LIST_NOT_SAVED), title=_(words.NOT_DISCARDED))

        self._writer.discard(self.account.profile(), entry, self._answer(done))

    @staticmethod
    def _hint() -> QLabel:
        label = QLabel()
        label.setObjectName("help")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def _wanted_height(self) -> int:
        return min(640, max(460, self.sizeHint().height()))

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    # -- the publications ----------------------------------------------------

    def _answer(self, handler: Callable) -> Callable:
        """``handler`` for an answer to a request sent now: dropped once
        the sheet closed (or is gone) or shows another account."""
        generation = self._generation

        def answer(*args) -> None:
            if shiboken6.isValid(self) and self._generation == generation:
                handler(*args)

        return answer

    def _load(self) -> None:
        self._generation += 1                       # the account asked for now
        self._load_failed = self._load_limited = False
        self._load_problem = ""
        self._load_retry_at = None
        if not self._writer.server:
            self._show_destinations([], _("The server’s address comes from the association."))
            return
        self._show_destinations([], "", loading=True)
        profile = self.account.profile()
        self._bound_known = False
        self._writer.signer_bound(profile, self._answer(self._on_bound))
        self._writer.instance(profile, self._answer(self._on_instance))
        self._writer.destinations(profile, self._answer(self._on_destinations),
                                  self._answer(self._on_load_failed))

    def _on_bound(self) -> None:
        self._bound_known = True
        self._refresh()

    def _on_instance(self, operator: str) -> None:
        self._operator = operator
        self.privacy.refresh()

    def _on_destinations(self, destinations: List[Destination], used: Sequence[str]) -> None:
        # Those that cannot be chosen are still listed, dimmed with why.
        none = not any(can_choose(d) for d in destinations)
        self._show_destinations(recent_first(destinations, used),
                                _("No publication takes submissions right now.") if none else "")

    def _on_load_failed(self, error) -> None:
        # A limit: asked again at once, it meets the same one (contract 15).
        limited = getattr(error, "code", None) == Code.RATE_LIMITED
        self._load_failed, self._load_limited = True, limited
        wait = error.retry_after if limited else None
        self._load_retry_at = _now() + wait if wait else None
        self._show_destinations([], "")

    def _try_again(self) -> None:
        """The publications loaded again, or the round asked again (the
        list of submissions read again first when it could not tell);
        never while a limit's wait is not over."""
        if self.ways_out.waiting():
            return
        if self._load_failed:
            self._load()
            return
        if self._round is not None and self._round.from_list:
            self._writer.reload(self.account.profile())
        self._on_picked(self.picker.currentIndex())

    def _show_destinations(self, destinations: List[Destination], problem: str, *,
                           loading: bool = False) -> None:
        self._destinations = destinations
        self._load_problem = problem
        fill_picker(self.picker, destinations, loading=loading, logo_for=self._logo_for,
                    request_logo=self._request_logo)
        self._on_picked(self.picker.currentIndex())

    def _logo_for(self, destination: Destination, size: int = _LOGO) -> QPixmap:
        pubkey = formats.parse_address(destination.address)[0]
        return publication_logo(destination.name, pubkey, self._avatars.get(pubkey), size)

    def _on_logo(self, pubkey: str, _picture) -> None:
        for index, destination in enumerate(self._destinations):
            if formats.parse_address(destination.address)[0] == pubkey:
                self.picker.setItemIcon(index, self._logo_for(destination))

    def destination(self) -> Optional[Destination]:
        index = self.picker.currentIndex()
        return self._destinations[index] if 0 <= index < len(self._destinations) else None

    def _on_picked(self, _index: int) -> None:
        destination = self.destination()
        rules = destination.key.rules if destination is not None and destination.key else {}
        self.rules.setText(rules_words(rules) if destination is not None else "")
        self.rules.setVisible(bool(self.rules.text()))
        self._form.setRowVisible(self._by_row, destination is not None
                                 and publish_by(destination) == "both")
        self._form.setRowVisible(self._rights_row, bool(rules.get("rights_required")))
        self._show_by_hint()
        self.privacy.refresh()
        self._round = None
        asked = self._asked_for = object()          # only the newest question counts
        if destination is not None and destination.usable:
            self._writer.round_for(
                self.account.profile(), destination.address, self._draft.d,
                self._answer(lambda answer: self._on_round(asked, answer)))
        self._refresh()

    def _on_round(self, asked: object, answer: RoundAnswer) -> None:
        if asked is self._asked_for:
            self._round = answer
            self._refresh()

    # -- what the person chooses ---------------------------------------------

    def _show_by_hint(self, *_args) -> None:
        self.by_hint.setText(
            _("After acceptance you publish it with your key.") if self.by_me.isChecked()
            else _("At acceptance the editorial team proposes how zaps are split; "
                   "you confirm it."))

    def _show_message(self, shown: bool) -> None:
        self.message_toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                         else Qt.ArrowType.RightArrow)
        self.message.setVisible(shown)
        if shown:
            self.message.setFocus()

    def privacy_words(self) -> List[str]:
        """The lines behind the lock, for the chosen publication."""
        destination = self.destination()
        return privacy_words(destination.name if destination is not None else "",
                             len(self._draft.pictures), self._public_pictures, self._operator)

    def choice(self) -> Optional[Choice]:
        destination = self.destination()
        if destination is None:
            return None
        by = publish_by(destination)
        signer = ("publication" if by == "publication"
                  or (by == "both" and self.by_publication.isChecked()) else "writer")
        return Choice(publication=destination.address, name=destination.name, signer=signer,
                      rights=self.rights.isChecked(), message=self.message.toPlainText())

    def blockers(self) -> List[str]:
        """Why Einreichen is dimmed, most pressing first; empty while
        there is nothing to say yet (the round is still being asked)."""
        if self._load_failed:
            # Said once, with its title; Try Again beside it (lessons 36).
            sentence = (server_limit_words(self._load_retry_at, _now()) if self._load_limited
                        else _(CHECK_CONNECTION))
            return [drafting.Blocker(sentence, title=_(words.PUBLICATIONS_NOT_LOADED))]
        if self._load_problem:
            return [self._load_problem]
        destination = self.destination()
        if destination is None:
            return []
        if destination.key is None:
            return [_(KEY_NOT_CONFIRMED)]
        if not can_choose(destination):
            return [rules_words(destination.key.rules)]
        if self._round is not None and self._round.words:
            return [drafting.Blocker(self._round.words, title=_(self._round.title),
                                     error=self._round.elsewhere is None)]
        if self._round is not None and self._round.limited:
            return [drafting.Blocker(limit_words(destination.name, self._round.retry_at, _now()),
                                     title=_(words.NOT_CHECKED))]
        rules = destination.key.rules
        found = drafting.blockers(
            self._draft, writer=self.account.profile().user_pubkey,
            rights=self.rights.isChecked(), rights_required=bool(rules.get("rights_required")),
            private_names=self._private_names, message=self.message.toPlainText(),
            submitted_to=destination.name if self._round is not None
            and self._round.round is None else "",
            checklist=rules.get("checklist"), publication=destination.name,
            signable=lambda unsigned: signable(self.account.profile(), unsigned))
        if not found and self.message.toPlainText().strip() and not self._bound_known:
            # The message's seal is measured over the relays of the signer
            # connection, read as the signer is got (_load): until then
            # the measure would not be the one that holds (lesson 29).
            return [_(WAITING_FOR_SIGNER)]
        return found

    def round(self) -> Optional[int]:
        """The round to submit in, once known and free."""
        return self._round.round if self._round is not None else None

    def _retry_at(self) -> Optional[float]:
        """When a limit's wait for Try Again is over (clock time), or None."""
        if self._load_failed:
            return self._load_retry_at
        return self._round.retry_at if self._round is not None and self._round.limited \
            else None

    def _refresh(self, *_args) -> None:
        round_ = self._round
        # A round step that met its bounds meets them again (final).
        self.ways_out.show_ways(
            retry=self._load_failed or (round_ is not None and not round_.final
                                        and (bool(round_.words) or round_.limited)),
            until=self._retry_at(), answer=round_)
        blockers = self.blockers()
        self.submit_button.setEnabled(not blockers and self.round() is not None)
        first = next(iter(blockers), "")
        first = first if isinstance(first, drafting.Blocker) else drafting.Blocker(first)
        # A total failure (it has a title) in the error color (lessons 36);
        # what exactly is wrong (a key, a file's type) in the tooltip (27).
        self.status.say(first, title=first.title, error=first.error, detail=first.detail)
        self.status.setAccessibleDescription(first.detail)

    # -- submitting ----------------------------------------------------------

    def _on_submit(self) -> None:
        choice = self.choice()
        if choice is None or self.round() is None or self.blockers():
            return
        run = self._writer.submit(self.account.profile(), self._draft, choice, self._round)
        self.submitted.emit(run.draft.d)
        self._show_run(run)
        run.start()

    def _show_run(self, run: SubmissionRun) -> None:
        """The handoff of ``run``, from where it is now."""
        self.run = run
        name, address = run.choice.name, run.choice.publication
        self.handoff.set_work(title=self._draft.title, summary=self._draft.summary,
                              byline=self._byline, cover=self._cover, publication=name,
                              logo=publication_logo(name, formats.parse_address(address)[0],
                                                    self._avatars.get(
                                                        formats.parse_address(address)[0]), 40))
        run.behind_the_sheet(None)                  # the sheet says it now
        run.changed.connect(self.handoff.show_progress)
        self._pages.setCurrentIndex(1)
        self.handoff.show_progress(run.progress)
        self.handoff.setFocus()

    def _cancel_run(self) -> None:
        if self.run is not None and self.run.progress.cancellable:
            self.reject()

    def reject(self) -> None:  # noqa: N802, Qt's name
        """Cancel and Escape, which is Cancel (HIG): a run that may still
        be cancelled is cancelled. Once a request went out nothing is
        cancelled (the server may hold it), and the sheet closes with the
        run going on, as Continue in Background does (RG-M2b 10)."""
        if self.run is not None and self.run.progress.cancellable:
            self.run.cancel()
        super().reject()

    def _on_account_stopped(self, writer: str) -> None:
        """The account this sheet submits as was let go (sign-out, another
        account, a signer paired anew): what it shows belongs to nobody
        now, so it closes; the status bar says what stopped (RG-M2b 9).
        An account chosen in this sheet lets the previous one go too, and
        the sheet stays."""
        if self.account.profile().user_pubkey != writer:
            return
        if self.run is not None:
            self.run.changed.disconnect(self.handoff.show_progress)
            self.run = None                         # stopped, not in the background
        self.reject()

    def done(self, result: int) -> None:  # noqa: N802, Qt's name
        self._generation += 1                       # no answer counts after this
        if self.run is not None:
            self.run.changed.disconnect(self.handoff.show_progress)
            if self.run.progress.message is MessageStatus.FAILED:
                self.run.let_go()                   # Send Again was there
            self._writer.in_background(self.run)
        super().done(result)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(sheet_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        self.card.set_dark(is_dark)
        self.account.set_dark(is_dark)
        self.status.set_dark(is_dark)
        self.handoff.set_dark(is_dark)
        self.privacy.set_dark(is_dark)


__all__ = ["SubmitSheet", "publication_logo", "recent_first", "rules_words"]
