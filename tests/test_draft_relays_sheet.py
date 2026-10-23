# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Draft Relays sheet (nostr/ui/draft_relays_sheet.py).

What must hold: it reads the account's list before anything can change,
and says so when it can't (with Try Again); an address is added only
when it can be a relay for drafts (wss://, or ws:// on this computer),
once, and at most ten; Use My Usual Relays empties the form and Save
makes it so; Save moves the drafts, with progress, before it publishes
the encrypted list and moves the drafts' routing; a save that failed
says why, with the cause, and keeps the sheet; a relay being left that
did not answer offers Save Anyway; a list changed elsewhere is read
again rather than written over; a save can be cancelled until the list
itself is on its way, and not after; Return in the address field adds it; the focus starts on the
list or the field; every control has a name.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.draft_relays import (  # noqa: E402
    MOVING_DRAFTS, SAVING_LIST, UNDECRYPTED, UNREACHABLE, MoveOutcome, SaveOutcome,
)
from nostr.outbox import defaults  # noqa: E402
from nostr.outbox.writer import (  # noqa: E402
    CAUSE_CHANGED, CAUSE_CLOCK, CAUSE_RELAYS, CAUSE_SIGNER, FAILED, UNKNOWN_BASE, WRITTEN,
    WriteOutcome,
)
from nostr.ui.draft_relays_sheet import (  # noqa: E402
    EDITING, READ_FAILED, SAVING, DraftRelaysSheet,
)
from tests.accessibility import unnamed_controls  # noqa: E402
from tests.outbox_fakes import PK  # noqa: E402
from tests.widget_lifetime import delete_new_windows  # noqa: E402
from tests.test_draft_relays import (  # noqa: E402
    LIST_ON_MINE, MINE, U1, U2, moving, sent, wrap,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every sheet a test opens is deleted after it: left to the cycle
    collector, one could be destroyed in the middle of a later test."""
    yield from delete_new_windows()


SAVED = SaveOutcome(MoveOutcome(), WriteOutcome(WRITTEN))


def list_failed(status, cause, event=None):
    return SaveOutcome(MoveOutcome(), WriteOutcome(status, event=event, cause=cause))


class Lists(QObject):
    """Stands in for PrivateDraftRelays: answers reads, and answers saves
    at once, or with ``answer=None`` keeps one under way until ``finish``
    (``step`` reports its progress). It holds the sheet's callbacks only
    while a save is under way, as the real one does."""

    left_behind_changed = Signal()

    def __init__(self, relays=(), *, readable=True, answer=SAVED, failure=UNREACHABLE,
                 usual=("wss://usual-1.example", "wss://usual-2.example")):
        super().__init__()
        self.relays = list(relays)
        self.usual = list(usual)
        self.readable = readable
        self.failure = failure
        self.answer = answer
        self.saves = []
        self._pending = None
        self.last_failure = None
        self.cancelled = 0
        self.list_on_its_way = False
        self.left_behind = []
        self.asked_again = 0

    def ask_again(self):
        self.asked_again += 1

    def drafts_left_behind(self):
        return list(self.left_behind)

    def stop_reading(self, relay):
        self.left_behind.remove(relay)

    def cancel(self):
        if self.list_on_its_way:
            return False
        self.cancelled += 1
        self._pending = None
        return True

    def usual_relays(self, on_done):
        on_done(list(self.usual))

    def read(self, on_done):
        self.last_failure = None if self.readable else self.failure
        on_done(list(self.relays) if self.readable else None)

    def save(self, relays, on_done, *, on_step=None, leave_unread=()):
        self.saves.append((list(relays), list(leave_unread)))
        if self.answer is None:
            self._pending = (on_done, on_step)
        else:
            on_done(self.answer)

    def step(self, *where):
        self.list_on_its_way = where[0] == SAVING_LIST
        self._pending[1](*where)

    def finish(self, outcome):
        on_done, _on_step = self._pending
        self._pending = None
        on_done(outcome)


def settle():
    for _ in range(6):
        QApplication.processEvents()


def open_sheet(lists):
    sheet = DraftRelaysSheet(lists, is_dark=False)
    sheet.show()
    saved = []
    sheet.saved.connect(saved.append)
    return sheet, saved


def add(sheet, text):
    sheet._field.setText(text)
    sheet._add.click()


def words(sheet):
    return [sheet.buttons[key].text() for key in sheet.buttons]


def test_it_reads_first_and_edits_what_it_read():
    sheet, _saved = open_sheet(Lists(["wss://mine.example"]))
    assert sheet.state == EDITING and sheet.chosen() == ["wss://mine.example"]
    assert sheet._summary.text() == "Drafts are saved only on these relays:"
    assert words(sheet) == ["Use My Usual Relays", "Cancel", "Save"]
    assert not sheet.buttons["save"].isEnabled()          # nothing changed yet
    assert sheet.buttons["save"].isDefault()
    assert unnamed_controls(sheet) == []


def shown(sheet):
    """Each row of the list: its address, and whether it can be selected
    (a usual relay is shown, not chosen: it can't)."""
    rows = [sheet._list.item(row) for row in range(sheet._list.count())]
    return [(item.text(), bool(item.flags() & Qt.ItemFlag.ItemIsSelectable)) for item in rows]


USUAL = [("wss://usual-1.example", False), ("wss://usual-2.example", False)]


def test_an_account_without_a_list_shows_its_usual_relays():
    """As macOS shows the DNS servers it got by itself: dimmed, in the list
    where chosen ones would be, until one is added."""
    sheet, _saved = open_sheet(Lists([]))
    assert sheet._summary.text() == "Drafts are kept on your usual relays:"
    assert shown(sheet) == USUAL and sheet.chosen() == []
    assert not sheet.buttons["usual"].isEnabled() and not sheet._remove.isEnabled()


def test_adding_a_relay_takes_the_place_of_the_usual_ones():
    sheet, _saved = open_sheet(Lists([]))
    add(sheet, "wss://mine.example")
    assert shown(sheet) == [("wss://mine.example", True)]
    assert sheet.chosen() == ["wss://mine.example"]
    assert sheet._summary.text() == "Drafts will be saved only on these relays:"
    assert sheet.buttons["usual"].isEnabled() and sheet.buttons["save"].isEnabled()


def test_the_usual_relays_come_back_when_none_is_chosen():
    sheet, _saved = open_sheet(Lists(["wss://mine.example"]))
    sheet._list.setCurrentRow(0)
    sheet._remove.click()
    assert shown(sheet) == USUAL and sheet.chosen() == []
    assert sheet._summary.text() == "Drafts will be kept on your usual relays:"
    sheet2, _saved2 = open_sheet(Lists(["wss://mine.example"]))
    sheet2.buttons["usual"].click()
    assert shown(sheet2) == USUAL and sheet2.chosen() == []


def test_usual_relays_not_known_yet_leave_the_list_empty_and_say_so_plainly():
    sheet, _saved = open_sheet(Lists([], usual=()))
    assert shown(sheet) == [] and sheet.chosen() == []
    assert sheet._summary.text() == "Drafts are kept on your usual relays."


def test_the_focus_starts_on_the_list_or_on_the_field():
    sheet, _saved = open_sheet(Lists(["wss://mine.example"]))
    assert sheet.focusWidget() is sheet._list
    assert sheet._scroll.focusPolicy() == Qt.FocusPolicy.NoFocus   # no invisible Tab stop
    sheet, _saved = open_sheet(Lists([]))
    assert sheet.focusWidget() is sheet._field


def test_a_list_the_signer_could_not_decrypt_says_so():
    sheet, _saved = open_sheet(Lists([], readable=False, failure=UNDECRYPTED))
    assert sheet.state == READ_FAILED and sheet._error.text().startswith(
        "Couldn’t decrypt where your drafts are kept. Make sure your signer app is running")


def test_a_list_that_could_not_be_read_is_not_edited_and_offers_try_again():
    lists = Lists(["wss://mine.example"], readable=False)
    sheet, _saved = open_sheet(lists)
    assert sheet.state == READ_FAILED and sheet._error.text().startswith(
        "Couldn’t read where your drafts are kept.")
    assert words(sheet) == ["Cancel", "Try Again"] and sheet.buttons["retry"].isDefault()
    assert not sheet._field.isEnabled() and not sheet._list.isEnabled()
    assert not sheet._summary.isVisibleTo(sheet)          # where drafts go is not known
    assert unnamed_controls(sheet) == []
    lists.readable = True
    sheet.buttons["retry"].click()
    assert sheet.state == EDITING and sheet.chosen() == ["wss://mine.example"]


def test_an_address_is_added_only_when_it_can_be_one_and_only_once():
    sheet, _saved = open_sheet(Lists([]))
    add(sheet, "ws://relay.example")
    assert sheet.chosen() == [] and sheet._error.isVisibleTo(sheet)
    assert sheet._error.text() == "Enter a relay’s address. It starts with wss://."
    assert sheet._field.accessibleDescription() == sheet._error.text()
    add(sheet, "wss://Relay.Example/")
    assert sheet.chosen() == ["wss://relay.example"] and not sheet._error.isVisibleTo(sheet)
    add(sheet, "wss://relay.example")
    assert sheet._error.text() == "That relay is already in the list."
    add(sheet, "ws://localhost:4869")                     # a relay on this computer
    assert sheet.chosen() == ["wss://relay.example", "ws://localhost:4869"]
    assert sheet.buttons["save"].isEnabled()


def test_return_in_the_address_field_adds_it_and_does_not_save():
    lists = Lists([])
    sheet, saved = open_sheet(lists)
    sheet._field.setFocus()
    sheet._field.setText("wss://typed.example")
    QTest.keyClick(sheet._field, Qt.Key.Key_Return)
    assert sheet.chosen() == ["wss://typed.example"] and lists.saves == [] and saved == []
    assert sheet._field.text() == ""


def test_drafts_go_to_at_most_ten_relays():
    sheet, _saved = open_sheet(Lists([f"wss://r{n}.example" for n in range(defaults.PRIVATE_CAP)]))
    add(sheet, "wss://one-more.example")
    assert len(sheet.chosen()) == defaults.PRIVATE_CAP
    assert sheet._error.text() == "Drafts can be kept on at most 10 relays."


def test_removing_a_relay():
    sheet, _saved = open_sheet(Lists(["wss://a.example", "wss://b.example", "wss://c.example"]))
    assert not sheet._remove.isEnabled()                  # nothing selected
    sheet._list.setCurrentRow(1)
    sheet._remove.click()
    assert sheet.chosen() == ["wss://a.example", "wss://c.example"]
    assert sheet._list.currentRow() == 1                  # the next one, for the keyboard
    QTest.keyClick(sheet._list, Qt.Key.Key_Delete)
    assert sheet.chosen() == ["wss://a.example"]


def test_use_my_usual_relays_empties_the_form_and_save_makes_it_so():
    lists = Lists(["wss://mine.example"])
    sheet, saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    assert sheet.chosen() == [] and sheet._summary.text() == (
        "Drafts will be kept on your usual relays:")      # once saved, not yet
    assert lists.saves == []                              # nothing changes before Save
    sheet.buttons["save"].click()
    assert lists.saves == [([], [])] and saved == [[]] and not sheet.isVisible()


def test_it_says_that_drafts_move_with_the_setting():
    sheet, _saved = open_sheet(Lists([]))
    texts = [label.text() for label in sheet.findChildren(type(sheet._summary))]
    assert any("When you change this, your drafts move with it." in text for text in texts)


def test_a_save_that_failed_says_why_and_keeps_the_sheet():
    signed_but_refused = {"kind": 10013}
    for answer, start in (
            (list_failed(UNKNOWN_BASE, CAUSE_RELAYS), "Couldn’t read the current setting"),
            (list_failed(FAILED, CAUSE_RELAYS, signed_but_refused),
             "Too few relays took the change."),
            (list_failed(FAILED, CAUSE_CLOCK, signed_but_refused),
             "The current setting is dated ahead of this computer’s clock"),
            (list_failed(FAILED, CAUSE_SIGNER), "Your signer app didn’t approve"),
            (SaveOutcome(MoveOutcome(total=2, moved=1, not_moved=("x",))),
             "Some drafts couldn’t be moved, so nothing was changed. Check your internet"),
            (SaveOutcome(MoveOutcome(total=2, moved=1, not_moved=("x",), refused=True)),
             "The relays your drafts would move to turned some of them down"),
            (SaveOutcome(MoveOutcome(total=1, moved=1, unreadable=("wss://mine.example",))),
             "wss://mine.example didn’t give your drafts back when MyEditor asked, so "
             "nothing was changed. If your signer app asked you to approve signing in")):
        sheet, saved = open_sheet(Lists([], answer=answer))
        add(sheet, "wss://mine.example")
        sheet.buttons["save"].click()
        assert sheet.isVisible() and saved == [] and sheet.state == EDITING
        assert sheet._error.text().startswith(start), sheet._error.text()
        assert sheet.buttons["save"].isEnabled()          # try again
        assert "anyway" not in [spec[0] for spec in sheet._shown_buttons]


def test_a_relay_being_left_that_did_not_answer_offers_save_anyway():
    lists = Lists([MINE], answer=SaveOutcome(MoveOutcome(unread=(MINE,))))
    sheet, saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    assert sheet._error.text().startswith(f"Couldn’t read the drafts on {MINE}")
    assert "If that relay is gone for good, choose Save Anyway" in sheet._error.text()
    assert words(sheet) == ["Save Anyway", "Cancel", "Save"]
    assert unnamed_controls(sheet) == []
    lists.answer = SAVED
    sheet.buttons["anyway"].click()
    assert lists.saves == [([], []), ([], [MINE])] and saved == [[]]   # MINE only


def test_after_a_failed_save_the_summary_does_not_state_the_form_as_the_setting():
    lists = Lists([MINE], answer=SaveOutcome(MoveOutcome(unread=(MINE,))))
    sheet, _saved = open_sheet(lists)
    assert sheet._summary.text() == "Drafts are saved only on these relays:"
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    assert sheet._error.text().endswith("won’t be shown any more.")
    assert sheet._summary.text() == "Drafts will be kept on your usual relays:"
    add(sheet, MINE)                                      # back to the setting as read
    assert sheet._summary.text() == "Drafts are saved only on these relays:"


def test_a_relay_that_sends_only_its_newest_drafts_is_named_for_that_and_can_be_left():
    # Review M2: it was blamed on the internet connection, and Save Anyway
    # could not get past it.
    lists = Lists([MINE], answer=SaveOutcome(MoveOutcome(unpaged=(MINE,))))
    sheet, saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    assert sheet._error.text().startswith(
        f"{MINE} sends only its newest drafts, whichever part MyEditor asks for")
    assert "If you can do without the drafts kept only there" in sheet._error.text()
    assert "internet connection" not in sheet._error.text()
    assert words(sheet) == ["Save Anyway", "Cancel", "Save"]
    lists.answer = SAVED
    sheet.buttons["anyway"].click()
    assert lists.saves[-1] == ([], [MINE]) and saved == [[]]


def test_relays_agreed_to_leave_add_up_while_the_choice_stays():
    # Review N3, live: agreeing to leave MINE's drafts, then SECOND going
    # quiet, must not ask about MINE again.
    second = "wss://second.example"
    lists = Lists([MINE, second], answer=SaveOutcome(MoveOutcome(unread=(MINE,))))
    sheet, saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    lists.answer = SaveOutcome(MoveOutcome(unread=(second,)))
    sheet.buttons["anyway"].click()
    assert sheet._error.text().startswith(f"Couldn’t read the drafts on {second}")
    lists.answer = SAVED
    sheet.buttons["anyway"].click()
    assert lists.saves == [([], []), ([], [MINE]), ([], [MINE, second])] and saved == [[]]


def test_save_anyway_goes_once_the_choice_changes():
    lists = Lists([MINE, "wss://two.example"],
                  answer=SaveOutcome(MoveOutcome(unread=(MINE,))))
    sheet, _saved = open_sheet(lists)
    sheet._list.setCurrentRow(0)
    sheet._remove.click()
    sheet.buttons["save"].click()
    assert words(sheet)[0] == "Save Anyway"
    sheet._list.setCurrentRow(0)
    sheet._remove.click()                                 # another choice now
    assert words(sheet) == ["Use My Usual Relays", "Cancel", "Save"]
    assert not sheet._error.isVisibleTo(sheet)


def test_drafts_left_on_a_relay_that_went_quiet_are_said_once_saved():
    lists = Lists([MINE], answer=SaveOutcome(MoveOutcome(), WriteOutcome(WRITTEN),
                                             left_behind=(MINE,)))
    sheet, saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    assert saved == [[]] and sheet.isVisible()            # saved, with a word on it
    assert sheet._left_note.text().startswith(
        f"Drafts saved during the change may still be only on {MINE}, which didn’t answer.")
    assert words(sheet) == ["Done"] and sheet.focusWidget() is sheet.buttons["done"]
    assert unnamed_controls(sheet) == []
    QApplication.processEvents()
    # A short page after the form: nothing to scroll in a window that tall.
    assert not sheet._scroll.verticalScrollBar().isVisible()
    sheet.buttons["done"].click()
    assert not sheet.isVisible()


def test_a_list_changed_elsewhere_is_read_again_and_not_written_over():
    lists = Lists([MINE], answer=list_failed(FAILED, CAUSE_CHANGED))
    sheet, saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    lists.relays = ["wss://else.example"]                 # as the other device left it
    sheet.buttons["save"].click()
    assert saved == [] and sheet.state == EDITING
    assert sheet.chosen() == ["wss://else.example"]
    assert sheet._error.text().startswith("The setting was changed on another device")


def test_moving_the_drafts_is_counted():
    lists = Lists([MINE], answer=None)
    sheet, _saved = open_sheet(lists)
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    assert sheet._busy_label.text() == "Reading your drafts…"
    assert sheet._progress.maximum() == 0                 # not counted yet
    lists.step(MOVING_DRAFTS, 0, 14, 12)
    assert sheet._busy_label.text() == "Moving 12 drafts…"
    assert (sheet._progress.maximum(), sheet._progress.value()) == (14, 0)
    lists.step(MOVING_DRAFTS, 5, 14, 12)
    assert sheet._progress.value() == 5
    lists.step(MOVING_DRAFTS, 0, 2, 0)                   # only deleted ones to carry
    assert sheet._busy_label.text() == "Moving your drafts…"
    lists.step(SAVING_LIST, 0, 0, 0)
    assert sheet._busy_label.text().startswith("Saving. Approve the request")
    assert sheet._progress.maximum() == 0
    lists.finish(SAVED)
    assert not sheet.isVisible()


def test_a_save_can_be_cancelled_until_the_list_is_on_its_way():
    lists = Lists([], answer=None)
    sheet, saved = open_sheet(lists)
    add(sheet, "wss://mine.example")
    sheet.buttons["save"].click()
    assert sheet.state == SAVING and sheet._busy.isVisibleTo(sheet)
    assert sheet.buttons["cancel"].isEnabled() and not sheet._field.isEnabled()
    lists.step(MOVING_DRAFTS, 1, 4, 4)
    QTest.keyClick(sheet, Qt.Key.Key_Escape)
    assert lists.cancelled == 1 and saved == [] and not sheet.isVisible()


def test_a_list_on_its_way_cannot_be_called_back():
    lists = Lists([], answer=None)
    sheet, saved = open_sheet(lists)
    add(sheet, "wss://mine.example")
    sheet.buttons["save"].click()
    lists.step(SAVING_LIST, 0, 0, 0)
    assert not sheet.buttons["cancel"].isEnabled()
    sheet.reject()                                        # Escape, the close box
    assert sheet.isVisible() and lists.cancelled == 0
    lists.finish(SAVED)
    assert saved == [["wss://mine.example"]] and not sheet.isVisible()


def test_an_answer_after_the_sheet_closed_is_dropped():
    lists = Lists([], answer=None)
    sheet, saved = open_sheet(lists)
    add(sheet, "wss://mine.example")
    sheet.buttons["save"].click()
    sheet.done(0)
    lists.step(MOVING_DRAFTS, 1, 2, 2)                   # dropped too
    lists.finish(SAVED)
    assert saved == []


def test_save_moves_the_drafts_then_publishes_the_list_and_drafts_follow_it():
    draft = wrap("draft-1")
    lists, relays, directory, _client = moving(LIST_ON_MINE, drafts_on={MINE: [draft]})
    sheet, saved = open_sheet(lists)
    relays.run()
    assert sheet.state == EDITING and sheet.chosen() == [MINE]
    sheet.buttons["usual"].click()
    sheet.buttons["save"].click()
    relays.run(rounds=20)
    assert saved == [[]] and not sheet.isVisible()
    (moved_to, moved), (_to, listed) = sent(relays)
    assert moved == draft and set(moved_to) == {U1, U2}  # the draft first, the list last
    assert listed["kind"] == 10013 and listed["tags"] == []
    assert listed["content"] == "sealed:[]"
    assert directory.draft_relays_of(PK) == []


def test_relays_still_looked_at_are_listed_and_one_can_be_let_go(monkeypatch):
    # Review L11: relays left behind were read for good, and the person
    # could neither see them nor stop it.
    import nostr.ui.draft_relays_sheet as sheet_module
    asked, answers = [], [False, True]
    monkeypatch.setattr(sheet_module, "confirm_destructive",
                        lambda parent, *, title, message, action, **_kw: (
                            asked.append((title, message, action)), answers.pop(0))[1])
    gone = "wss://old.example"
    lists = Lists([MINE])
    sheet, _saved = open_sheet(lists)
    assert not sheet._left.isVisibleTo(sheet)                  # none: nothing shown
    sheet.done(0)
    lists.left_behind = [gone]
    sheet, _saved = open_sheet(lists)
    assert sheet._left.isVisibleTo(sheet)
    assert [sheet._left_list.item(0).text()] == [gone]
    assert not sheet._stop_looking.isEnabled()                  # nothing selected
    assert unnamed_controls(sheet) == []
    sheet._left_list.setCurrentRow(0)
    sheet._stop_looking.click()                                 # Cancel, the default
    assert asked == [("Stop looking for drafts on this relay?",
                      f"Drafts kept only on {gone} won’t be shown any more.", "Stop Looking")]
    assert lists.left_behind == [gone]
    sheet._left_list.setCurrentRow(0)
    sheet._stop_looking.click()                                 # Stop Looking
    assert lists.left_behind == [] and not sheet._left.isVisibleTo(sheet)



def test_the_buttons_keep_their_words_whole_at_the_narrowest_width():
    # Round 2 screenshots: at its narrowest the German sheet cut
    # "Meine üblichen Relays verwenden" short.
    import i18n
    i18n.install("de")
    try:
        sheet, _saved = open_sheet(Lists([MINE]))
        sheet.resize(sheet.minimumWidth(), sheet.height())
        QApplication.processEvents()
        assert all(button.width() >= button.sizeHint().width()
                   for button in sheet.buttons.values()), [
            (b.text(), b.width(), b.sizeHint().width()) for b in sheet.buttons.values()]
    finally:
        i18n.install("en")


def test_a_long_address_still_looked_at_is_shortened_in_the_middle_not_hidden():
    # Review round 3, L14: a long address put a sideways scroll bar over
    # the only row, and the relay the person decides about could not be seen.
    long = "wss://relay.example.org/a/very/long/path/that/goes/on/and/on/for/drafts/only"
    lists = Lists([MINE])
    lists.left_behind = [long]
    sheet, _saved = open_sheet(lists)
    sheet.resize(sheet.minimumWidth(), sheet.height())
    QApplication.processEvents()
    view = sheet._left_list
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert view.textElideMode() == Qt.TextElideMode.ElideMiddle
    assert view.viewport().height() >= view.sizeHintForRow(0)        # the row whole
    item = view.item(0)
    assert item.toolTip() == long and item.data(Qt.ItemDataRole.AccessibleTextRole) == long


def test_the_relays_still_looked_at_follow_a_move_in_the_background():
    # Review round 3, note: a relay cleared by a background move stayed listed.
    lists = Lists([MINE])
    lists.left_behind = ["wss://old.example"]
    sheet, _saved = open_sheet(lists)
    assert sheet._left_list.count() == 1
    lists.left_behind = []
    lists.left_behind_changed.emit()                        # its drafts moved
    assert sheet._left_list.count() == 0 and not sheet._left.isVisibleTo(sheet)


def test_the_person_asking_again_lets_a_declined_sign_in_be_asked_again():
    # Review round 2, R1: "approve that, then try again" has to be true.
    lists = Lists(["wss://mine.example"], readable=False)
    sheet, _saved = open_sheet(lists)
    opened = lists.asked_again
    lists.readable = True
    sheet.buttons["retry"].click()
    assert lists.asked_again == opened + 1
    add(sheet, "wss://two.example")
    sheet.buttons["save"].click()
    assert lists.asked_again == opened + 2


@pytest.mark.parametrize("mac", [True, False])
def test_a_button_that_dims_itself_hands_the_focus_to_save(monkeypatch, mac):
    # Review L1 of the merge: on Windows and Linux, Return pressed the
    # focused Use My Usual Relays, which then dimmed itself; the focus fell
    # to Cancel, which took the default, and the next Return dropped the
    # change. The focus goes to the sheet's action, Save, on every system
    # (on a Mac the focused button is pressed with Space).
    import alerts
    monkeypatch.setattr(alerts, "IS_MAC", mac)
    sheet, saved = open_sheet(Lists([MINE]))
    sheet.activateWindow()
    settle()                                        # in front, as when a person uses it
    try:
        usual = sheet.buttons["usual"]
        usual.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        QTest.keyClick(QApplication.focusWidget(),
                       Qt.Key.Key_Space if mac else Qt.Key.Key_Return)
        settle()
        assert not usual.isEnabled()                    # pressed, and dimmed itself
        assert QApplication.focusWidget() is sheet._action
        assert sheet._action.text() == "Save"
    finally:
        sheet._generation += 1
        sheet.reject()


def test_the_list_follows_the_apps_appearance_and_keeps_its_height():
    """Dark in the app's dark appearance whatever the system's (it took the
    system's colors), and five rows high however tall the sheet grows, as
    a list in a settings sheet is."""
    from PySide6.QtGui import QColor
    from nostr.ui.appearance import roles
    sheet = DraftRelaysSheet(Lists([]), is_dark=True)
    sheet.resize(560, 560)
    sheet.show()
    settle()
    height = sheet._list.height()
    image = sheet._list.viewport().grab().toImage()
    corner = image.pixelColor(image.width() - 8, image.height() - 8)
    expected = QColor(roles(True).field)
    assert all(abs(a - b) <= 4 for a, b in zip(corner.getRgb()[:3], expected.getRgb()[:3]))
    sheet.resize(560, 900)
    settle()
    assert sheet._list.height() == height
    sheet.close()
