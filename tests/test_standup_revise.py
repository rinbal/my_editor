# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Neue Fassung abgeben (nostr/ui/standup_revise_sheet.py, plan 8.1),
against the checking fake server with the real list.

Pinned here: the sheet says what changed in one line; Abgeben hands in
the next version with the keys its submission has, and it counts only
with its receipt; the note goes with the version and shows under its
card; the same text again is not handed in; the changes show word by
word on a page of the sheet; Einreichung ansehen opens the detail; and
the sheet closes when its account is let go."""

from __future__ import annotations

import time

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from nostr.standup import thread as threads
from nostr.standup.compare import ADDED, SAME
from nostr.standup.drafting import ArticleDraft
from nostr.standup.submitting import Phase
from nostr.ui.standup_compare import Comparer
from nostr.ui.standup_revise_sheet import ReviseSheet, change_line, comparable
from tests.standup_following_fakes import FollowWorld
from tests.test_standup_submissions_ui import ME, NAMES, Window

FIRST = "Der Anfang war leicht."
SECOND = FIRST + "\n\nUnd die Buchhaltung? Lisa trägt jede Zahlung abends ein."


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(monkeypatch):
    return Window(FollowWorld(), monkeypatch)


def inline(fn, on_done, on_error):
    on_done(fn())


def sheet_for(window, content=SECOND):
    """The sheet for a submission whose changes were requested, as the
    window opens it: its row, the version handed in, the text now."""
    world = window.world
    # The version's note is sealed at the time it is sent (standup_message
    # has no clock of its own): the world's clock stands at that time.
    world.clock.now = time.time()
    seed = world.account()
    prepared, entry = world.submit(seed, d="text", title="Ein Jahr", content=FIRST)
    world.decide(prepared, "request_changes", text="Bitte einen Absatz mehr.")
    window.follow()
    window.view.show_detail(entry.sid)
    world.settle()
    following = window.following
    draft = ArticleDraft(d="text", title="Ein Jahr", summary="", content=content)
    sheet = ReviseSheet(
        writer=window.writer, profile=window.profile, draft=draft, cover=QPixmap(),
        byline="Jana · Article", row=following.row(entry.sid),
        publication="Einundzwanzig Magazin", logo=QPixmap(),
        previous=following.reading.latest(entry.sid), comparer=Comparer(run=inline),
        is_dark=False)
    return sheet, entry


def test_a_new_version_is_handed_in_with_its_note_and_counts_with_its_receipt(window):
    world = window.world
    sheet, entry = sheet_for(window)
    assert sheet.heading.text() == "Version 2 to Einundzwanzig Magazin"
    assert sheet.changed.text() == "Changed: 1 paragraph new"
    assert sheet.hand_in.isEnabled() and sheet.hand_in.isDefault()
    sheet.note.setPlainText("Absatz zur Buchhaltung ist drin.")
    sheet.hand_in.click()
    world.settle()
    assert sheet.run.progress.phase is Phase.DONE
    record = world.record(entry.sid)
    assert record.revision == 2 and record.state == "in_review"
    listed = window.accounts[ME].submissions.entry(entry.sid)
    assert (listed.revision, listed.stored) == (2, True)
    assert (listed.s_sub, listed.status_key) == (entry.s_sub, entry.status_key)   # its keys
    # Einreichung ansehen, once it is in.
    seen = []
    sheet.view_submission.connect(seen.append)
    assert not sheet.handoff.view_button.isHidden()
    sheet.handoff.view_button.click()
    assert seen == [entry.sid]
    # The card of version 2: its one line, and the note under it.
    window.following.open(entry.sid)
    world.settle()
    detail = window.following.detail(entry.sid)
    [card] = [item for item in detail.thread if isinstance(item, threads.VersionCard)]
    assert threads.card_title(card, me=ME, name_of=NAMES.get) == "Version 2 by you"
    assert card.note == "Absatz zur Buchhaltung ist drin."
    assert detail.versions[2].change_summary == "1 paragraph new"
    assert detail.versions[2].work["content"] == SECOND
    assert not any(isinstance(item, threads.Message) and item.mine for item in detail.thread)


def test_the_same_text_again_is_not_handed_in(window):
    sheet, _entry = sheet_for(window, content=FIRST)
    assert not sheet.hand_in.isEnabled()
    assert sheet.blockers() == [
        "This text is the same as version 1. Change it to hand in a new one."]
    sheet.hand_in.click()
    assert sheet.run is None


def test_the_changes_show_word_by_word_on_a_page_of_the_sheet(window):
    sheet, _entry = sheet_for(window)
    sheet.show_changes.click()
    marked = [(kind, text) for kind, text in sheet.changes.marked() if kind != SAME]
    assert marked == [(ADDED, "Und die Buchhaltung? Lisa trägt jede Zahlung abends ein.")]
    sheet.reject()                                      # Escape: back to the form
    assert sheet._pages.currentIndex() == 0 and sheet.result() == 0


def test_a_picture_named_anew_in_each_version_is_no_change():
    a = "Text.\n\n![Café](standup-media:" + "a" * 64 + ")"
    b = "Text.\n\n![Café](myeditor-asset:" + "b" * 64 + ")"
    assert comparable(a) == comparable(b)
    draft = ArticleDraft(d="d", title="T", summary="", content=b)
    assert change_line(draft, {"title": "T", "summary": "", "content": a, "tags": []}) == ""
    assert change_line(draft, {"title": "Alt", "content": a}) == "Details changed"


def test_the_sheet_closes_when_its_account_is_let_go(window):
    world = window.world
    sheet, entry = sheet_for(window)
    signer = window.accounts[ME].sign
    posted = len(world.server.requests)
    sheet.open()
    sheet.note.setPlainText("Kommt gleich.")
    sheet.hand_in.click()                               # the run starts
    window.writer.stop()                                # sign-out while it is on its way
    world.settle()
    assert sheet.run is None and not sheet.isVisible()
    assert world.record(entry.sid).revision == 1        # nothing was handed in
    submits = [path for verb, path, _b, _h in world.server.requests[posted:]
               if verb == "POST" and path == "/api/submissions"]
    assert submits == [] and signer is not None
