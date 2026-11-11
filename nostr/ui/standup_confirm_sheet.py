# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bestätigen: the split and the rights of an accepted submission that
the publication publishes (M-4 M14; editorial contract 9.4, 11.2 step
``confirm``, 12.4 ``/confirmation``).

    Deinen Teil bestätigen
    Von Einundzwanzig Magazin angenommen, Fassung 2.
    Jana 70 %  ·  Paul 30 %
    Dein Anteil geht an jana@getalby.com.
    [ ] Ich habe die Rechte an meinem Teil und bin mit der
        Veröffentlichung einverstanden.            (contributors)
    [Rückfrage stellen]              [Abbrechen] [Bestätigen]

The split shown is only the ``split-proposal`` the verified accept names
for the revision it accepted (``Reading.accepted_split``); the request
names that revision's verified hash and that rumor's id. A writer
confirms the split, a contributor the split and their rights. Where the
person's profile names no lightning address, their share cannot reach
them: Profil bearbeiten opens Edit Profile. Rückfrage stellen closes the
sheet and puts the question into the conversation's composer.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from alerts import answer_return_as_the_system_does
from i18n import N_, _

from ..standup.errors import cause_words
from . import appearance
from .publish_sheet import StatusLine, fit_margins, fit_to_window, make_sheet, sheet_stylesheet
from .standup_publish_sheet import ACCEPTED_LINE

SHARE_GOES_TO = N_("Your share goes to {address}.")
NO_ADDRESS = N_("Your profile names no Lightning address yet, so your share can’t reach you.")
RIGHTS = N_("I hold the rights to my part and agree to it being published.")
NOT_CONFIRMED = N_("Not confirmed")
ASK_PREFIX = N_("A short question about the split: ")


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
                 confirm: Callable, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Confirm Your Part"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        make_sheet(self)
        self.setMinimumSize(400, 260)
        self._is_dark, self._confirm = bool(is_dark), confirm
        self._contributor = role != "writer"
        self._build(publication, number, shares or (), me, name_of)
        if shares and any(pk == me for pk, weight, _role in shares if weight > 0):
            zap_address(me, self._show_address)
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
        parts = ["{name} {weight} %".format(
            name=_("You") if pk == me else (name_of(pk) or _("Someone")), weight=weight)
            for pk, weight, _role in shares]
        self.split = self._label("  ·  ".join(parts), "")
        self.split.setVisible(bool(parts))
        self.split.setAccessibleName(_("The split"))
        root.addWidget(self.split)
        self.address = self._label("")
        self.address.hide()
        root.addWidget(self.address)
        self.edit_profile = QPushButton(_("Edit Profile…"))
        self.edit_profile.setAutoDefault(False)
        self.edit_profile.hide()
        self.edit_profile.clicked.connect(self._edit_profile)
        root.addWidget(self.edit_profile, 0, Qt.AlignmentFlag.AlignLeft)
        self.rights = QCheckBox(_(RIGHTS))
        self.rights.setVisible(self._contributor)
        self.rights.toggled.connect(self._refresh)
        root.addWidget(self.rights)
        root.addStretch(1)
        self.status = StatusLine(self._is_dark)
        root.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.ask = buttons.addButton(_("Ask a Question"), QDialogButtonBox.ButtonRole.ActionRole)
        self.ask.setAutoDefault(False)
        self.ask.clicked.connect(self._ask)
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button = buttons.addButton(_("Confirm"),
                                                QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm_button.setDefault(True)
        self.confirm_button.clicked.connect(self._on_confirm)
        root.addWidget(buttons)
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    def _show_address(self, address: Optional[str]) -> None:
        """Where the person's share goes; nothing while it couldn't be
        told (lessons 36: a lookup that did not answer says nothing)."""
        try:
            if address is None:
                return
            self.address.setText(_(SHARE_GOES_TO).format(address=address) if address
                                 else _(NO_ADDRESS))
            self.address.show()
            self.edit_profile.setVisible(not address)
        except RuntimeError:
            pass                                # the sheet closed meanwhile

    def _refresh(self) -> None:
        self.confirm_button.setEnabled(not self._contributor or self.rights.isChecked())

    def _on_confirm(self) -> None:
        confirms = ("split", "rights") if self._contributor else ("split",)
        self.confirm_button.setEnabled(False)
        self.status.say(_("Confirming …"), quietly=True)
        self._confirm(confirms, self._confirmed)

    def _confirmed(self, error) -> None:
        try:
            if error is None:
                self.accept()
                return
            cause = cause_words(getattr(error, "code", ""))
            self.status.say(_(cause), error=True, title=_(NOT_CONFIRMED))
            self._refresh()
        except RuntimeError:
            pass                                # the sheet closed meanwhile

    def _ask(self) -> None:
        self.ask_requested.emit(_(ASK_PREFIX))
        self.reject()

    def _edit_profile(self) -> None:
        self.edit_profile_requested.emit()

    def _style(self) -> None:
        self.setStyleSheet(sheet_stylesheet(self._is_dark))
        appearance.surface(self, self._is_dark)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self._style()
        self.status.set_dark(is_dark)


__all__ = ["ConfirmSheet", "NO_ADDRESS", "RIGHTS", "SHARE_GOES_TO"]
