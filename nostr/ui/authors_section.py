# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Article Details' Authors: who writes the article, and how much of the
zaps to it each of them receives.

    ┌──────────────────────────────────────┐
    │ Alice · you                     60 % │   everyone's share, once
    │ Bob                             25 % │   (selected)
    │ Carol                           15 % │
    └──────────────────────────────────────┘
    [+] [−]                     Split Evenly
    Share for Bob
    ━━━━━━━●━━━━━━━━━━━━━━━━━━━   [ 25 % ]   the selected person's share
    Apps with zap splits pay each share; others pay you.

A list you add to and remove from, as a Mac's settings have them (Users,
Login Items): the list, then + and − under it, then the controls for the
row selected. Each person's share is said once, in the list; the slider
and the exact value beside it (Apple's guidelines pair a slider with a
field for a precise value) change the selected co-author's. The list is
in the order the article is published with (ArticleDetails.authors),
you first when it names no place for you. You keep what the others
leave, so the shares always add up to 100 % (nostr/zap_shares.py). A co-author is credited and receives nothing
until given a share; one whose profile has no lightning address can't be
paid, and the controls say so. The line under them only says what is
true, and only while zaps are shared: whether a zap is split depends on
the reader's app.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSlider, QSpinBox,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from i18n import _

from .. import zap_shares
from ..article_details import ArticleDetails
from .coauthors import Coauthors

# The table's rows say whose they are in this role: "" for you.
_PERSON = Qt.ItemDataRole.UserRole


