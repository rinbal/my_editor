# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Imports window does with the posts chosen: the action bar
under the list and its Options popover.

    [Skip]  [Options...]  [Create 3 Drafts]
    Imported drafts you don't change are removed after 90 days. Your
    signer app may ask up to 6 times, twice for each draft and once for
    each image. Keep it open until this finishes.

The actions apply to the checked posts, or to the open one when none is
checked (Mail's rule, workspace.targets). Create is the prominent button
at the trailing edge; the buttons wrap onto a second row rather than
widen the list (flow_layout.py). The line under them says, before
anything is made, what will happen: how long an imported draft is kept
(the owner's decision Q-11, said once, here) and, for a signer app, how
often it may ask.

Options (a popover, so the choices stay out of the way until wanted):
copy images to your media server, with Review Images..., and fetch the
full article. They start from each source's defaults; a source with
other defaults than the rest shows the box as mixed, and a choice made
here applies to this run only.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from i18n import _, ngettext
import theme
from . import appearance

from .flow_layout import FlowLayout

EXPIRY_NOTE = _("Imported drafts you don't change are removed after 90 days.")
BUSY_TIP = _("Pause the current import before starting another.")

COPY_IMAGES = "rehost_images"
FULL_TEXT = "fetch_full_text"


def create_label(count: int) -> str:
    if count <= 1:
        return _("Create Draft")
    return ngettext("Create {count} Draft", "Create {count} Drafts", count).format(count=count)


def signer_note(prompts: int) -> str:
    if prompts <= 0:
        return ""
    # The glossary's words (docs/translating.md): a signer app.
    return ngettext("Your signer app may ask once. Keep it open until this finishes.",
                    "Your signer app may ask up to {count} times, twice for each draft and "
                    "once for each image. Keep it open until this finishes.",
                    prompts).format(count=prompts)


class _Note(QLabel):
    """A wrapping line of small print."""

    def __init__(self, text: str = "", name: str = "imports_footnote") -> None:
        super().__init__(text)
        self.setObjectName(name)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)


class ActionBar(QWidget):
    """Skip, Options and Create, with what will happen under them.

    Signals:
      skip_clicked()      Skip (or Restore, in the Skipped list)
      options_clicked()   Options...
      create_clicked()    Create N Drafts
    """

    skip_clicked = Signal()
    options_clicked = Signal()
    create_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("imports_actions")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 8, 12, 10)
        column.setSpacing(6)
        self.row = FlowLayout(trailing=True)
        self.skip = QPushButton(_("Skip"))
        self.skip.setObjectName("imports_skip")
        self.skip.clicked.connect(self.skip_clicked)
        self.options = QPushButton(_("Options…"))
        self.options.setObjectName("imports_options")
        self.options.setToolTip(_("Copy images, fetch the full article"))
        self.options.clicked.connect(self.options_clicked)
        self.create = QPushButton(create_label(1))
        self.create.setObjectName("imports_create")
        self.create.clicked.connect(self.create_clicked)
        for button in (self.skip, self.options, self.create):
            button.setAutoDefault(False)
            self.row.addWidget(button)
        column.addLayout(self.row)
        self.note = _Note(EXPIRY_NOTE)
        column.addWidget(self.note)

    def show_targets(self, count: int, *, can_create: bool, busy: bool, skip_text: str,
                     can_skip: bool, prompts: int) -> None:
        """What the buttons say and allow for ``count`` posts."""
        self.create.setText(create_label(count))
        self.create.setEnabled(count > 0 and can_create and not busy)
        self.create.setToolTip(BUSY_TIP if busy and count else "")
        self.options.setEnabled(count > 0 and can_create)
        self.skip.setText(skip_text)
        self.skip.setEnabled(can_skip)
        note = EXPIRY_NOTE
        signer = signer_note(prompts) if count and can_create else ""
        if signer:
            note = f"{note} {signer}"
        self.note.setText(note)


class OptionsPopover(QFrame):
    """The choices for the drafts of this run (see the module docstring).

    Signals:
      changed(str, bool)     the person chose an option (its key, on or off)
      review_requested()     Review Images...
    """

    changed = Signal(str, bool)
    review_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("imports_options_popover")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName(_("Options for the New Drafts"))
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(6)
        self.copy = QCheckBox(_("Copy images to your media server"))
        self.copy.setObjectName("imports_copy_images")
        self.copy.clicked.connect(self._copy_clicked)
        column.addWidget(self.copy)
        column.addWidget(_Note(_("Drafts keep working if the original site removes its "
                                 "images."), "imports_hint"))
        self.review = QPushButton(_("Review Images…"))
        self.review.setObjectName("imports_review_images")
        self.review.setAutoDefault(False)
        self.review.clicked.connect(self._review)
        column.addWidget(self.review, 0, Qt.AlignmentFlag.AlignLeft)
        column.addSpacing(6)
        self.full = QCheckBox(_("Fetch the full article"))
        self.full.setObjectName("imports_full_text")
        self.full.clicked.connect(self._full_clicked)
        column.addWidget(self.full)
        column.addWidget(_Note(_("Uses the whole article when the feed only has a summary."),
                               "imports_hint"))
        self.mixed = _Note(_("These posts come from sources with different settings. What "
                             "you choose here applies to all of them, this time."),
                           "imports_hint")
        column.addSpacing(4)
        column.addWidget(self.mixed)
        self.setMinimumWidth(300)
        self.setMaximumWidth(380)
        self.apply_theme(theme.is_dark_active())
        appearance.bind(self)

    def apply_theme(self, dark: bool) -> None:
        appearance.popover(self, dark)

    @staticmethod
    def _set(box: QCheckBox, value: Optional[bool]) -> None:
        box.setTristate(value is None)
        box.setCheckState(Qt.CheckState.PartiallyChecked if value is None
                          else Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)

    def show_choices(self, *, copy: Optional[bool], full: Optional[bool],
                     has_images: bool) -> None:
        """``copy`` and ``full``: on, off, or None for sources that differ."""
        self._set(self.copy, copy)
        self._set(self.full, full)
        self.mixed.setVisible(copy is None or full is None)
        self.review.setVisible(has_images)
        self.review.setEnabled(copy is not False)

    # Bound slots, not closures over this popover: a closure on one of its
    # boxes would keep the popover (and the window) alive.
    def _copy_clicked(self) -> None:
        self._clicked(self.copy, COPY_IMAGES)

    def _full_clicked(self) -> None:
        self._clicked(self.full, FULL_TEXT)

    def _clicked(self, box: QCheckBox, key: str) -> None:
        # A mixed box turns on when clicked, and is a plain box after.
        if box.isTristate():
            box.setTristate(False)
            box.setCheckState(Qt.CheckState.Checked)
        on = box.checkState() == Qt.CheckState.Checked
        if key == COPY_IMAGES:
            self.review.setEnabled(on)
        self.changed.emit(key, on)

    def _review(self) -> None:
        self.hide()
        self.review_requested.emit()

    def open_below(self, anchor: QWidget) -> None:
        """Show under ``anchor``, kept on its screen."""
        self.adjustSize()
        point = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 4))
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            if point.y() + self.height() > screen.bottom():
                point.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)
            point.setX(max(screen.left(), min(point.x(), screen.right() - self.width())))
        self.move(point)
        self.show()
        self.copy.setFocus(Qt.FocusReason.PopupFocusReason)
