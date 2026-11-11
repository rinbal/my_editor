# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bestätigen: the split and the rights of an accepted submission that
the publication publishes (M-4 M14; editorial contract 9.4, 11.2 step
``confirm``, 12.4 ``/confirmation``).

    Deinen Teil bestätigen
    Von Einundzwanzig Magazin angenommen, Fassung 2.
    Jana 70 %  ·  Paul 30 %                        (a narrow no-break space)
    Dein Anteil geht an jana@getalby.com.
    [ ] Mein Beitrag (Fotos) gehört mir. Ich bin mit der
        Lizenz CC BY 4.0 einverstanden.            (contributors)
    [Rückfrage stellen]              [Abbrechen] [Bestätigen]

The split shown is only the ``split-proposal`` the verified accept names
for the revision it accepted (``Reading.accepted_split``); the request
names that revision's verified hash and that rumor's id. A writer
confirms the split, a contributor the split and their rights. A share
reaches its person only at the lightning address their profile names, so
Bestätigen is there only once a read named one (RG-M4-a 3, RG-M4-b 2, as
the web's): where the profile names none, Profil bearbeiten opens Edit
Profile in its place; where the profile could not be read, Erneut
versuchen reads it again. The primary action comes first in the focus
order. Rückfrage stellen closes the sheet and puts the question into the
conversation's composer. A contributor's rights are the web's sentences
(RG-M4-b 7), their part as the revision credits it and the license the
revision names, in a label that wraps.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import N_, _

from ..standup.errors import cause_words
from ..standup.thread import share_text
from . import appearance
from .publish_sheet import StatusLine, fit_margins, fit_to_window, make_sheet, sheet_stylesheet
from .standup_publish_sheet import ACCEPTED_LINE

SHARE_GOES_TO = N_("Your share goes to {address}.")
# The web's words (editorial-confirmation.js) for the same moments.
NO_ADDRESS = N_("For your share, your Nostr profile needs a Lightning address.")
PROFILE_READING = N_("Reading your profile …")
PROFILE_NOT_READ = N_("Your profile couldn’t be read, so it’s not known where your share goes.")
RIGHTS = N_("My contribution is mine.")
RIGHTS_PART = N_("My contribution ({part}) is mine.")
LICENSE = N_("I agree to the license {license}.")
NOT_CONFIRMED = N_("Not confirmed")
ASK_PREFIX = N_("A short question about the split: ")


class _Toggles(QLabel):
    """The words of a check box, wrapping; a click on them toggles it."""

    def __init__(self, text: str, box: QCheckBox) -> None:
        super().__init__(text)
        self._box = box
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setBuddy(box)

    def mousePressEvent(self, event) -> None:  # noqa: N802, Qt's name
        if self._box.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self._box.toggle()
            self._box.setFocus(Qt.FocusReason.MouseFocusReason)


def rights_words(part: str, license: str) -> str:
    """The rights a contributor confirms, as the web says them: their part
    as the revision credits it, then the license the revision names."""
    words = _(RIGHTS_PART).format(part=part) if part else _(RIGHTS)
    return f"{words} {_(LICENSE).format(license=license)}" if license else words


class ConfirmSheet(QDialog):
    """See the module docstring. ``shares`` are ``(pubkey, weight, role)``
    of the accepted split, or None for an accept that names none;
    ``confirm(confirms, on_done)`` sends the confirmation;
    ``zap_address(pubkey, answer)`` reads a profile's lightning address."""

    ask_requested = Signal(str)              # the question's first words
    edit_profile_requested = Signal()

    def __init__(self, *, role: str, publication: str, number: int,
                 shares: Optional[Sequence[Tuple[str, int, str]]], me: str,
                 name_of: Callable[[str], str], zap_address: Callable,
                 confirm: Callable, is_dark: bool, part: str = "", license: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Confirm Your Part"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        make_sheet(self)
        self.setMinimumSize(400, 260)
        self._is_dark, self._confirm = bool(is_dark), confirm
        self._contributor = role != "writer"
        # A share of this person's can reach them only at an address their
        # profile names: Confirm waits for a read that names one (RG-M4-a 3).
        self._needs_address = bool(shares) and any(
            pk == me for pk, weight, _role in shares if weight > 0)
        # None while not read (or not readable), "" for none, else the address.
        self._address: Optional[str] = None
        self._reading = self._profile_edited = self._confirming = self._stale = False
        self._zap_address = zap_address
        self._rights_words = rights_words(part, license)
        self._build(publication, number, shares or (), me, name_of)
        if self._needs_address:
            self._read_address()
        self._refresh()
        fit_to_window(self, QSize(520, min(460, max(300, self.sizeHint().height()))))

    def _label(self, text: str, name: str = "help") -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def _build(self, publication, number, shares, me, name_of) -> None:
        self._style()
        appearance.bind(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)
        root.addWidget(self._label(_("Confirm Your Part"), "title"))
        root.addWidget(self._label(_(ACCEPTED_LINE).format(publication=publication,
                                                           number=number), ""))
        self._shares, self._me, self._name_of = tuple(shares), me, name_of
        self.split = self._label("", "")
        self.split.setVisible(bool(shares))
        self.show_names()
        root.addWidget(self.split)
        self.address = self._label("")
        self.address.setVisible(self._needs_address)
        root.addWidget(self.address)
        # A check box's own text does not wrap: the words are a label
        # beside it that wraps and toggles it, and its accessible name.
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        self.rights = QCheckBox()
        self.rights.setAccessibleName(self._rights_words)
        self.rights.toggled.connect(self._refresh)
        line.addWidget(self.rights, 0, Qt.AlignmentFlag.AlignTop)
        self.rights_text = _Toggles(self._rights_words, self.rights)
        line.addWidget(self.rights_text, 1)
        row.setVisible(self._contributor)
        root.addWidget(row)
        root.addStretch(1)
        self.status = StatusLine(self._is_dark)
        root.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.ask = buttons.addButton(_("Ask a Question"), QDialogButtonBox.ButtonRole.ActionRole)
        self.ask.setAutoDefault(False)
        self.ask.clicked.connect(self._ask)
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.clicked.connect(self.reject)
        # One primary at a time: Bestätigen, or what makes it possible.
        self.confirm_button = buttons.addButton(_("Confirm"),
                                                QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm_button.clicked.connect(self._on_confirm)
        self.edit_profile = buttons.addButton(_("Edit Profile…"),
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self.edit_profile.clicked.connect(self._edit_profile)
        self.read_again = buttons.addButton(_("Try Again"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.read_again.clicked.connect(self._read_address)
        root.addWidget(buttons)
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def show_names(self) -> None:
        """The split by the people's names (again when a profile's name
        came in late), read as one line with what it is (VoiceOver)."""
        parts = [share_text(_("You") if pk == self._me else (self._name_of(pk) or _("Someone")),
                            weight) for pk, weight, _role in self._shares]
        self.split.setText("  ·  ".join(parts))
        self.split.setAccessibleName(_("The split: {shares}").format(shares=", ".join(parts)))

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    def _read_address(self) -> None:
        self._reading = True
        self._refresh()
        self._zap_address(self._me, self._show_address)

    def _show_address(self, address: Optional[str]) -> None:
        """Where the person's share goes, once read; None: the profile
        could not be read (the lookup asked again before it said so)."""
        try:
            self._reading, self._address = False, address
            self._refresh()
        except RuntimeError:
            pass                                # the sheet closed meanwhile

    def _refresh(self) -> None:
        address, needs = self._address, self._needs_address
        ready = (not needs or bool(address)) and not self._stale
        self.cancel_button.setText(_("Close") if self._stale else _("Cancel"))
        missing = needs and not self._reading and address == "" and not self._stale
        unread = needs and not self._reading and address is None and not self._stale
        if needs:
            self.address.setText(_(SHARE_GOES_TO).format(address=address) if address
                                 else _(NO_ADDRESS) if missing
                                 else _(PROFILE_NOT_READ) if unread else _(PROFILE_READING))
        self.confirm_button.setVisible(ready)
        self.confirm_button.setEnabled(ready and not self._confirming
                                       and (not self._contributor or self.rights.isChecked()))
        self.edit_profile.setVisible(missing)
        self.read_again.setVisible(unread)
        primary = (self.confirm_button if ready else self.edit_profile if missing
                   else self.read_again if unread else None)
        for button in (self.confirm_button, self.edit_profile, self.read_again):
            button.setDefault(button is primary)
        self._order(primary)

    def _order(self, primary: Optional[QPushButton]) -> None:
        """The focus order (RG-M4-b 2): the rights first where they are
        asked, then the primary action, then Rückfrage stellen and
        Abbrechen."""
        chain = [w for w in (self.rights if self._contributor else None, primary,
                             self.ask, self.cancel_button) if w is not None]
        for before, after in zip(chain, chain[1:]):
            QWidget.setTabOrder(before, after)

    def showEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().showEvent(event)
        first = self.rights if self._contributor else next(
            (b for b in (self.confirm_button, self.edit_profile, self.read_again)
             if b.isVisibleTo(self)), self.ask)
        first.setFocus(Qt.FocusReason.TabFocusReason)

    def _on_confirm(self) -> None:
        confirms = ("split", "rights") if self._contributor else ("split",)
        self._confirming = True
        self._refresh()
        self.status.say(_("Confirming …"), quietly=True)
        self._confirm(confirms, self._confirmed)

    def _confirmed(self, error) -> None:
        try:
            self._confirming = False
            if error is None:
                self.accept()
                return
            cause = cause_words(getattr(error, "code", ""))
            # A 409: the submission was read again, and this version is no
            # longer the one to confirm; nothing to try again here.
            self._stale = getattr(error, "status", None) == 409
            self.status.say(_(cause), error=True, title=_(NOT_CONFIRMED))
            self._refresh()
        except RuntimeError:
            pass                                # the sheet closed meanwhile

    def _ask(self) -> None:
        self.ask_requested.emit(_(ASK_PREFIX))
        self.reject()

    def _edit_profile(self) -> None:
        self._profile_edited = True
        self.edit_profile_requested.emit()

    def changeEvent(self, event) -> None:  # noqa: N802, Qt's name
        """Back from editing the profile, the address is read again, so a
        newly named one enables Confirm without reopening the sheet."""
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()
                and getattr(self, "_profile_edited", False) and not self._address):
            self._profile_edited = False
            self._read_address()

    def _style(self) -> None:
        self.setStyleSheet(sheet_stylesheet(self._is_dark))
        appearance.surface(self, self._is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self._style()
        self.status.set_dark(is_dark)


__all__ = ["ConfirmSheet", "NO_ADDRESS", "PROFILE_NOT_READ", "PROFILE_READING", "RIGHTS",
           "SHARE_GOES_TO"]
