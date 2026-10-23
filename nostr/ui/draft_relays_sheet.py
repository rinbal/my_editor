# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draft Relays: where the account keeps its drafts.

A sheet on the window (window-modal: on macOS it slides down from the
title bar), opened from the Drafts panel's action menu:

    Draft Relays
    Drafts are encrypted wherever they are kept. With relays chosen
    here, drafts are saved only there. When you change this, your
    drafts move with it. Apps that don't read this setting won't see
    the drafts saved there.

    Drafts are saved only on these relays:
    ┌──────────────────────────────────────┐
    │ wss://relay.example                  │
    └──────────────────────────────────────┘
    (While none are chosen: "Drafts are kept on your usual relays:", and
    the list shows those, dimmed and not selectable, as macOS shows the
    DNS servers it got by itself; adding a relay takes their place.)
                                   [Remove]
    Add a relay: [ wss://            ] [Add]

    Still looking for drafts on:          (only while there are such relays)
    These relays are no longer in the setting ...
    ┌──────────────────────────────────────┐
    │ wss://old.example                    │
    └──────────────────────────────────────┘
                                [Stop Looking…]

    [Use My Usual Relays]     [Cancel] [Save]

It reads the account's list first (nostr/draft_relays.py), and edits
only what it has just read: a list changed on another device in the
meantime is not written over; the sheet reads it again and says so.
Adding checks the address (``wss://``, or ``ws://`` on this computer).
Use My Usual Relays empties the list, the way Restore Defaults resets a
settings sheet, and the usual relays show again; nothing changes before
Save.

Save moves the drafts to where they go next ("Moving 12 drafts…", with
progress), reads them back from the relays chosen (a relay that takes
drafts but does not give them back is no place for them, and the sheet
names it), then publishes the encrypted list (or an empty one), and only
then do drafts follow it (PrivateDraftRelays.save). Cancel (or Escape)
stops it while the drafts are read or moved, and nothing changes; once
the list itself is being saved, the change can't be called back. When a
draft could not be moved, nothing is changed and the sheet says what to
do. When a relay being left did not answer,
Save Anyway (in place of Use My Usual Relays) changes the setting all
the same, leaving the drafts kept only on the relays it named behind:
for a relay that is gone for good. Any other relay being left must still
answer. When one being left stops answering only after the list went
out, the setting is saved, and a last page says that drafts saved there
meanwhile are looked for there and moved once it answers (Done closes).

Relays no longer in the setting that may still hold drafts (a list
changed on another device, or a relay that went quiet during a change)
are read until each answers (RelayDirectory.leave_drafts_behind). The
sheet lists them, so the person can see what MyEditor still looks at,
and Stop Looking… lets one go, after asking ("Drafts kept only on … won't
be shown any more", Cancel the default).

Keys: Return in the address field adds it, elsewhere it saves; Delete or
Backspace in the list removes the selected relay; Escape cancels. Once
the list is read, it has the focus when it has relays, the address field
otherwise.

Signals:
  saved(list)   the relays drafts now go to ([] for the usual relays)
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import shiboken6
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

from alerts import answer_return_as_the_system_does, confirm_destructive
from i18n import _, ngettext

from ..draft_relays import (
    MOVING_DRAFTS, READING_DRAFTS, SAVING_LIST, UNDECRYPTED, PrivateDraftRelays, SaveOutcome,
    draft_relay_address,
)
from ..outbox import defaults
from ..outbox.writer import CAUSE_CHANGED, CAUSE_CLOCK, CAUSE_RELAYS, CAUSE_SIGNER, UNKNOWN_BASE
from .assistant import DEFAULT, LEADING, NORMAL, AssistantWindow, busy_bar, page, text_label

MAIN = "main"
LEFT_BEHIND = "left_behind"

# What the sheet is doing.
READING = "reading"
READ_FAILED = "read_failed"
EDITING = "editing"
SAVING = "saving"
SAVED = "saved"              # saved, with a note on drafts left behind

# Marks a usual relay shown in the list while no draft relays are chosen.
_USUAL = Qt.ItemDataRole.UserRole + 1


class DraftRelaysSheet(AssistantWindow):
    """Choose the relays drafts are kept on (see the module's text)."""

    saved = Signal(list)

    def __init__(self, relays: PrivateDraftRelays, *, is_dark: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(_("Draft Relays"), is_dark=is_dark, min_width=480, parent=parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._relays = relays
        # A move in the background may clear a relay still looked at.
        relays.left_behind_changed.connect(self._show_relays_left_behind)
        self._read: List[str] = []        # the account's list, as last read
        self._state = READING
        self._list_saving = False     # the save reached the list's own change
        self._generation = 0
        self._shown_buttons: list = []
        # The choice a save failed for because a relay being left did not
        # answer: while the form shows it, Save Anyway is offered, and
        # leaves the drafts of the relays named so far (``_unread``) behind:
        # agreeing to leave one relay's drafts holds for that choice until
        # it changes, so a second relay going quiet adds to it.
        self._left_unread: Optional[List[str]] = None
        self._unread: List[str] = []
        self._usual_asked = 0         # answers to an older question are dropped
        self._build()
        self._read_list()

    # -- what is shown -------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def chosen(self) -> List[str]:
        """The relays chosen in the list now (not the usual ones it shows
        while there are none)."""
        return [item.text() for item in self._rows() if not item.data(_USUAL)]

    def _rows(self) -> List[QListWidgetItem]:
        return [self._list.item(row) for row in range(self._list.count())]

    def _build(self) -> None:
        widget, column = page(10)
        column.addWidget(text_label(_("Draft Relays"), "title"))
        column.addWidget(text_label(
            _("Drafts are encrypted wherever they are kept. With relays chosen here, drafts "
              "are saved only there. When you change this, your drafts move with it. Apps "
              "that don’t read this setting won’t see the drafts saved there."), "muted"))

        self._summary = text_label("", "item_title")
        column.addWidget(self._summary)
        self._list = QListWidget()
        self._list.setAccessibleName(_("Draft relays"))
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Five rows high, as a list in a settings sheet is: room for the
        # usual relays and a few more, the sheet compact however tall.
        self._list.setFixedHeight((self._list.fontMetrics().height() + 6) * 5 + 8)
        self._list.itemSelectionChanged.connect(self._update)
        self._list.installEventFilter(self)
        column.addWidget(self._list)

        under_list = QHBoxLayout()
        under_list.addStretch(1)
        self._remove = QPushButton(_("Remove"))
        answer_return_as_the_system_does([self._remove])
        self._remove.clicked.connect(self._remove_selected)
        under_list.addWidget(self._remove)
        column.addLayout(under_list)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        label = QLabel(_("Add a relay:"))
        self._field = QLineEdit()
        self._field.setPlaceholderText("wss://")
        label.setBuddy(self._field)
        self._field.textChanged.connect(self._on_typing)
        self._field.installEventFilter(self)
        self._add = QPushButton(_("Add"))
        answer_return_as_the_system_does([self._add])
        self._add.clicked.connect(self._add_typed)
        add_row.addWidget(label)
        add_row.addWidget(self._field, 1)
        add_row.addWidget(self._add)
        column.addLayout(add_row)

        self._error = text_label("", "error")
        self._error.hide()
        column.addWidget(self._error)

        self._left = QWidget()
        left_column = QVBoxLayout(self._left)
        left_column.setContentsMargins(0, 8, 0, 0)
        left_column.setSpacing(6)
        left_column.addWidget(text_label(_("Still looking for drafts on:"), "item_title"))
        left_column.addWidget(text_label(
            _("These relays are no longer in the setting, but may still hold drafts saved "
              "there before it changed. MyEditor moves them to where drafts are kept now "
              "once each relay answers."),
            "muted"))
        self._left_list = QListWidget()
        self._left_list.setAccessibleName(_("Relays still looked at for drafts"))
        self._left_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # One line per relay, never a sideways scroll bar over the rows: a
        # long address is shortened in the middle, as Finder shortens long
        # names, and its tooltip and accessible name hold it whole.
        self._left_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._left_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._left_list.setWordWrap(False)
        self._left_list.itemSelectionChanged.connect(self._update)
        left_column.addWidget(self._left_list)
        under_left = QHBoxLayout()
        under_left.addStretch(1)
        self._stop_looking = QPushButton(_("Stop Looking…"))
        answer_return_as_the_system_does([self._stop_looking])
        self._stop_looking.clicked.connect(self._stop_looking_on_selected)
        under_left.addWidget(self._stop_looking)
        left_column.addLayout(under_left)
        self._left.hide()
        column.addWidget(self._left)

        self._busy = QWidget()
        busy_row = QHBoxLayout(self._busy)
        busy_row.setContentsMargins(0, 0, 0, 0)
        busy_row.setSpacing(10)
        self._progress = busy_bar(120)      # counts once the drafts to move are known
        busy_row.addWidget(self._progress)
        self._busy_label = text_label("", "muted")
        busy_row.addWidget(self._busy_label, 1)
        self._busy.hide()
        column.addWidget(self._busy)
        column.addStretch(1)
        self.add_page(MAIN, widget)

        widget, column = page(10)
        column.addWidget(text_label(_("The setting is saved."), "title"))
        self._left_note = text_label("", "muted")
        column.addWidget(self._left_note)
        column.addStretch(1)
        self.add_page(LEFT_BEHIND, widget)
        self.show_page(MAIN, [])

    # -- reading ---------------------------------------------------------------

    def _read_list(self, *, then_say: str = "") -> None:
        self._relays.ask_again()             # the person asked: open, or Try Again
        self._generation += 1
        generation = self._generation
        self._set_state(READING, busy=_("Reading where your drafts are kept…"))
        self._relays.read(lambda relays: self._on_read(generation, relays, then_say))

    def _on_read(self, generation: int, relays: Optional[List[str]], then_say: str) -> None:
        if generation != self._generation:
            return
        if relays is None:
            if self._relays.last_failure == UNDECRYPTED:
                message = _("Couldn’t decrypt where your drafts are kept. Make sure your "
                            "signer app is running, then try again.")
            else:
                message = _("Couldn’t read where your drafts are kept. Check your internet "
                            "connection, then try again.")
            self._set_state(READ_FAILED, error=message)
            return
        self._read = list(relays)
        self._left_unread = None
        self._list.clear()
        self._list.addItems(self._read)
        self._show_relays_left_behind()
        self._set_state(EDITING, error=then_say)
        self._show_usual_relays()
        # The list to work on when there is one, otherwise the field to
        # start one.
        (self._list if self.chosen() else self._field).setFocus()

    # -- editing ---------------------------------------------------------------

    def _on_typing(self, _text: str) -> None:
        if self._state == EDITING:
            self._error.hide()
        self._update()

    def _add_typed(self) -> None:
        if self._state != EDITING or not self._field.text().strip():
            return
        address = draft_relay_address(self._field.text())
        if address is None:
            self._say(_("Enter a relay’s address. It starts with wss://."))
            return
        if address in self.chosen():
            self._say(_("That relay is already in the list."))
            return
        if len(self.chosen()) >= defaults.PRIVATE_CAP:
            self._say(ngettext("Drafts can be kept on at most {count} relay.",
                               "Drafts can be kept on at most {count} relays.",
                               defaults.PRIVATE_CAP).format(count=defaults.PRIVATE_CAP))
            return
        self._hide_usual_relays()
        self._list.addItem(address)
        self._list.setCurrentRow(self._list.count() - 1)
        self._field.clear()
        self._update()

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if self._state != EDITING or row < 0 or not self._list.item(row).isSelected():
            return
        self._list.takeItem(row)
        if self._list.count():
            self._list.setCurrentRow(min(row, self._list.count() - 1))
        self._show_usual_relays()
        self._update()

    def _use_usual_relays(self) -> None:
        """Like Restore Defaults: the form changes, Save makes it so."""
        self._list.clear()
        self._show_usual_relays()
        self._update()

    # -- the usual relays ------------------------------------------------------

    def _show_usual_relays(self) -> None:
        """While no draft relays are chosen, the list shows where drafts go
        then: the account's usual relays, dimmed and not selectable, until
        one is added (as macOS shows the DNS servers it got by itself)."""
        if self.chosen():
            return
        self._usual_asked += 1
        asked = self._usual_asked
        self._relays.usual_relays(lambda relays: self._on_usual_relays(asked, relays))

    def _on_usual_relays(self, asked: int, relays: List[str]) -> None:
        # The answer may come after the sheet closed, or after a relay was
        # chosen or the question asked again.
        if not shiboken6.isValid(self._list) or asked != self._usual_asked or self.chosen():
            return
        self._hide_usual_relays()
        for relay in relays:
            item = QListWidgetItem(relay)
            item.setData(_USUAL, True)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setData(Qt.ItemDataRole.AccessibleTextRole,
                         _("{relay}, a usual relay").format(relay=relay))
            self._list.addItem(item)
        self._update()

    def _hide_usual_relays(self) -> None:
        for row in reversed(range(self._list.count())):
            if self._list.item(row).data(_USUAL):
                self._list.takeItem(row)

    # -- relays left behind ----------------------------------------------------

    def _show_relays_left_behind(self) -> None:
        if not hasattr(self, "_left_list"):
            return                      # still being built
        self._left_list.clear()
        for relay in self._relays.drafts_left_behind():
            item = QListWidgetItem(relay)
            item.setToolTip(relay)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, relay)
            self._left_list.addItem(item)
        # As tall as its relays, up to three rows (usually one or two): the
        # sheet stays short, and a longer list scrolls.
        rows = min(max(self._left_list.count(), 1), 3)
        row = self._left_list.sizeHintForRow(0) if self._left_list.count() else 0
        self._left_list.setFixedHeight(
            rows * (row or self._left_list.fontMetrics().height())
            + 2 * self._left_list.frameWidth() + 4)
        self._left.setVisible(self._left_list.count() > 0)
        self._update()

    def _stop_looking_on_selected(self) -> None:
        item = self._left_list.currentItem()
        if self._state != EDITING or item is None or not item.isSelected():
            return
        relay = item.text()
        # The address in the message, where it wraps as a whole; the title
        # stays a short question.
        if confirm_destructive(
                self, title=_("Stop looking for drafts on this relay?"),
                message=_("Drafts kept only on {relay} won’t be shown any more.").format(
                    relay=relay),
                action=_("Stop Looking"), is_dark=self._is_dark, nostr=True):
            self._relays.stop_reading(relay)
            self._show_relays_left_behind()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if (watched is self._field and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and self._field.text().strip()):
                self._add_typed()           # Return adds what was typed, not Save
                return True
            if watched is self._list and key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self._remove_selected()
                return True
        return super().eventFilter(watched, event)

    # -- saving ----------------------------------------------------------------

    def _save(self, *, leave_unread: Sequence[str] = ()) -> None:
        if self._state != EDITING or not self._changed():
            return
        chosen = self.chosen()
        self._relays.ask_again()             # the person asked: Save, or Save Anyway
        self._generation += 1
        generation = self._generation
        self._list_saving = False
        self._set_state(SAVING, busy=_("Reading your drafts…"))
        self._relays.save(
            chosen, lambda outcome: self._on_saved(generation, chosen, outcome),
            on_step=lambda step, done, total, drafts: self._on_step(
                generation, step, done, total, drafts),
            leave_unread=list(leave_unread))

    def _save_anyway(self) -> None:
        self._save(leave_unread=self._unread)

    def _on_step(self, generation: int, step: str, done: int, total: int,
                 drafts: int) -> None:
        """Where the save is: reading the drafts, moving them (counted),
        saving the list (the signer may ask)."""
        if generation != self._generation:
            return
        if step == MOVING_DRAFTS and total:
            self._busy_label.setText(
                ngettext("Moving {count} draft…", "Moving {count} drafts…", drafts).format(
                    count=drafts) if drafts else _("Moving your drafts…"))
            self._progress.setRange(0, total)
            self._progress.setValue(done)
        elif step in (READING_DRAFTS, SAVING_LIST):
            self._busy_label.setText(
                _("Reading your drafts…") if step == READING_DRAFTS
                else _("Saving. Approve the request in your signer app if it asks…"))
            self._progress.setRange(0, 0)
        if step == SAVING_LIST:
            self._list_saving = True
            self._update_buttons()

    def _on_saved(self, generation: int, chosen: List[str], outcome: SaveOutcome) -> None:
        if generation != self._generation:
            return
        if outcome.ok:
            self.saved.emit(list(chosen))
            if outcome.left_behind:
                self._show_left_behind(outcome.left_behind)
            else:
                self._state = EDITING
                self.accept()
            return
        if outcome.write is not None and outcome.write.cause == CAUSE_CHANGED:
            # Not written over: show the setting as it is now, to decide again.
            self._read_list(then_say=_(
                "The setting was changed on another device while this was open, so nothing "
                "was changed here. This is the setting as it is now."))
            return
        # Relays being left whose drafts can't all be read (not answering,
        # or sending only their newest): Save Anyway may leave them behind.
        stuck = (*outcome.move.unread, *outcome.move.unpaged)
        agreed = self._unread if self._left_unread == chosen else []
        self._left_unread = list(chosen) if stuck else None
        self._unread = list(dict.fromkeys([*agreed, *stuck])) if stuck else []
        self._set_state(EDITING, error=self._why_not_saved(outcome))

    def _show_left_behind(self, relays) -> None:
        """Saved, but a relay being left did not answer the last pass:
        drafts saved there meanwhile are read on and moved once it does."""
        self._left_note.setText(ngettext(
            "Drafts saved during the change may still be only on {relays}, which didn’t "
            "answer. MyEditor keeps looking for them there and moves them once it answers.",
            "Drafts saved during the change may still be only on {relays}, which didn’t "
            "answer. MyEditor keeps looking for them there and moves them once they answer.",
            len(relays)).format(relays=", ".join(relays)))
        self._state = SAVED
        specs = self._buttons()
        self._shown_buttons = [spec[:3] for spec in specs]
        self.show_page(LEFT_BEHIND, [spec[:4] for spec in specs])
        # The page's one button takes the focus from the list just hidden
        # (a button laid out now is shown only later: show it first).
        self.buttons["done"].show()
        self.buttons["done"].setFocus()

    @staticmethod
    def _why_not_saved(outcome: SaveOutcome) -> str:
        move, write = outcome.move, outcome.write
        if move.unread:
            return ngettext(
                "Couldn’t read the drafts on {relays}, so nothing was changed. Check your "
                "internet connection, then try again. If that relay is gone for good, choose "
                "Save Anyway: the drafts kept only there won’t be shown any more.",
                "Couldn’t read the drafts on {relays}, so nothing was changed. Check your "
                "internet connection, then try again. If those relays are gone for good, "
                "choose Save Anyway: the drafts kept only there won’t be shown any more.",
                len(move.unread)).format(relays=", ".join(move.unread))
        if move.unpaged:
            return ngettext(
                "{relays} sends only its newest drafts, whichever part MyEditor asks for, so "
                "the older ones there can’t be moved, and nothing was changed. If you can do "
                "without the drafts kept only there, choose Save Anyway: they won’t be shown "
                "any more.",
                "{relays} send only their newest drafts, whichever part MyEditor asks for, so "
                "the older ones there can’t be moved, and nothing was changed. If you can do "
                "without the drafts kept only there, choose Save Anyway: they won’t be shown "
                "any more.",
                len(move.unpaged)).format(relays=", ".join(move.unpaged))
        if move.unreadable:
            return ngettext(
                "{relays} didn’t give your drafts back when MyEditor asked, so nothing was "
                "changed. If your signer app asked you to approve signing in to it, approve "
                "that, then try again. Otherwise choose another relay.",
                "{relays} didn’t give your drafts back when MyEditor asked, so nothing was "
                "changed. If your signer app asked you to approve signing in to them, "
                "approve that, then try again. Otherwise choose other relays.",
                len(move.unreadable)).format(relays=", ".join(move.unreadable))
        if move.not_moved and move.refused:
            return _("The relays your drafts would move to turned some of them down, so "
                     "nothing was changed. Choose other relays, then try again.")
        if move.not_moved:
            return _("Some drafts couldn’t be moved, so nothing was changed. Check your "
                     "internet connection, then try again.")
        if write is not None and write.status == UNKNOWN_BASE:
            return _("Couldn’t read the current setting, so nothing was changed. Check "
                     "your internet connection, then try again.")
        cause = write.cause if write is not None else ""
        if cause == CAUSE_CLOCK:
            return _("The current setting is dated ahead of this computer’s clock, so "
                     "nothing was changed. Check the date and time on this computer, then "
                     "try again.")
        if cause == CAUSE_RELAYS:
            return _("Too few relays took the change. Check your internet connection, "
                     "then try again.")
        if cause == CAUSE_SIGNER:
            return _("Your signer app didn’t approve the change. Try again, and approve "
                     "it when your signer app asks.")
        return _("Couldn’t save the change. Try again.")

    def _changed(self) -> bool:
        return self.chosen() != self._read

    # -- state and buttons -------------------------------------------------------

    def _set_state(self, state: str, *, busy: str = "", error: str = "") -> None:
        self._state = state
        self._busy_label.setText(busy)
        self._progress.setRange(0, 0)       # a wait of unknown length, until counted
        self._busy.setVisible(bool(busy))
        if error:
            self._say(error)
        else:
            self._error.hide()
        self._update()

    def _say(self, text: str) -> None:
        self._error.setText(text)
        self._error.show()
        # Read with the field it is about, by a screen reader too.
        self._field.setAccessibleDescription(text)

    def _offers_save_anyway(self) -> bool:
        return (self._state == EDITING and self._left_unread is not None
                and self._left_unread == self.chosen())

    def _update(self) -> None:
        if self._left_unread is not None and self._left_unread != self.chosen():
            # Another choice now: what the failure said is about the old one.
            self._left_unread = None
            self._unread = []
            self._error.hide()
        editing = self._state == EDITING
        # Where drafts go is said once it is known, not while reading.
        known = self._state in (EDITING, SAVING)
        # The setting as it is, or, once the form differs from it, as it
        # will be after Save (not as if it were so already).
        self._summary.setText(self._summary_text())
        self._summary.setVisible(known)
        self._list.setEnabled(editing)
        self._field.setEnabled(editing)
        self._add.setEnabled(editing and bool(self._field.text().strip()))
        selected = self._list.currentItem()
        self._remove.setEnabled(editing and selected is not None and selected.isSelected())
        left = self._left_list.currentItem()
        self._left_list.setEnabled(editing)
        self._stop_looking.setEnabled(editing and left is not None and left.isSelected())
        if not self._error.isVisible():
            self._field.setAccessibleDescription("")
        self._update_buttons()

    def _summary_text(self) -> str:
        """Where drafts go: as the setting is, or, once the form differs from
        it, as it will be after Save. Ends with a colon when the list under
        it shows the relays."""
        if self.chosen():
            return (_("Drafts will be saved only on these relays:") if self._changed()
                    else _("Drafts are saved only on these relays:"))
        if self._list.count():
            return (_("Drafts will be kept on your usual relays:") if self._changed()
                    else _("Drafts are kept on your usual relays:"))
        return (_("Drafts will be kept on your usual relays.") if self._changed()
                else _("Drafts are kept on your usual relays."))

    def _buttons(self) -> list:
        """``(key, label, placement, handler, enabled)`` for the state."""
        if self._state == READ_FAILED:
            return [("cancel", _("Cancel"), NORMAL, self.reject, True),
                    ("retry", _("Try Again"), DEFAULT, self._read_list, True)]
        if self._state == SAVED:
            return [("done", _("Done"), DEFAULT, self.accept, True)]
        editing = self._state == EDITING
        if self._offers_save_anyway():
            leading = ("anyway", _("Save Anyway"), LEADING, self._save_anyway, True)
        else:
            leading = ("usual", _("Use My Usual Relays"), LEADING, self._use_usual_relays,
                       editing and bool(self.chosen()))
        return [leading,
                ("cancel", _("Cancel"), NORMAL, self.reject, self._can_cancel()),
                ("save", _("Save"), DEFAULT, self._save, editing and self._changed())]

    def _can_cancel(self) -> bool:
        return self._state != SAVING or not self._list_saving

    def _update_buttons(self) -> None:
        """Lay the buttons out again only when they change (a new layout
        would take the focus from the one in use), and keep their enabled
        state current."""
        specs = self._buttons()
        shape = [spec[:3] for spec in specs]
        if shape != self._shown_buttons:
            self._shown_buttons = shape
            self.set_buttons([spec[:4] for spec in specs])
        for key, _label, _placement, _handler, enabled in specs:
            self.buttons[key].setEnabled(enabled)

    # -- closing -----------------------------------------------------------------

    def reject(self) -> None:
        # Drafts being read or moved can be left where they are; a list on
        # its way to the signer and the relays can't be called back: the
        # sheet stays until it is answered, and says so.
        if self._state == SAVING and not (self._can_cancel() and self._relays.cancel()):
            return
        super().reject()

    def done(self, result: int) -> None:
        self._generation += 1           # an answer still on its way is dropped
        super().done(result)
