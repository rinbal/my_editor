# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Einreichungen view, the detail, the pill and the notice (nostr/ui/
submissions_view.py, submission_detail.py, submission_thread.py,
submission_pill.py, submissions_host.py), driven as the window drives
them, against the checking fake server with the real list.

Pinned here: the three sections with one status word a row and Erledigt
closed with its count; the empty list's way to submit; a row opens the
detail and ‹ Einreichungen comes back to it; the detail's sentence with
at most one action, and a button only for an action the window handles
(publish and confirm are a later phase's); the composer, the failure in
place with the text kept; reactions on the bubble; the withdraw alert
with Cancel as the default; the pill; the notice; Im Editor öffnen; and
that a view closed while an answer is pending raises nothing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import alerts
from document_notice import DocumentNotice
from nostr.standup import conversation
from nostr.standup.following import Action
from nostr.ui import standup_submit, submission_detail
from nostr.ui.standup_submit import StandupWriter
from nostr.ui.submission_pill import SubmissionPill
from nostr.ui.submissions_host import Ports, SubmissionsHost
from nostr.ui.submissions_view import PANEL_VIEW, SubmissionsView
from tests.standup_fakes import DROP_BEFORE, SERVER
from tests.standup_following_fakes import FollowWorld
from tests.standup_vectors import pk

ME, EDITOR, OTHER = pk("writer"), pk("editor"), pk("contributor")
NAMES = {EDITOR: "Anna", ME: "Jana", OTHER: "Paul"}


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


class Window:
    """The window's side: one writer, the view, the pill, the host, and
    tabs that are plain values (an article ``d``, its text, its notice)."""

    def __init__(self, world: FollowWorld, monkeypatch, *, server: str = SERVER) -> None:
        self.world = world
        monkeypatch.setattr(standup_submit, "_later", world.later)
        self.accounts = {}
        self.profile = SimpleNamespace(user_pubkey=ME, bunker_relays=[])
        self.writer = StandupWriter(
            session_pool=None, relay_pool=None, relay_directory=None, media_store=None,
            server=server, uploads=lambda *a: (lambda: None), accounts=self.account,
            clock=world.clock)
        self.view = SubmissionsView(name_of=lambda p: NAMES.get(p, ""),
                                    avatar_of=lambda p: None, is_dark=False, clock=world.clock)
        self.view.resize(380, 640)
        self.pill = SubmissionPill()
        self.tabs, self.current = [], None
        self.said, self.shown, self.opened, self.submits = [], [], [], []
        self.host = SubmissionsHost(Ports(
            standup=lambda: self.writer, profile=lambda: self.profile,
            current_editor=lambda: self.current,
            editor_of=lambda d: next((t for t in self.tabs if t.d == d), None),
            identifier=lambda ed: ed.d, text_of=lambda ed: ed.text,
            focus=lambda ed: setattr(self, "current", ed),
            open_text=lambda *a: self.opened.append(a), submission_of=lambda ed, p: None,
            show_view=self.shown.append, submit=lambda: self.submits.append(self.current),
            notice_of=lambda ed: ed.notice, say=self.said.append, keep=lambda sheet: None,
            avatar_of=lambda p: None, request_logo=lambda *a: None, is_dark=lambda: False,
            window=lambda: None), view=self.view, pill=self.pill,
            name_of=lambda p: NAMES.get(p, ""))

    def account(self, profile):
        who = {ME: "writer", OTHER: "contributor"}[profile.user_pubkey]
        self.accounts[profile.user_pubkey] = self.world.account(who)
        return self.accounts[profile.user_pubkey]

    def tab(self, d: str, text: str = ""):
        ed = QObject()                              # an editor is a Qt object
        ed.d, ed.text, ed.notice = d, text, DocumentNotice()
        self.tabs.append(ed)
        self.current = ed
        return ed

    def follow(self) -> None:
        self.host.follow()
        self.world.settle()

    @property
    def following(self):
        return self.writer.followed(self.profile)

    def rows(self):
        return [(i.kind, i.title, i.word) for i in self.view.items()]


@pytest.fixture
def window(monkeypatch):
    return Window(FollowWorld(), monkeypatch)


def seeded(window, **submissions):
    """Submissions of the writer, made before this start of the app."""
    seed = window.world.account()
    made = {d: window.world.submit(seed, d=d, title=title)
            for d, title in submissions.items()}
    return {d: prepared for d, (prepared, _entry) in made.items()}, \
        {d: entry for d, (_prepared, entry) in made.items()}


# -- the list -------------------------------------------------------------------------

def test_the_list_has_three_sections_one_word_a_row_and_done_closed_with_its_count(window):
    world = window.world
    prepared, _entries = seeded(window, aendern="Ein Jahr", laeuft="Steuern", fertig="Wallets")
    world.take(prepared["aendern"])
    world.decide(prepared["aendern"], "request_changes", text="Bitte kürzen.")
    world.decide(prepared["fertig"], "decline")
    window.follow()
    assert window.rows() == [
        ("header", "Your Turn", ""), ("row", "Ein Jahr", "Changes requested"),
        ("header", "In Progress", ""), ("row", "Steuern", "Submitted"),
        ("done", "Done (1)", "")]
    turn = window.view.items()[1]
    assert turn.unread and turn.turn and turn.rest == "Anna: Bitte kürzen."
    assert turn.spoken.endswith("New")                  # the dot, to a screen reader
    done = window.view.list.model().index(4)
    window.view.list.activated.emit(done)               # Return or a click opens Erledigt
    assert window.rows()[-2:] == [("done", "Done (1)", ""), ("row", "Wallets", "Declined")]


def test_an_empty_list_says_how_to_submit_and_offers_it(window):
    window.follow()
    assert window.view.empty_title.text() == "No Submissions Yet"
    assert "Submit to a Publication" in window.view.empty_body.text()
    window.current = "the article in front"
    window.view.empty_action.click()
    assert window.submits == ["the article in front"]


def test_without_a_server_nothing_is_read_and_the_list_says_where_the_address_comes_from(
        monkeypatch):
    window = Window(FollowWorld(), monkeypatch, server="")
    window.follow()
    assert window.accounts == {} and window.following is None
    assert window.view.empty_body.text() == "The server’s address comes from the association."
    assert not window.view.empty_action.isVisible()


def test_a_row_opens_its_detail_and_back_returns_to_the_row(window):
    _prepared, entries = seeded(window, eins="Ein Jahr", zwei="Steuern")
    window.follow()
    index = window.view.list.model().index(2)
    assert index.data(Qt.ItemDataRole.DisplayRole).startswith("Steuern")
    window.view.list.activated.emit(index)
    window.world.settle()
    detail = window.view.detail
    assert window.view.showing_detail() and detail.sid == entries["zwei"].sid
    assert detail.title.text() == "Steuern"
    assert detail.sentence.text() == "In the inbox of Einundzwanzig Magazin."
    assert not detail.action.isVisibleTo(detail)        # nothing to do: no button
    detail.back.click()
    assert not window.view.showing_detail()
    assert window.view.list.currentIndex().row() == 2   # the row it came from


# -- the detail -----------------------------------------------------------------------

def opened(window, entry):
    window.view.show_detail(entry.sid)
    window.world.settle()
    return window.view.detail


def test_the_detail_has_one_sentence_and_a_button_only_for_what_the_window_handles(window):
    world = window.world
    prepared, entries = seeded(window, aendern="Ein Jahr", annahme="Steuern")
    world.take(prepared["aendern"])
    world.decide(prepared["aendern"], "request_changes", text="Bitte kürzen.")
    world.decide(prepared["annahme"], "accept")
    window.follow()
    detail = opened(window, entries["aendern"])
    assert detail.sentence.text() == "Anna asks for changes."
    assert detail.more.text() == "What exactly is in the conversation below."
    assert detail.action.isVisibleTo(detail) and detail.action.text() == "Revise in the Editor"
    assert detail.thread.captions == ["Submitted · Version 1", "Anna took it on",
                                      "Anna asked for changes"]
    # Accepted, the writer publishes: the sentence is there, the action is
    # a later phase's, so no button stands in for it.
    detail = opened(window, entries["annahme"])
    assert detail.sentence.text() == "Accepted."
    assert detail._status.action is Action.PUBLISH and not detail.action.isVisibleTo(detail)
    assert Action.PUBLISH not in window.host.actions()


def test_the_composer_sends_with_return_and_a_failure_stays_in_place_with_the_text(window):
    _prepared, entries = seeded(window, reden="Ein Jahr")
    window.follow()
    detail = opened(window, entries["reden"])
    world = window.world
    assert not detail.send.isEnabled()
    detail.composer.setPlainText("Mach ich, danke!")
    QTest.keyClick(detail.composer, Qt.Key.Key_Return)
    world.settle()
    assert detail.composer.toPlainText() == ""
    assert [b.message.bubble.text for b in detail.thread.bubbles] == ["Mach ich, danke!"]
    for _ in range(4):
        world.server.inject("messages", DROP_BEFORE)
    detail.composer.setPlainText("Bis morgen.")
    detail.send.click()
    world.settle()
    while world.later.waiting:
        world.later.run()
        world.settle()
    [footer] = detail.thread.footers
    assert footer.property("words").startswith("Not sent.")
    assert footer.property("action") == "Send Again"
    [out] = window.following.talk.outgoing(entries["reden"].sid)
    assert out.text == "Bis morgen."                    # the text is kept
    detail.thread.retry.emit(out.key)                   # Erneut senden
    world.settle()
    assert [b.message.bubble.text for b in detail.thread.bubbles] == ["Mach ich, danke!",
                                                                     "Bis morgen."]
    assert detail.thread.footers == []


def test_a_message_that_cannot_be_sent_is_said_above_the_composer_and_kept(window):
    _prepared, entries = seeded(window, geheim="Ein Jahr")
    window.follow()
    detail = opened(window, entries["geheim"])
    secret = "nsec1" + "q" * 58
    detail.composer.setPlainText(secret)
    detail.send.click()
    assert detail.problem.isVisibleTo(detail) and detail.problem.text()
    assert detail.composer.toPlainText() == secret
    assert window.following.talk.outgoing(entries["geheim"].sid) == []


def test_a_reaction_shows_on_the_bubble_dimmed_until_it_is_stored(window):
    world = window.world
    prepared, entries = seeded(window, reagieren="Ein Jahr")
    world.message(prepared["reagieren"], "Perfekt.")
    window.follow()
    detail = opened(window, entries["reagieren"])
    [bubble] = detail.thread.bubbles
    assert bubble.react_button.accessibleName() == "React" and bubble.chip is None
    assert bubble.react_button.focusPolicy() == Qt.FocusPolicy.TabFocus
    signer = window.accounts[ME].signer
    signer.held = []
    detail.thread.reacted.emit(bubble.message.bubble.id, EDITOR, conversation.REACTIONS[0])
    [bubble] = detail.thread.bubbles
    assert bubble.chip.text() == conversation.REACTIONS[0] and bubble.dimmed
    assert bubble.chip.accessibleName() == "Reactions: You: Heart"
    signer.release()
    world.settle()
    [bubble] = detail.thread.bubbles
    assert bubble.chip.text() == conversation.REACTIONS[0] and not bubble.dimmed
    detail.thread.reacted.emit(bubble.message.bubble.id, EDITOR, conversation.REACTIONS[0])
    world.settle()
    assert detail.thread.bubbles[0].chip is None        # the same again took it back


def test_withdrawing_asks_first_and_cancel_is_the_default(window, monkeypatch):
    _prepared, entries = seeded(window, zurueck="Ein Jahr")
    window.follow()
    detail = opened(window, entries["zurueck"])
    asked = []

    def ask(parent, *, title, message, buttons, **more):
        asked.append((title, message, [(b.label, b.role) for b in buttons]))
        return False                                # Return: the default, Cancel

    monkeypatch.setattr(alerts, "ask", ask)
    assert detail.withdraw_action.isEnabled()
    detail.withdraw_action.trigger()                    # the alert: answered with Cancel
    window.world.settle()
    title, message, buttons = asked[0]
    assert title == "Withdraw this submission?" and "Einundzwanzig Magazin" in message
    assert buttons == [("Withdraw", alerts.DESTRUCTIVE), ("Cancel", alerts.DEFAULT)]
    assert detail.sentence.text().startswith("In the inbox")
    monkeypatch.setattr(submission_detail, "confirm_destructive", lambda *a, **k: True)
    detail.withdraw_action.trigger()
    window.world.settle()
    assert detail.sentence.text() == "Withdrawn."
    assert not detail.withdraw_action.isEnabled() and not detail.composer_bar.isVisibleTo(detail)


def test_an_unsent_entry_offers_to_send_it_again_or_discard_it(window):
    seed = window.world.account()
    _prepared, entry = window.world.submit(seed, d="nie", title="Nie gesendet", stored=False,
                                           send=False)
    window.follow()
    assert window.rows() == [("header", "Your Turn", ""), ("row", "Nie gesendet", "Not sent")]
    detail = opened(window, entry)
    assert detail.action.text() == "Send Again" and detail.second.text() == "Discard"
    assert detail.action.isVisibleTo(detail) and detail.second.isVisibleTo(detail)
    detail.action.click()
    assert window.said == ["Open the article in the editor first."]
    tab = window.tab("nie")
    detail.action.click()
    assert window.submits == [tab]                      # the sheet reuses the entry
    detail.second.click()
    window.world.settle()
    assert not window.view.showing_detail() and window.view.items() == []


# -- the pill, the notice, the editor --------------------------------------------------

def test_the_pill_says_the_status_of_the_article_in_front(window):
    world = window.world
    prepared, entries = seeded(window, mein="Ein Jahr")
    other = window.tab("anderes")
    mine = window.tab("mein")
    window.follow()
    pill = window.pill
    assert not pill.isHidden() and pill.text() == "Submitted" and pill.sid == entries["mein"].sid
    assert pill.accessibleName() == "Submission to Einundzwanzig Magazin: Submitted"
    window.current = other
    window.host.update_pill()
    assert pill.isHidden()
    world.decide(prepared["mein"], "request_changes", text="Bitte.")
    window.current = mine
    window.following.refresh()
    world.settle()
    assert pill.text() == "Your turn"
    pill.click()                                        # opens the detail
    world.settle()
    assert window.shown == [PANEL_VIEW] and window.view.detail.sid == entries["mein"].sid


def test_a_notice_at_the_article_says_what_is_new_and_opens_the_detail(window):
    world = window.world
    prepared, entries = seeded(window, neu="Ein Jahr")
    tab = window.tab("neu")
    window.follow()
    assert tab.notice.isHidden()                        # one's own submission is no news
    world.decide(prepared["neu"], "request_changes", text="Bitte einen Absatz mehr.")
    window.following.refresh()
    world.settle()
    assert tab.notice.text() == "Einundzwanzig Magazin asks for changes to “Ein Jahr”."
    assert tab.notice.action_labels() == ["View"]
    assert window.host.notice_acted(tab, "submission")
    world.settle()
    assert window.view.detail.sid == entries["neu"].sid
    assert not window.host.notice_acted(tab, "revert")  # another notice's action


def test_open_in_editor_brings_the_tab_forward_or_opens_the_version(window):
    _prepared, entries = seeded(window, offen="Ein Jahr")
    tab = window.tab("offen", "# Ein Jahr\n\nText.")
    window.tab("anderes")
    window.follow()
    detail = opened(window, entries["offen"])
    detail.open_action.trigger()
    assert window.current is tab and window.opened == []
    window.tabs.remove(tab)                             # the article is not open any more
    detail.open_action.trigger()
    window.world.settle()
    assert window.opened == [("Ein Jahr", "# Ein Jahr\n\nText.", "offen", "", ())]


def test_the_action_turns_into_handing_in_once_the_text_in_the_editor_differs(window):
    world = window.world
    prepared, entries = seeded(window, text="Ein Jahr")
    world.decide(prepared["text"], "request_changes", text="Bitte.")
    tab = window.tab("text", "# Ein Jahr\n\nText.")
    window.follow()
    detail = opened(window, entries["text"])
    assert detail.action.text() == "Revise in the Editor"
    tab.text = "# Ein Jahr\n\nText.\n\nUnd ein Absatz mehr."
    detail.refresh()
    assert detail.sentence.text() == "Your changes are in the editor."
    assert detail.action.text() == "Hand In New Version …" and detail.action.isVisibleTo(detail)


# -- lifetimes (lesson 19) ---------------------------------------------------------------

def test_a_view_closed_while_answers_are_pending_raises_nothing(window):
    world = window.world
    prepared, entries = seeded(window, weg="Ein Jahr")
    world.message(prepared["weg"], "Noch da?")
    window.host.follow()                                # the list read is on its way
    window.view.show_detail(entries["weg"].sid)         # nothing known yet: stays on the list
    world.settle()
    window.view.show_detail(entries["weg"].sid)
    window.following.talk.send(entries["weg"].sid, "Antwort")
    window.following.refresh()
    window.view.deleteLater()                           # the panel goes with answers pending
    window.pill.deleteLater()
    QApplication.sendPostedEvents(None, 52)             # QEvent.DeferredDelete
    world.settle()
    assert window.following.rows()                      # the account goes on being followed


def test_letting_the_account_go_empties_the_view_the_pill_and_the_notice(window):
    world = window.world
    prepared, entries = seeded(window, konto="Ein Jahr")
    tab = window.tab("konto")
    window.follow()
    world.message(prepared["konto"], "Hallo")
    window.following.refresh()
    world.settle()
    opened(window, entries["konto"])
    assert not window.pill.isHidden() and not tab.notice.isHidden()
    window.writer.stop()                                # sign-out, or another account
    assert window.view.items() == [] and not window.view.showing_detail()
    assert window.pill.isHidden() and tab.notice.isHidden()
    assert window.view.empty_title.text() == "No account in use"
    world.settle()
    assert window.view.items() == []
