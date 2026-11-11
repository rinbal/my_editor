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
with Cancel as the default; the pill; the notice; Im Editor öffnen;
that what strangers name shows as written; and that a view closed while
an answer is pending raises nothing."""

from __future__ import annotations

from dataclasses import replace

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QToolButton

import alerts
from document_notice import DocumentNotice
from nostr.standup import conversation
from nostr.standup.errors import Code, StandupError
from nostr.standup import following as rules
from nostr.standup.following import Action
from nostr.ui import standup_submit, submission_cover, submission_detail
from nostr.ui.standup_submit import StandupWriter
from nostr.ui.submission_cover import cover_pixmap, png_bytes
from nostr.ui.submission_pill import SubmissionPill
from nostr.ui.submissions_host import Ports, SubmissionsHost
from nostr.ui.drafts_panel import DraftsPanel
from nostr.ui.submissions_view import PANEL_VIEW, SubmissionsView, add_to_panel
from tests.standup_fakes import DROP_BEFORE, SERVER
from tests.standup_following_fakes import FollowWorld
from tests.standup_vectors import pk, sk

ME, EDITOR, OTHER = pk("writer"), pk("editor"), pk("contributor")
NAMES = {EDITOR: "Anna", ME: "Jana", OTHER: "Paul"}


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


class Window:
    """The window's side: one writer, the view, the pill, the host, and
    tabs that are plain values (an article ``d``, its text, its notice)."""

    def __init__(self, world: FollowWorld, monkeypatch, *, server: str = SERVER,
                 uploads=lambda *a: (lambda: None), local_key: bool = True, **host) -> None:
        self.world = world
        monkeypatch.setattr(standup_submit, "_later", world.later)
        self.accounts = {}
        self.profile = SimpleNamespace(user_pubkey=ME, bunker_relays=[], is_local=local_key)
        self.writer = StandupWriter(
            session_pool=None, relay_pool=None, relay_directory=None, media_store=None,
            server=server, uploads=uploads, accounts=self.account,
            clock=world.clock)
        self.view = SubmissionsView(name_of=lambda p: NAMES.get(p, ""),
                                    avatar_of=lambda p: None, is_dark=False, clock=world.clock)
        self.view.resize(380, 640)
        self.pill = SubmissionPill()
        self.tabs, self.current = [], None
        self.kept = {}                              # articles this computer keeps, by d
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
            window=lambda: None, open_article=lambda d: self.tab(d, self.kept[d])
            if d in self.kept else None), view=self.view, pill=self.pill,
            name_of=lambda p: NAMES.get(p, ""), **host)

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

    def tab_of(self, d: str):
        return next(t for t in self.tabs if t.d == d)

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


def test_a_cover_is_decoded_once_and_one_too_large_shows_the_symbol(monkeypatch):
    decoded = []
    real = submission_cover.decode_image_bytes
    monkeypatch.setattr(submission_cover, "decode_image_bytes",
                        lambda *a, **k: decoded.append(a) or real(*a, **k))
    look = ("article", 40, QColor("#eeeeee"), QColor("#333333"))

    def picture(width, height):
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor("#cc3300"))
        return png_bytes(image)

    small = picture(80, 80)
    first = cover_pixmap(small, *look)
    for _paint in range(20):                            # a row painted again and again
        assert cover_pixmap(small, *look) is first
    assert len(decoded) == 1
    # A thumb far larger than any thumb is never decoded: the symbol stands in.
    wide = picture(4000, 40)
    assert cover_pixmap(wide, *look).toImage() == cover_pixmap(None, *look).toImage()
    assert len(decoded) == 1


def test_an_empty_list_says_how_to_submit_and_offers_it(window):
    window.host.follow()
    # Not read yet: not said to be empty.
    assert window.view.empty_title.text() == "Loading …"
    assert not window.view.empty_action.isVisibleTo(window.view)
    window.world.settle()
    assert window.view.empty_title.text() == "No Submissions Yet"
    assert window.view.empty_body.text() == (
        "Follow here what you submit to publications. Open an article and choose "
        "“Submit to a Publication…” in the Nostr menu.")
    window.current = "the article in front"
    window.view.empty_action.click()
    assert window.submits == ["the article in front"]


def test_with_the_submissions_view_the_panel_opens_wide_enough_for_all_three_names(window):
    panel = DraftsPanel(is_dark=False)
    panel.add_view("published", "Published", "", QLabel())
    two = panel.default_width()
    add_to_panel(panel, window.view, server=SERVER)
    width = panel.default_width()
    assert width >= two
    panel.resize(width, 600)
    panel.show()
    QApplication.processEvents()
    assert not panel._top_band._switch.is_popup()       # three segments, side by side
    panel.hide()


def test_an_open_detail_has_no_search_field_and_no_second_menu_above_it(window):
    _prepared, entries = seeded(window, text="Ein Jahr")
    panel = DraftsPanel(is_dark=False)
    add_to_panel(panel, window.view, server=SERVER)
    panel.show_view(PANEL_VIEW)
    panel.show()
    window.follow()
    QApplication.processEvents()
    assert panel._search_edit.isVisible() and panel._actions_btn.isVisible()
    opened(window, entries["text"])
    QApplication.processEvents()
    assert not panel._search_edit.isVisible() and not panel._actions_btn.isVisible()
    window.view.detail.back.click()                     # ‹ Submissions
    QApplication.processEvents()
    assert panel._search_edit.isVisible() and panel._actions_btn.isVisible()
    panel.hide()


def test_without_a_server_nothing_is_read_and_the_panel_has_no_submissions_view(monkeypatch):
    window = Window(FollowWorld(), monkeypatch, server="")
    window.follow()
    assert window.accounts == {} and window.following is None
    panel = DraftsPanel(is_dark=False)
    assert not add_to_panel(panel, window.view, server="") and PANEL_VIEW not in panel.views()
    assert add_to_panel(panel, window.view, server=SERVER) and PANEL_VIEW in panel.views()


def test_starting_myeditor_asks_no_signer_app_until_einreichungen_opens(monkeypatch):
    window = Window(FollowWorld(), monkeypatch, local_key=False)
    world = window.world
    seeded(window, eins="Ein Jahr")
    asked = len(world.relay.queries)
    window.follow()                                     # MyEditor starts
    assert len(world.relay.queries) == asked and window.view.items() == []
    window.view.opened()                                # the person opens Einreichungen
    world.settle()
    assert len(world.relay.queries) > asked
    assert window.rows()[1:] == [("row", "Ein Jahr", "Submitted")]


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
    # Accepted, the writer publishes (M-4 M13): one button for it.
    detail = opened(window, entries["annahme"])
    assert detail.sentence.text() == "Accepted."
    assert detail._status.action is Action.PUBLISH and detail.action.isVisibleTo(detail)
    assert detail.action.text() == "Publish …"


def test_publish_opens_the_accepted_version_and_says_when_the_editor_text_differs(window):
    # M-4 M13, RG-M4-b 1: the accepted version is published; edited since,
    # the sheet says so, and offers no handing in (contract 11.3).
    from nostr.ui.standup_publish_sheet import PublishSheet
    world = window.world
    prepared, entries = seeded(window, annahme="Steuern")
    world.decide(prepared["annahme"], "accept")
    window.follow()
    window.tab(entries["annahme"].d, "Ganz anderer Text.")
    detail = opened(window, entries["annahme"])
    detail.action.click()
    sheet = window.host._publish_sheet
    assert isinstance(sheet, PublishSheet) and sheet.publish.text() == "Publish"
    assert sheet.accepted.text() == "Accepted by Einundzwanzig Magazin, version 1."
    assert sheet.differs.isVisibleTo(sheet) and sheet.differs.text().startswith(
        "The accepted version is published.")
    assert not any("Hand In" in b.text() for b in sheet.findChildren(QPushButton))
    assert window.host.publish is not None and sheet.run is window.writer.publisher(
        window.profile, entries["annahme"].sid)              # one run per submission
    sheet.reject()


def test_a_name_that_comes_in_late_shows_in_the_open_publish_sheet(window, monkeypatch):
    # A profile read after the sheet opened: the sheet says the name then.
    from nostr.standup import publishing
    late = pk("recipient")
    monkeypatch.setattr(publishing, "credited", lambda revision, writer: [writer, late])
    prepared, entries = seeded(window, annahme="Steuern")
    window.world.decide(prepared["annahme"], "accept")
    window.follow()
    opened(window, entries["annahme"]).action.click()
    sheet = window.host._publish_sheet
    assert sheet.people.text() == "Jana, Someone"
    monkeypatch.setitem(NAMES, late, "Lena")
    window.host.names_learned()
    assert sheet.people.text() == "Jana, Lena"
    sheet.reject()


def test_a_contributor_confirms_the_split_and_their_rights_from_their_own_app():
    # M-4 M14, the contributor's side: their own app, the split the verified
    # accept names, the rights ticked before Confirm; the writer's app then
    # holds Paul's confirmation and reminds nobody.
    from nostr.ui.standup_confirm_sheet import ConfirmSheet
    world = FollowWorld()
    writer = world.account()
    prepared, entry = world.submit(writer, d="gemeinsam", title="Gemeinsam",
                                   participants=(OTHER,))
    sid = entry.sid
    world.record(sid).signer = "publication"
    world.decide(prepared, "accept", split=[{"pubkey": ME, "weight": 70, "role": "writer"},
                                            {"pubkey": OTHER, "weight": 30,
                                             "role": "contributor"}])
    paul = world.account("contributor")
    following = world.following(paul)
    following.view_opened()
    world.settle()
    following.open(sid)
    world.settle()
    reading = following.reading
    accepted, version = reading.accepted(sid), reading.latest(sid)
    sheet = ConfirmSheet(
        role=following.row(sid).role, publication="Magazin", number=1,
        shares=reading.accepted_split(sid), me=OTHER, name_of=lambda p: NAMES.get(p, ""),
        zap_address=lambda pubkey, answer: answer("paul@getalby.com"),
        confirm=lambda confirms, done: following.confirm(
            sid, confirms=confirms, revision_hash=version.revision_hash,
            split_rumor_id=accepted.split_rumor_id, on_done=done), is_dark=False)
    assert sheet.split.text() == "Jana 70 %  ·  You 30 %"
    assert sheet.rights.isVisibleTo(sheet) and not sheet.confirm_button.isEnabled()
    sheet.rights.setChecked(True)
    sheet.confirm_button.click()
    world.settle()
    assert following.row(sid).view.confirmations.get(OTHER) == ("split", "rights")
    mine = world.following(writer)
    mine.open(sid)
    world.settle()
    assert mine.row(sid).view.confirmations.get(OTHER) == ("split", "rights")
    assert OTHER not in rules.waiting_for(mine.row(sid))


def test_confirm_is_there_only_after_a_read_that_names_a_lightning_address():
    # RG-M4-a 3, RG-M4-b 2: a share reaches a person only at the address
    # their profile names. No Bestätigen while it is read, where it names
    # none (Profil bearbeiten instead) or could not be read (Erneut
    # versuchen reads it again); once a read names one, Bestätigen is the
    # primary action.
    from nostr.ui.standup_confirm_sheet import NO_ADDRESS, PROFILE_NOT_READ, ConfirmSheet
    answers, sent = [], []
    sheet = ConfirmSheet(
        role="writer", publication="Magazin", number=2,
        shares=((ME, 70, "writer"), (OTHER, 30, "contributor")), me=ME,
        name_of=lambda p: NAMES.get(p, ""),
        zap_address=lambda pubkey, answer: answers.append(answer),
        confirm=lambda confirms, done: sent.append(confirms), is_dark=False)
    assert not sheet.confirm_button.isVisibleTo(sheet)
    answers[-1](None)
    assert sheet.address.text() == PROFILE_NOT_READ and sheet.read_again.isDefault()
    assert not sheet.confirm_button.isVisibleTo(sheet)
    sheet.read_again.click()
    assert len(answers) == 2
    answers[-1]("")
    assert sheet.address.text() == NO_ADDRESS and sheet.edit_profile.isDefault()
    assert not sheet.confirm_button.isVisibleTo(sheet) and sent == []
    answers[-1]("jana@getalby.com")
    assert sheet.confirm_button.isVisibleTo(sheet) and sheet.confirm_button.isDefault()
    assert not sheet.edit_profile.isVisibleTo(sheet)
    sheet.confirm_button.click()
    assert sent == [("split",)]
    sheet.close()


def test_a_correction_is_compared_in_its_sheet_and_a_question_goes_to_the_composer(window):
    # M-4 M15 at the host: the writer's published article, the editorial
    # team's correction; Compare opens the sheet with the note and the
    # comparison, Take the Correction is the one primary action.
    from nostr import events
    from nostr.bech32 import encode_naddr
    from nostr.ui.standup_correction_sheet import CorrectionSheet
    world = window.world
    seed = world.account()
    prepared, entry = world.submit(seed, d="ein-jahr", title="Ein Jahr mit Lightning im Café")
    world.decide(prepared, "accept")
    article = events.sign_event({"kind": 30023, "created_at": world.clock(), "content": "Alt.",
                                 "tags": [["d", "ein-jahr"]]}, sk("writer"))
    reported = []
    seed.api.published(entry.sid, naddr=encode_naddr("ein-jahr", ME, 30023), event=article,
                       revision=1, on_success=reported.append, on_failure=reported.append)
    world.settle()
    world.correct(seed, entry.sid, "Korrigiert.")
    window.follow()
    detail = opened(window, entry)
    assert detail.action.text() == "Compare …"
    detail.action.click()
    world.settle()
    sheet = window.host._correction_sheet
    assert isinstance(sheet, CorrectionSheet) and sheet.isVisible()
    assert sheet.note.text() == "“Zwei Zahlen korrigiert.”"
    assert sheet.take.text() == "Take the Correction" and sheet.take.isDefault()
    sheet.ask.click()
    assert window.view.detail.composer.toPlainText() == "A short question about the correction: "


def test_a_second_correction_is_compared_with_the_article_as_it_stands_in_public(window):
    # RG-M4-a 6: two corrections wait; the second is compared with the
    # published article, never with the first correction, so the writer
    # sees both changes before taking it.
    from nostr import events
    from nostr.bech32 import encode_naddr
    world = window.world
    seed = world.account()
    prepared, entry = world.submit(seed, d="ein-jahr", title="Ein Jahr mit Lightning im Café")
    world.decide(prepared, "accept")
    article = events.sign_event({"kind": 30023, "created_at": world.clock(), "content": "Alt.",
                                 "tags": [["d", "ein-jahr"]]}, sk("writer"))
    seed.api.published(entry.sid, naddr=encode_naddr("ein-jahr", ME, 30023), event=article,
                       revision=1, on_success=lambda _v: None, on_failure=lambda _e: None)
    world.settle()
    world.correct(seed, entry.sid, "Erste Korrektur.")
    world.correct(seed, entry.sid, "Zweite Korrektur.", revision=3)
    window.follow()
    compared = []
    window.host.comparer.compare = lambda old, new, done: compared.append((old, new)) or (
        lambda: None)
    detail = opened(window, entry)
    detail.action.click()
    world.settle()
    assert compared == [("Alt.", "Zweite Korrektur.")]


def test_view_opens_the_address_the_published_event_proves_never_the_servers(window, monkeypatch):
    # Contract 10, The address of a published work: Ansehen opens only the
    # address a counting report proves; until one is read, it says so once.
    from nostr.standup.publishing import Published
    from nostr.ui import submissions_host
    links = []
    monkeypatch.setattr(submissions_host.QDesktopServices, "openUrl",
                        lambda url: links.append(url.toString()))
    prepared, entries = seeded(window, annahme="Steuern")
    window.follow()
    sid = entries["annahme"].sid
    window.host.view_published(sid)
    assert links == [] and window.said == [
        "The published article couldn’t be found yet: try again in a moment."]
    monkeypatch.setattr(window.host._following.reading, "published",
                        lambda _sid: Published(f"30023:{ME}:steuern-d", {}, 1))
    window.host.view_published(sid)
    assert len(links) == 1 and links[0].endswith(f"/{ME}/steuern-d")   # never the row's d


def test_confirming_names_the_accepted_split_and_where_the_share_goes_then_reminds(
        window, monkeypatch):
    # M-4 M14: a work the publication signs; the writer confirms the split
    # the verified accept names, sees where their share goes, then waits
    # for Paul and can remind him.
    from nostr.ui.standup_confirm_sheet import ConfirmSheet
    world = window.world
    seed = world.account()
    prepared, entry = world.submit(seed, d="steuern", title="Steuern", participants=(OTHER,))
    world.record(entry.sid).signer = "publication"          # as the publication's rules allow
    world.decide(prepared, "accept", split=[{"pubkey": ME, "weight": 70, "role": "writer"},
                                            {"pubkey": OTHER, "weight": 30,
                                             "role": "contributor"}])
    window.host._ports = replace(window.host._ports,
                                 zap_address=lambda pubkey, answer: answer("jana@getalby.com"))
    window.follow()
    detail = opened(window, entry)
    assert detail.action.text() == "Confirm …"
    # Item 7: the proposal is in the conversation, marked as the one accepted.
    assert "Anna proposes a split: You 70 %, Paul 30 %. Accepted with this split." in \
        detail.thread.captions
    detail.action.click()
    sheet = window.host._confirm_sheet
    assert isinstance(sheet, ConfirmSheet) and sheet.split.text() == "You 70 %  ·  Paul 30 %"
    assert sheet.address.text() == "Your share goes to jana@getalby.com."
    assert not sheet.rights.isVisibleTo(sheet)               # the writer confirms the split
    sheet.confirm_button.click()
    world.settle()
    record = world.record(entry.sid)
    assert record.confirmations == {ME: ["split"]}
    detail = opened(window, entry)
    assert detail.sentence.text() == "Thanks. Paul still confirms."
    assert detail.action.text() == "Remind Paul"
    reminded = []
    monkeypatch.setattr(type(window.following), "remind",
                        lambda self, sid, to, done: (reminded.append(to), done(None)))
    detail.action.click()
    assert reminded == [OTHER] and detail.note.text() == "Reminder sent."


def test_a_conversation_is_opened_once_and_a_refresh_draws_only_what_is_new(window,
                                                                          monkeypatch):
    world = window.world
    prepared, entries = seeded(window, lang="Ein Jahr", andere="Steuern")
    for number in range(5):
        world.message(prepared["lang"], f"Nachricht {number}")
    opened, real = [], conversation.open_item
    monkeypatch.setattr(conversation, "open_item",
                        lambda *a, **k: opened.append(a[2]) or real(*a, **k))
    window.follow()
    window.view.show_detail(entries["lang"].sid)
    world.settle()
    detail = window.view.detail
    assert opened.count(entries["lang"].sid) == 5      # each message once, list and detail
    drawn = detail.thread.bubbles
    world.message(prepared["lang"], "Noch eine")
    window.following.refresh()
    world.settle()
    assert opened.count(entries["lang"].sid) == 6      # only the new one
    # Nothing before it is drawn again, but the one whose picture moved to
    # the new last of the group.
    assert detail.thread.bubbles[:4] == drawn[:4] and detail.thread.bubbles[4] is not drawn[4]
    redrawn = []
    monkeypatch.setattr(detail.thread, "show_thread", lambda *a: redrawn.append(a))
    world.message(prepared["andere"], "Eine andere Einreichung")
    window.following.refresh()
    world.settle()
    assert redrawn == []                                # another submission's news


def test_a_long_conversation_opens_at_its_end_under_a_pinned_sentence_and_stays_there(window):
    world = window.world
    prepared, entries = seeded(window, lang="Ein Jahr")
    world.decide(prepared["lang"], "request_changes", text="Bitte.")
    for number in range(8):
        world.message(prepared["lang"], f"Nachricht {number}: " + "ein ganzer Satz. " * 12)
    window.tab("lang", "# Ein Jahr\n\nText.")
    window.follow()
    window.view.show()
    detail = opened(window, entries["lang"])
    QApplication.processEvents()
    bar = detail._scroll.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == bar.maximum()      # the newest message
    # The sentence and the action are not in what scrolls.
    assert not detail._scroll.widget().isAncestorOf(detail.action)
    assert detail.action.isVisible() and detail.action.text() == "Revise in the Editor"
    # Sent: still at the end, the new bubble in sight.
    detail.composer.setPlainText("Mach ich.")
    QTest.keyClick(detail.composer, Qt.Key.Key_Return)
    world.settle()
    QApplication.processEvents()
    assert bar.value() == bar.maximum()
    # Scrolled up to read: news does not pull it down.
    bar.setValue(0)
    world.message(prepared["lang"], "Noch eine")
    window.following.refresh()
    world.settle()
    QApplication.processEvents()
    assert bar.value() == 0
    window.view.hide()


def test_tab_reaches_the_conversation_before_the_composer_and_each_reaction_is_named(window):
    world = window.world
    prepared, entries = seeded(window, text="Ein Jahr")
    world.decide(prepared["text"], "request_changes", text="Bitte einen Absatz mehr.")
    window.tab("text", "# Ein Jahr\n\nText.")
    window.follow()
    detail = opened(window, entries["text"])
    [bubble] = detail.thread.bubbles
    react = bubble.react_button
    assert react.accessibleName() == "React to the message from Anna"
    assert (react.width(), react.height()) == (28, 28)
    chain, widget = [], detail.action
    while widget is not detail.composer and len(chain) < 50:
        widget = widget.nextInFocusChain()
        chain.append(widget)
    assert react in chain                               # before the composer
    # Drawn again with news: the order holds.
    world.message(prepared["text"], "Und noch etwas.")
    window.following.refresh()
    world.settle()
    chain, widget = [], detail.action
    while widget is not detail.composer and len(chain) < 50:
        widget = widget.nextInFocusChain()
        chain.append(widget)
    assert all(b.react_button in chain for b in detail.thread.bubbles)


def test_more_about_sits_on_the_sentence_and_a_narrow_unread_row_shows_the_message(window):
    world = window.world
    prepared, entries = seeded(window, text="Ein Jahr")
    world.decide(prepared["text"], "request_changes", text="Bitte einen Absatz mehr.")
    world.server.publication().members.pop(EDITOR)      # the decision does not verify
    window.follow()
    [row] = [i for i in window.view.items() if i.kind == "row"]
    # The name goes first when the row is narrow: the message is what is new.
    assert row.unread and row.rest.endswith(": Bitte einen Absatz mehr.")
    assert row.short == "Bitte einen Absatz mehr."
    window.view.show()
    detail = opened(window, entries["text"])
    QApplication.processEvents()
    [more] = [w for w in detail.status.findChildren(QToolButton)
              if w.text() == "More about this"]
    sentence = detail.sentence.geometry()
    assert sentence.top() <= more.geometry().center().y() <= sentence.bottom()
    window.view.hide()


def test_the_composer_is_one_line_that_grows_and_says_how_to_start_a_new_line(window):
    _prepared, entries = seeded(window, text="Ein Jahr")
    window.follow()
    window.view.show()
    detail = opened(window, entries["text"])
    composer = detail.composer
    one = composer.height()
    composer.setPlainText("Hallo")
    assert composer.height() == one                     # one line
    assert detail.send.toolTip() == "Send (↩)" and detail.hint.isHidden()
    composer.setFocus()
    QApplication.processEvents()
    assert detail.hint.isVisible() and detail.hint.text() == "⇧↩ for a new line"
    composer.setPlainText("Eins\nZwei\nDrei")
    assert composer.height() > one
    composer.setPlainText("\n".join(["Zeile"] * 5))
    five = composer.height()
    composer.setPlainText("\n".join(["Zeile"] * 20))
    assert composer.height() == five                    # then it scrolls
    window.view.hide()


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
    assert bubble.react_button.accessibleName().startswith("React to") and bubble.chip is None
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


def test_a_withdrawal_names_its_real_cause_or_shows_what_was_decided_meanwhile(
        window, monkeypatch):
    world = window.world
    prepared, entries = seeded(window, text="Ein Jahr")
    window.follow()
    detail = opened(window, entries["text"])
    monkeypatch.setattr(submission_detail, "confirm_destructive", lambda *a, **k: True)
    asked = []
    monkeypatch.setattr(submission_detail, "ask_try_again",
                        lambda *a, **k: asked.append(k["message"]) or False)
    # The signer app said no: that is the cause, with Try Again.
    monkeypatch.setattr(window.following.api_for(entries["text"].sid), "withdraw",
                        lambda sid, ok, failed: failed(StandupError(Code.SIGNER_DECLINED)))
    detail.withdraw_action.trigger()
    assert asked == ["Your signer app declined the request: allow it there and try again."]
    monkeypatch.undo()
    monkeypatch.setattr(submission_detail, "confirm_destructive", lambda *a, **k: True)
    monkeypatch.setattr(submission_detail, "ask_try_again", lambda *a, **k: asked.append(k))
    # Decided meanwhile: read again and shown, nothing to try again.
    world.decide(prepared["text"], "decline")
    detail.withdraw_action.trigger()
    world.settle()
    assert len(asked) == 1 and detail.sentence.text() == "Declined."
    assert detail.note.text() == ("It wasn’t withdrawn: Einundzwanzig Magazin decided on it in "
                                  "the meantime.")


def test_an_unsent_entry_offers_to_send_it_again_or_discard_it(window):
    seed = window.world.account()
    _prepared, entry = window.world.submit(seed, d="nie", title="Nie gesendet", stored=False,
                                           send=False)
    window.follow()
    assert window.rows() == [("header", "Your Turn", ""), ("row", "Nie gesendet", "Not sent")]
    detail = opened(window, entry)
    assert detail.action.text() == "Send Again" and detail.second.text() == "Discard"
    assert detail.action.isVisibleTo(detail) and detail.second.isVisibleTo(detail)
    assert detail.more.text() == ("This submission didn’t reach Einundzwanzig Magazin. Send it "
                                  "again or discard the attempt. Your article stays.")
    # Nothing to write into, nobody reading, no version that would refuse.
    assert not detail.composer_bar.isVisibleTo(detail) and not detail.readers.isVisibleTo(detail)
    assert not detail.encrypted.isVisibleTo(detail) and not detail.open_action.isVisible()
    # Not found: said in place, and it stays through a refresh.
    detail.action.click()
    window.following.refresh()
    window.world.settle()
    assert detail.note.isVisibleTo(detail) and window.said == []
    assert "can’t find this article" in detail.note.text()
    # Found by its link name: it opens, and the sheet reuses the entry.
    window.kept["nie"] = "Der Text"
    detail.action.click()
    assert window.submits == [window.tab_of("nie")] and not detail.note.isVisibleTo(detail)
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
    # Finished: said until it is read, then the pill goes.
    window.view.show_list()
    world.decide(prepared["mein"], "decline")
    window.following.refresh()
    world.settle()
    assert not pill.isHidden() and pill.text() == "Declined"
    pill.click()
    world.settle()
    assert pill.isHidden()


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


def test_a_name_someone_else_chose_shows_as_written_never_as_markup(window):
    world = window.world
    prepared, _entries = seeded(window, name="Ein Jahr")
    tab = window.tab("name")
    window.follow()
    window.following.publication_name = lambda _address: "<h1>Bank</h1>"
    world.decide(prepared["name"], "request_changes", text="Bitte.")
    window.following.refresh()
    world.settle()
    label = tab.notice.findChild(QLabel, "DocumentNoticeText")
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == "<h1>Bank</h1> asks for changes to “Ein Jahr”."
    assert "&lt;h1&gt;Bank" in window.pill.toolTip()       # a tooltip shown as written


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
    # Typed in: the detail follows once the typing pauses (RG-M3-b 1).
    tab.text = "# Ein Jahr\n\nText.\n\nUnd ein Absatz mehr."
    window.host.article_changed(typing=True)
    assert detail.action.text() == "Revise in the Editor"      # still typing
    window.host._typing.timeout.emit()                          # the pause
    assert detail.sentence.text() == "Your changes are in the editor."
    assert detail.action.text() == "Hand In New Version …" and detail.action.isVisibleTo(detail)
    # Another tab in front, then this one again: at once.
    window.tab("anders")
    window.host.article_changed()
    assert window.pill.isHidden()
    window.current = tab
    window.host.article_changed()
    assert not window.pill.isHidden()


def test_a_text_changed_during_review_is_said_with_a_plain_button(window):
    prepared, entries = seeded(window, text="Ein Jahr")
    tab = window.tab("text", "# Ein Jahr\n\nText.\n\nNoch ein Absatz.")
    window.follow()
    detail = opened(window, entries["text"])
    assert detail.more.text() == "You’ve changed the text since."
    assert detail.action.text() == "Hand In New Version …"
    assert detail.action.objectName() == "submission_offered"     # not the accent
    tab.text = "# Ein Jahr\n\nText."
    window.host.article_changed()
    assert detail.more.text() == "You don’t need to do anything right now."
    assert detail.action.objectName() == "submission_action"


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