class AuthorsSection(QWidget):
    """The Authors section (see the module). ``changed(ArticleDetails)`` as
    co-authors and shares are changed."""

    changed = Signal(object)

    def __init__(self, *, coauthors: Coauthors, own_pubkey: Callable[[], str],
                 own_name: Callable[[], str], name_of: Callable[[str], str],
                 pick_person: Optional[Callable[[QWidget, Callable], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._coauthors = coauthors
        self._own_pubkey = own_pubkey
        # Your account's name ("" for an account without one), and how
        # anyone else is called.
        self._own_name = own_name
        self._name_of = name_of
        self._pick_person = pick_person
        self._details = ArticleDetails()
        self._editable = True
        self._unpaid: set = set()
        # Whose row was chosen ("" for you); None until one is, when the
        # first co-author is shown.
        self._chosen: Optional[str] = None
        # Made in the order Tab reaches them: the list, its buttons, then
        # the selected person's share.
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.table = QTableWidget(0, 2, self)
        self.table.setObjectName("article_details_authors")
        self.table.setAccessibleName(_("Authors"))
        self.table.horizontalHeader().hide()
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setCornerButtonEnabled(False)
        # Its corner button sits where the hidden headers meet: not there.
        corner = self.table.findChild(QAbstractButton, "qt_tableview_cornerbutton")
        if corner is not None:
            corner.hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        # Rows as high as a Mac's lists draw them: the text and a little air.
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 8)
        self.table.itemSelectionChanged.connect(self._show_selected)
        # Delete removes the selected co-author, as in a Mac's lists.
        self.table.installEventFilter(self)
        column.addWidget(self.table)
        # An article that names its co-authors and not the account's own
        # person (signer_place -1): said, quietly, where your row would be.
        self.unnamed = QLabel(_("This article doesn’t list you by name."), self)
        self.unnamed.setObjectName("article_details_hint")
        self.unnamed.setWordWrap(True)
        self.unnamed.hide()
        column.addWidget(self.unnamed)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        self.add_button = self._list_button("+", _("Add Co-Author…"))
        self.add_button.clicked.connect(self._ask)
        self.remove_button = self._list_button("−", _("Remove Co-Author"))
        self.remove_button.clicked.connect(self._remove_selected)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.even_button = QPushButton(_("Split Evenly"), self)
        self.even_button.setObjectName("article_details_link")
        self.even_button.setFlat(True)
        self.even_button.setAutoDefault(False)
        self.even_button.setToolTip(_("Give you and every co-author who can be paid an "
                                      "equal share of the zaps."))
        self.even_button.clicked.connect(
            lambda: self._hand_back(self._coauthors.split_evenly(self._details)))
        buttons.addWidget(self.even_button)
        column.addLayout(buttons)

        self.share_title = QLabel(self)
        self.share_title.setObjectName("article_details_label")
        self.share_title.setWordWrap(True)
        column.addWidget(self.share_title)
        self._share_row = QWidget(self)
        share = QHBoxLayout(self._share_row)
        share.setContentsMargins(0, 0, 0, 0)
        share.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal, self._share_row)
        self.slider.setRange(0, 100)
        self.slider.valueChanged.connect(self._share_set)
        self.field = QSpinBox(self._share_row)
        self.field.setRange(0, 100)
        self.field.setSuffix(" %")
        self.field.valueChanged.connect(self._share_set)
        # Named for the person selected as one is (see _show_selected).
        for control in (self.slider, self.field):
            control.setAccessibleName(_("Share of the zaps"))
        share.addWidget(self.slider, 1)
        share.addWidget(self.field)
        column.addWidget(self._share_row)

        self.note = QLabel(_("Apps with zap splits pay each share; others pay you."), self)
        self.note.setToolTip(_("Apps that support zap splits, like Amethyst and noStrudel, "
                               "pay each person their share. Other apps pay you."))
        self.note.setObjectName("article_details_hint")
        self.note.setWordWrap(True)
        column.addWidget(self.note)

    def _list_button(self, text: str, name: str) -> QToolButton:
        """A list's + or −, small and square, as under a Mac's lists."""
        button = QToolButton(self)
        button.setObjectName("article_details_list_button")
        button.setText(text)
        button.setAccessibleName(name)
        button.setToolTip(name)
        button.setFixedSize(26, 22)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

    # -- showing ------------------------------------------------------------------

    def show_details(self, details: ArticleDetails, *, editable: bool = True) -> None:
        """Show ``details``' authors and shares; ``editable`` False when the
        file keeps them as written."""
        self._details, self._editable = details, editable
        own = self._own_pubkey()
        shares = self._coauthors.shares(details)
        self._unpaid = set(self._coauthors.unzappable(details.coauthors))
        # You by your account's name, as the account menu says it; by your
        # short key only while the account has none.
        you = (_("{name} · you").format(name=self._own_name() or self._name_of(own))
               if own else _("You"))
        # In the order the article is published with (ArticleDetails.authors);
        # without an account or co-authors, you, then them.
        order = (details.authors((own, "")) if own and details.coauthors
                 else [(own, "")] + list(details.coauthors))
        people: List[tuple] = [
            ("", you, zap_shares.author_share(shares)) if pubkey == own
            else (pubkey, self._name_of(pubkey), shares.get(pubkey, 0))
            for pubkey, _hint in order]
        self.unnamed.setVisible(not any(pubkey == "" for pubkey, _n, _s in people))
        self.table.blockSignals(True)
        self.table.setRowCount(len(people))
        for row, (pubkey, name, share) in enumerate(people):
            who = QTableWidgetItem(name)
            who.setData(_PERSON, pubkey)
            who.setToolTip(name)
            amount = QTableWidgetItem(f"{share} %")
            amount.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if pubkey in self._unpaid:
                amount.setText("–")
                amount.setToolTip(_("No lightning address in their profile, so they can’t "
                                    "be paid a share."))
                amount.setForeground(self.palette().placeholderText())
            self.table.setItem(row, 0, who)
            self.table.setItem(row, 1, amount)
        rows = self.table.rowCount()
        self.table.setFixedHeight(self.table.verticalHeader().length() + 2 * self.table.frameWidth()
                                  if rows else 0)
        chosen = self._chosen if any(p == self._chosen for p, _n, _s in people) else None
        first = next((p for p, _n, _s in people if p), "")      # the first co-author
        keep = chosen if chosen is not None else first
        self.table.selectRow(next(i for i, (p, _n, _s) in enumerate(people) if p == keep))
        self.table.blockSignals(False)
        self.add_button.setEnabled(self._pick_person is not None and editable)
        payable = [pubkey for pubkey, _hint in details.coauthors if pubkey not in self._unpaid]
        self.even_button.setVisible(bool(payable))
        self.even_button.setEnabled(editable)
        self.note.setVisible(any(share > 0 for share in shares.values()))
        self._show_selected()
        # Last: an answer at once shows the authors again.
        self._coauthors.ask(details.coauthors, self._answered)

    def selected(self) -> str:
        """The selected co-author's key; "" for you, or nobody."""
        items = self.table.selectedItems()
        return str(items[0].data(_PERSON) or "") if items else ""

    def select(self, pubkey: str) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(_PERSON) == pubkey:
                self.table.selectRow(row)
                return

    def _show_selected(self) -> None:
        """The controls for the row selected: a co-author's share, what you
        keep, or why a co-author can't be paid."""
        pubkey = self.selected()
        if not self.table.signalsBlocked() and self.table.selectedItems():
            self._chosen = pubkey
        shares = self._coauthors.shares(self._details)
        self.remove_button.setEnabled(bool(pubkey) and self._editable)
        if not pubkey:
            self.share_title.setText(_("You keep what the others leave.")
                                     if self._details.coauthors else "")
            self.share_title.setVisible(bool(self._details.coauthors))
            self._share_row.hide()
            return
        name = self._name_of(pubkey)
        self.share_title.show()
        if pubkey in self._unpaid:
            self.share_title.setText(_("{name} has no lightning address in their profile, so "
                                       "they can’t be paid a share.").format(name=name))
            self._share_row.hide()
            return
        self.share_title.setText(_("Share for {name}").format(name=name))
        self._share_row.show()
        for control in (self.slider, self.field):
            control.blockSignals(True)
            control.setValue(shares.get(pubkey, 0))
            control.blockSignals(False)
            control.setEnabled(self._editable)
            control.setAccessibleName(_("Share for {name}").format(name=name))

    # -- changing -----------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if (watched is self.table and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and self.selected()):
            self._remove_selected()
            return True
        return False

    def _share_set(self, share: int) -> None:
        pubkey = self.selected()
        if not pubkey:
            return
        changed = self._coauthors.with_share(self._details, pubkey, share)
        self._hand_back(changed)
        # Held within what the others leave: the control shows what it got.
        got = self._coauthors.shares(changed).get(pubkey, 0)
        for control in (self.slider, self.field):
            if control.value() != got:
                control.blockSignals(True)
                control.setValue(got)
                control.blockSignals(False)

    def _remove_selected(self) -> None:
        pubkey = self.selected()
        if pubkey and self._editable:
            self._hand_back(self._coauthors.removed(self._details, pubkey))

    def _ask(self) -> None:
        if self._pick_person is not None:
            self._pick_person(self.add_button, self.add)

    def add(self, person) -> None:
        """Add ``person`` (a Person) as a co-author, credited with no share,
        and select them."""
        details = self._coauthors.added(self._details, person)
        if details is not None:        # None: already there, or the author
            self._hand_back(details)
            self.select(person.pubkey)

    def _answered(self, pubkey: str, able: Optional[bool]) -> None:
        """A co-author who can't receive zaps loses their share; one whose
        profile couldn't be read keeps it and nothing is said."""
        details = self._coauthors.learned(self._details, pubkey, able)
        if details is None:
            return
        if details != self._details:
            self._hand_back(details)
        else:
            self.show_details(details, editable=self._editable)

    def _hand_back(self, details: ArticleDetails) -> None:
        # Handed back before it is shown: showing it may learn at once that a
        # co-author can't be paid, and hand back what follows from that.
        self._details = details
        self.changed.emit(details)
        self.show_details(details, editable=self._editable)

    def shares_shown(self) -> Dict[str, str]:
        """What the list says each person gets, by key ("" for you)."""
        return {str(self.table.item(row, 0).data(_PERSON) or ""): self.table.item(row, 1).text()
                for row in range(self.table.rowCount())}
