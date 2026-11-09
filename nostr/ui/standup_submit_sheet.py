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

Einreichen turns the sheet into the handoff (standup_handoff.py), which
shows the submission's progress and forwards the person's choices; the
steps themselves are the table's (nostr/standup/submitting.py). Closed
while the submission is on its way, the sheet lets it go on in the
background (StandupWriter.in_background).
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton, QStackedWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import _, ngettext

from ..avatar_store import AvatarStore
from ..profiles import Profile, ProfileStore
from ..standup import drafting, formats
from ..standup.drafting import ArticleDraft, Choice
from ..standup.trust import KEY_NOT_CONFIRMED, Destination
from . import appearance
from .avatar import color_for_pubkey, initials_for
from .post_list import symbol_pixmap
from .publish_article_dialog import ArticleCard, form_label, form_layout
from .publish_sheet import (
    AccountRow, BottomRow, StatusLine, fit_margins, fit_to_window, make_sheet, scroll_area,
    sheet_stylesheet,
)
from .standup_handoff import HandoffView
from .standup_submit import RoundAnswer, StandupWriter, SubmissionRun

_WIDTH = 780
_LOGO = 20

def publication_logo(name: str, pubkey: str, picture: Optional[QPixmap], size: int) -> QPixmap:
    """A publication's logo as a rounded square: its picture, cropped to
    a square, or a monogram of its name (as Contacts draws one)."""
    out = QPixmap(size * 2, size * 2)
    out.setDevicePixelRatio(2.0)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)
    painter.setClipPath(path)
    if picture is not None and not picture.isNull():
        side = min(picture.width(), picture.height())
        square = picture.copy((picture.width() - side) // 2, (picture.height() - side) // 2,
                              side, side)
        painter.drawPixmap(0, 0, size, size, square)
    else:
        painter.fillPath(path, color_for_pubkey(pubkey))
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(max(8, int(size * 0.45)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(0, 0, size, size, Qt.AlignmentFlag.AlignCenter, initials_for(name))
    painter.end()
    return out


def _and(items: Sequence[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return _("{items} and {last}").format(items=", ".join(items[:-1]), last=items[-1])


def rules_words(rules) -> str:
    """The rules of a publication, as the picker says them under it."""
    if rules and not rules.get("open", False):
        return _("Not taking submissions right now")
    if "article" not in (rules.get("kinds") or ["article"]):
        return _("Takes audio only") if rules.get("kinds") == ["audio"] \
            else _("Takes no articles")
    parts = []
    asked = [str(item.get("label")) for item in rules.get("checklist") or ()
             if isinstance(item, dict) and item.get("level") == "required" and item.get("label")]
    if asked:
        parts.append(_("Asks for {items}").format(items=_and(asked)))
    most = rules.get("max_open_per_writer")
    if formats.is_int(most, low=1):
        parts.append(ngettext("at most {count} open submission",
                              "at most {count} open submissions", most).format(count=most))
    return " · ".join(parts)


def takes_articles(destination: Destination) -> bool:
    rules = destination.key.rules if destination.key is not None else {}
    return "article" in (rules.get("kinds") or ["article"])


def can_choose(destination: Destination) -> bool:
    """Whether the picker offers it: open, for articles. One whose key
    could not be confirmed is offered and says why it cannot be used."""
    return destination.key is None or (destination.usable and takes_articles(destination))


def publish_by(destination: Destination) -> str:
    """Who publishes an accepted article: "writer", "publication" or
    "both" (the writer chooses)."""
    rules = destination.key.rules if destination.key is not None else {}
    by = rules.get("publish_by")
    value = by.get("article") if isinstance(by, dict) else by
    return value if value in ("writer", "publication", "both") else "writer"


def recent_first(destinations: Sequence[Destination], used: Sequence[str]) -> List[Destination]:
    """The publications submitted to before first (``used``, newest
    first), then the others in the server's order."""
    rank = {address: i for i, address in enumerate(used)}
    return sorted(destinations, key=lambda d: rank.get(d.address, len(rank)))


class SubmitSheet(QDialog):
    """See the module docstring."""

    def __init__(self, *, writer: StandupWriter, draft: ArticleDraft, cover: QPixmap,
                 byline: str, private_names: Sequence[str], profile: Profile,
                 store: ProfileStore, avatars: AvatarStore,
                 request_logo: Callable[[str, str], None], is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Submit to a Publication"))
        make_sheet(self)
        self.setMinimumSize(420, 320)
        self._writer = writer
        self._draft = draft
        self._cover = cover
        self._byline = byline
        self._private_names = list(private_names)
        self._store = store
        self._avatars = avatars
        self._request_logo = request_logo
        self._is_dark = is_dark
        self._destinations: List[Destination] = []
        # The round this text would go in at the chosen publication, as
        # StandupWriter.round_for answered: None while asking.
        self._round: Optional[RoundAnswer] = None
        self._asked_for = ""
        self._operator = ""
        self._load_problem = ""
        self.run: Optional[SubmissionRun] = None
        self._build(profile)
        avatars.avatar_added.connect(self._on_logo)
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
        inner.addWidget(self._build_privacy())
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
        self.retry_load = QPushButton(_("Try Again"))
        self.retry_load.setAutoDefault(False)
        self.retry_load.clicked.connect(self._load)
        self.retry_load.hide()
        to = QWidget()
        to_column = QVBoxLayout(to)
        to_column.setContentsMargins(0, 0, 0, 0)
        to_column.setSpacing(4)
        to_column.addWidget(self.picker)
        to_column.addWidget(self.rules)
        to_column.addWidget(self.retry_load, 0, Qt.AlignmentFlag.AlignLeft)
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

    def _build_privacy(self) -> QFrame:
        box = self._privacy_box = QFrame()
        box.setObjectName("privacy")
        column = QVBoxLayout(box)
        column.setContentsMargins(12, 8, 12, 10)
        column.setSpacing(6)
        line = QHBoxLayout()
        self._lock = QLabel()
        line.addWidget(self._lock)
        words = QLabel(_("Content end-to-end encrypted. The server sees the title."))
        words.setWordWrap(True)
        line.addWidget(words, 1)
        self.privacy_toggle = QToolButton()
        self.privacy_toggle.setObjectName("disclosure")
        self.privacy_toggle.setCheckable(True)
        self.privacy_toggle.setText(_("Details"))
        self.privacy_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.privacy_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.privacy_toggle.setAccessibleName(_("Privacy details"))
        self.privacy_toggle.toggled.connect(self._show_privacy)
        line.addWidget(self.privacy_toggle)
        column.addLayout(line)
        self.privacy_lines = QLabel()
        self.privacy_lines.setObjectName("muted")
        self.privacy_lines.setWordWrap(True)
        self.privacy_lines.setTextFormat(Qt.TextFormat.PlainText)
        self.privacy_lines.hide()
        column.addWidget(self.privacy_lines)
        self._paint_privacy()
        return box

    def _paint_privacy(self) -> None:
        r = appearance.roles(self._is_dark)
        self._privacy_box.setStyleSheet(
            f"QFrame#privacy {{ border: 1px solid {r.line}; border-radius: 10px;"
            f" background: {r.field}; }}")
        self._lock.setPixmap(symbol_pixmap("lock", 15, QColor(r.muted)))

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

    def _load(self) -> None:
        self.retry_load.hide()
        self._load_problem = ""
        if not self._writer.server:
            self._show_destinations([], _("The server’s address comes from the association."))
            return
        self._show_destinations([], "", loading=True)
        profile = self.account.profile()
        self._writer.instance(profile, self._on_instance)
        self._writer.destinations(profile, self._on_destinations, self._on_load_failed)

    def _on_instance(self, operator: str) -> None:
        self._operator = operator
        self._show_privacy(self.privacy_toggle.isChecked())

    def _on_destinations(self, destinations: List[Destination], used: Sequence[str]) -> None:
        # Those that cannot be chosen are still listed, dimmed with why.
        none = not any(can_choose(d) for d in destinations)
        self._show_destinations(recent_first(destinations, used),
                                _("No publication takes submissions right now.") if none else "")

    def _on_load_failed(self, _error) -> None:
        self.retry_load.show()
        self._show_destinations([], _("The publications couldn’t be loaded."))

    def _show_destinations(self, destinations: List[Destination], problem: str, *,
                           loading: bool = False) -> None:
        self._destinations = destinations
        self._load_problem = problem
        self.picker.blockSignals(True)
        self.picker.clear()
        model: QStandardItemModel = self.picker.model()
        if loading or not destinations:
            # The reason is said once, above the buttons.
            self.picker.addItem(_("Loading publications …") if loading else _("No publication"))
            self.picker.setEnabled(False)
        else:
            for destination in destinations:
                pubkey = formats.parse_address(destination.address)[0]
                if destination.picture and destination.picture.startswith("https://"):
                    self._request_logo(pubkey, destination.picture)
                label = destination.name
                if not can_choose(destination):
                    label = f"{destination.name} · {rules_words(destination.key.rules)}"
                self.picker.addItem(self._logo_for(destination), label)
                model.item(self.picker.count() - 1).setEnabled(can_choose(destination))
            self.picker.insertSeparator(self.picker.count())
            self.picker.addItem(_("Recently used first"))
            model.item(self.picker.count() - 1).setEnabled(False)
            self.picker.setEnabled(True)
            first = next((i for i, d in enumerate(destinations) if can_choose(d)), -1)
            self.picker.setCurrentIndex(first)
        self.picker.blockSignals(False)
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
        self._show_privacy(self.privacy_toggle.isChecked())
        self._round = None
        self._asked_for = destination.address if destination is not None else ""
        if destination is not None and destination.usable:
            self._writer.round_for(
                self.account.profile(), destination.address, self._draft.d,
                lambda answer, a=destination.address: self._on_round(a, answer))
        self._refresh()

    def _on_round(self, address: str, answer: RoundAnswer) -> None:
        if address == self._asked_for:
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

    def _show_privacy(self, shown: bool) -> None:
        self.privacy_toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                         else Qt.ArrowType.RightArrow)
        self.privacy_lines.setText("\n".join(f"• {line}" for line in self.privacy_words()))
        self.privacy_lines.setVisible(shown)

    def privacy_words(self) -> List[str]:
        """The five lines behind the lock (plan 10.2, MyEditor's wording)."""
        destination = self.destination()
        name = destination.name if destination is not None else _("the publication")
        lines = [_("Only you and the editorial team of {publication} can read it.").format(
                     publication=name),
                 _("The server sees the title, the publication and who takes part, never "
                   "the content.")]
        count = len(self._draft.pictures)
        if count:
            lines.append(ngettext(
                "The picture is encrypted on your media server. It sees when it is fetched, "
                "never what is in it.",
                "The pictures are encrypted on your media server. It sees when they are "
                "fetched, never what is in them.", count))
        lines.append(_("Withdrawing deletes nothing the editorial team already has."))
        lines.append(_("The server of {operator} keeps the submission encrypted.").format(
            operator=self._operator) if self._operator
            else _("The server keeps the submission encrypted."))
        return lines

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
            return [self._round.words]
        rules = destination.key.rules
        return drafting.blockers(
            self._draft, writer=self.account.profile().user_pubkey,
            rights=self.rights.isChecked(), rights_required=bool(rules.get("rights_required")),
            private_names=self._private_names, message=self.message.toPlainText(),
            submitted_to=destination.name if self._round is not None
            and self._round.round is None else "")

    def round(self) -> Optional[int]:
        """The round to submit in, once known and free."""
        return self._round.round if self._round is not None else None

    def _refresh(self, *_args) -> None:
        blockers = self.blockers()
        self.submit_button.setEnabled(not blockers and self.round() is not None)
        self.status.say(blockers[0] if blockers else "")

    # -- submitting ----------------------------------------------------------

    def _on_submit(self) -> None:
        choice = self.choice()
        if choice is None or self.round() is None or self.blockers():
            return
        run = self._writer.submit(self.account.profile(), self._draft, choice, self._round)
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
        run.changed.connect(self.handoff.show_progress)
        self._pages.setCurrentIndex(1)
        self.handoff.show_progress(run.progress)
        self.handoff.setFocus()

    def _cancel_run(self) -> None:
        if self.run is not None and self.run.progress.cancellable:
            self.run.cancel()
            self.reject()

    def done(self, result: int) -> None:  # noqa: N802, Qt's name
        if self.run is not None:
            self.run.changed.disconnect(self.handoff.show_progress)
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
        self._paint_privacy()


__all__ = ["SubmitSheet", "publication_logo", "recent_first", "rules_words"]
