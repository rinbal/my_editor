# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Submit to a Publication, the sheet (nostr/ui/standup_submit_sheet.py,
plan 10.2 and the prototype MyEditor_Einreichen_Prototyp).

Pinned here, in the prototype's German: without the server's address
nothing can be submitted and the sheet says it comes from the
association; the picker offers the publications open for articles, the
ones submitted to before first, the others dimmed with the reason; the
rules of the chosen one under it; "Veröffentlicht von" only where the
writer chooses, the rights only where they are asked for; Einreichen is
the default button and dimmed by a blocker whose reason is said; the
five privacy lines behind the lock; Einreichen turns the sheet into the
handoff, and a sheet closed meanwhile lets the submission go on in the
background."""

from __future__ import annotations

from typing import List

import pytest
import shiboken6
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

import i18n
from nostr.avatar_store import AvatarStore
from nostr.profiles import Profile, ProfileStore
from nostr.standup.drafting import ArticleDraft
from nostr.standup.submitting import Progress, Step
from nostr.standup.trust import Destination, RedaktionKey
from nostr.ui.standup_submit import RoundAnswer
from nostr.ui.standup_submit_sheet import SubmitSheet, recent_first, rules_words
from nostr import crypto as nostr_crypto
from tests.standup_vectors import pk

WRITER = pk("writer")


def _key(who: str) -> str:
    """A valid public key of its own for each publication of the tests."""
    seed = (sum(map(ord, who)) % 250) + 1
    return nostr_crypto.get_public_key(bytes([seed]) * 32).hex()


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def german():
    i18n.install("de")
    yield
    i18n.install("en")


def _rules(**more) -> dict:
    rules = {"open": True, "kinds": ["article"], "rights_required": False,
             "publish_by": {"article": "writer"}, "max_open_per_writer": 3,
             "checklist": [{"id": "t", "label": "Titel", "level": "required"},
                           {"id": "s", "label": "Zusammenfassung", "level": "required"},
                           {"id": "c", "label": "Titelbild", "level": "required"},
                           {"id": "q", "label": "Quellen", "level": "recommended"}]}
    rules.update(more)
    return rules


def _destination(who: str, name: str, *, key=True, **rules) -> Destination:
    address = f"30004:{_key(who)}:site"
    redaktion = RedaktionKey(
        address=address, publication_pubkey=_key(who), site_d="site", epoch=1,
        pubkey=_key(who + "-epoch"), server="https://standup.example", owner=_key(who),
        name=name, rules=_rules(**rules), event_id="e" * 64, created_at=1, event={})
    return Destination(address=address, name=name, picture=None,
                       key=redaktion if key else None)


MAGAZIN = _destination("magazin", "Einundzwanzig Magazin")
PODCAST = _destination("podcast", "Der Podcast", kinds=["audio"])
BLOG = _destination("blog", "Ein Blog", publish_by={"article": "both"}, rights_required=True)


class _Stops(QObject):
    stopped = Signal(str)


class Writer:
    """StandupWriter as the sheet sees it, answering at once."""

    def __init__(self, *, server="https://standup.example", destinations=(MAGAZIN,),
                 used=(), round_=(1, ""), operator="Einundzwanzig e.V.") -> None:
        self._stops = _Stops()
        self.stopped = self._stops.stopped
        self.server = server
        self._destinations, self._used = list(destinations), list(used)
        self.round, self.operator = round_, operator
        self.submitted: List[tuple] = []
        self.background: List[object] = []
        self.on_its_way = None
        self.asked = 0
        self.discarded: List[object] = []

    def instance(self, _profile, on_operator) -> None:
        if self.operator:
            on_operator(self.operator)

    def destinations(self, _profile, on_done, _on_failure) -> None:
        on_done(self._destinations, self._used)

    def running(self, _profile, _d, _publication=""):
        return self.on_its_way

    def round_for(self, _profile, _publication, _d, on_done) -> None:
        self.asked += 1
        on_done(RoundAnswer(*self.round))

    def discard(self, _profile, entry, on_done) -> None:
        self.discarded.append(entry)
        self.round = (1, "")
        on_done(True)

    def submit(self, profile, draft, choice, answer):
        self.submitted.append((profile, draft, choice, answer.round))
        self.on_its_way = self.on_its_way or Run(choice, draft)
        return self.on_its_way

    def in_background(self, run) -> None:
        self.background.append(run)


class Run:
    def __init__(self, choice=None, draft=None) -> None:
        self.choice, self.draft = choice, draft
        from PySide6.QtCore import QObject, Signal

        class Changes(QObject):
            changed = Signal(object)

        self._changes = Changes()
        self.changed = self._changes.changed
        self.started = 0
        self.cancelled = 0
        self.progress = Progress(Step.SIGNING)

    def start(self) -> None:
        self.started += 1
        self.changed.emit(self.progress)

    def cancel(self) -> None:
        self.cancelled += 1
        self.progress = Progress(Step.CANCELLED)


DRAFT = ArticleDraft(d="ein-jahr", title="Ein Jahr mit Lightning im Café",
                     summary="Wie ein Café in Leipzig ein Jahr lang mit Bitcoin bezahlt wurde.",
                     content="Ein Text.")


def _sheet(tmp_path, writer=None, *, draft=DRAFT, dark=False, private=()) -> SubmitSheet:
    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = Profile(user_pubkey=WRITER, bunker_pubkey="b" * 64, display_name="Jana Roth",
                      bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64)
    store.upsert(profile)
    return SubmitSheet(writer=writer or Writer(), draft=draft, cover=QPixmap(),
                       byline="Jana Roth · Artikel · 1 Bild", private_names=private,
                       profile=profile, store=store, avatars=AvatarStore(),
                       request_logo=lambda *_a: None, is_dark=dark)


def _entries(sheet) -> List[tuple]:
    model = sheet.picker.model()
    return [(sheet.picker.itemText(i), model.item(i).isEnabled())
            for i in range(sheet.picker.count()) if sheet.picker.itemText(i)]


def test_without_the_servers_address_nothing_is_sent_and_it_comes_from_the_association(
        tmp_path):
    sheet = _sheet(tmp_path, Writer(server=""))
    assert sheet.status.text() == "Die Adresse des Servers kommt vom Verein."
    assert not sheet.submit_button.isEnabled() and not sheet.picker.isEnabled()


def test_the_picker_offers_the_publications_for_articles_the_used_ones_first(tmp_path):
    writer = Writer(destinations=(PODCAST, MAGAZIN, BLOG), used=(BLOG.address,))
    sheet = _sheet(tmp_path, writer)
    assert _entries(sheet) == [("Ein Blog", True), ("Der Podcast · Nimmt nur Audio", False),
                               ("Einundzwanzig Magazin", True),
                               ("Zuletzt verwendete zuerst", False)]
    assert sheet.destination() is BLOG


def test_the_rules_of_the_chosen_one_are_said_under_it(tmp_path):
    sheet = _sheet(tmp_path)
    assert sheet.rules.text() == ("Verlangt Titel, Zusammenfassung und Titelbild · "
                                  "höchstens 3 offene Einreichungen")
    assert rules_words({"open": False}) == "Nimmt gerade keine Einreichungen"


def test_published_by_and_the_rights_show_only_where_they_are_asked_for(tmp_path):
    sheet = _sheet(tmp_path, Writer(destinations=(MAGAZIN, BLOG)))
    sheet.show()
    assert not sheet.by_me.isVisible() and not sheet.rights.isVisible()
    assert sheet.choice().signer == "writer"
    sheet.picker.setCurrentIndex(1)
    assert sheet.by_me.isVisible() and sheet.rights.isVisible()
    assert sheet.by_hint.text() == "Nach der Annahme veröffentlichst du mit deinem Schlüssel."
    sheet.by_publication.setChecked(True)
    assert sheet.by_hint.text() == ("Die Redaktion schlägt bei der Annahme die Aufteilung der "
                                    "Zaps vor, du bestätigst sie.")
    assert sheet.choice().signer == "publication"


def test_einreichen_is_the_default_dimmed_by_a_blocker_whose_reason_is_said(tmp_path):
    sheet = _sheet(tmp_path, Writer(destinations=(BLOG,)))
    assert sheet.submit_button.isDefault()
    assert not sheet.submit_button.isEnabled()
    assert sheet.status.text() == "Bestätige die Rechte, um einzureichen."
    sheet.rights.setChecked(True)
    assert sheet.submit_button.isEnabled() and sheet.status.isHidden()


@pytest.mark.parametrize("writer, said", [
    (Writer(destinations=(_destination("x", "Ohne Schlüssel", key=False),)),
     "Der Schlüssel der Redaktion ist nicht bestätigt. Einreichen ist gerade nicht möglich."),
    (Writer(round_=(None, "")),
     "Du hast diesen Text schon bei Einundzwanzig Magazin eingereicht."),
    (Writer(destinations=()), "Gerade nimmt keine Publikation Einreichungen an."),
    (Writer(destinations=(PODCAST,)), "Gerade nimmt keine Publikation Einreichungen an."),
])
def test_what_cannot_be_submitted_says_why(tmp_path, writer, said):
    sheet = _sheet(tmp_path, writer)
    assert not sheet.submit_button.isEnabled()
    assert sheet.status.text() == said


def test_a_private_picture_blocks(tmp_path):
    sheet = _sheet(tmp_path, private=("cafe.jpg",))
    assert not sheet.submit_button.isEnabled()
    assert "cafe.jpg" in sheet.status.text()


def test_the_five_privacy_lines_name_the_publication_and_the_operator(tmp_path):
    from nostr.standup.drafting import LocalPicture
    draft = ArticleDraft(d="a", title="T", summary="", content="x", pictures=(
        LocalPicture(key="k", name="a.jpg", mime="image/jpeg", data=b"1"),))
    sheet = _sheet(tmp_path, draft=draft)
    assert sheet.privacy_words() == [
        "Lesen können nur du und die Redaktion von Einundzwanzig Magazin.",
        "Der Server sieht Titel, Publikation und Beteiligte, nie den Inhalt.",
        "Das Bild liegt verschlüsselt auf deinem Medienserver. Er sieht, wann es abgerufen "
        "wird, nie was darin ist.",
        "Zurückziehen löscht nichts, was die Redaktion schon hat.",
        "Der Server von Einundzwanzig e.V. bewahrt die Einreichung verschlüsselt auf."]
    assert sheet.privacy_lines.isHidden()
    sheet.privacy_toggle.setChecked(True)
    assert not sheet.privacy_lines.isHidden()


def test_einreichen_hands_over_and_a_closed_sheet_lets_it_go_on(tmp_path):
    writer = Writer()
    sheet = _sheet(tmp_path, writer)
    sheet.message_toggle.setChecked(True)
    sheet.message.setPlainText("Passt zu eurer Reihe.")
    kept = []
    sheet.submitted.connect(kept.append)             # RG-M2b 8: the tab keeps its d
    sheet.submit_button.click()
    assert kept == ["ein-jahr"]
    (_profile, draft, choice, round_), = writer.submitted
    assert draft is DRAFT and round_ == 1
    assert (choice.publication, choice.signer, choice.message) == (
        MAGAZIN.address, "writer", "Passt zu eurer Reihe.")
    assert sheet.run.started == 1 and sheet._pages.currentIndex() == 1
    assert sheet.handoff.headline.text() == "Warte auf deine Signer-App."
    sheet.handoff.close_requested.emit()
    assert writer.background == [sheet.run]


def test_a_sheet_whose_account_was_let_go_closes_and_sends_nothing_to_the_background(
        tmp_path):
    # RG-M2b 9: signed out, the sheet stayed at "Sending" with no way to
    # close it, and its run answered nobody.
    writer = Writer()
    sheet = _sheet(tmp_path, writer)
    sheet.show()
    sheet.submit_button.click()
    writer.stopped.emit(WRITER)
    assert not sheet.isVisible() and sheet.run is None and writer.background == []
    form_writer = Writer()
    form = _sheet(tmp_path, form_writer)                 # still choosing: closes as well
    form.show()
    form_writer.stopped.emit(WRITER)
    assert not form.isVisible()


def test_another_account_chosen_in_the_sheet_lets_the_first_go_and_the_sheet_stays(tmp_path):
    writer = Writer()
    sheet = _sheet(tmp_path, writer)
    sheet.show()
    writer.stopped.emit("c" * 64)                        # not the one it submits as
    assert sheet.isVisible()


def _handed_over(tmp_path, progress):
    writer = Writer()
    sheet = _sheet(tmp_path, writer)
    sheet.show()
    sheet.submit_button.click()
    sheet.run.progress = progress
    sheet.handoff.show_progress(progress)
    return writer, sheet


def test_escape_cancels_what_can_still_be_cancelled(tmp_path):
    # RG-M2b 10, HIG: Escape is Cancel; it sent the sheet to the background.
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    writer, sheet = _handed_over(tmp_path, Progress(Step.UPLOADING))
    run = sheet.run
    QTest.keyClick(sheet, Qt.Key.Key_Escape)
    assert run.cancelled == 1 and not sheet.isVisible()
    writer, sheet = _handed_over(tmp_path, Progress(Step.UPLOADING))
    run = sheet.run
    sheet.handoff.cancel_button.click()                  # the button does the same
    assert run.cancelled == 1 and not sheet.isVisible()


def test_escape_once_it_is_on_its_way_lets_it_go_on_as_continue_in_background_does(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    writer, sheet = _handed_over(tmp_path, Progress(Step.SENDING, sent=True))
    run = sheet.run
    QTest.keyClick(sheet, Qt.Key.Key_Escape)
    assert run.cancelled == 0 and writer.background == [run] and not sheet.isVisible()
    writer, sheet = _handed_over(tmp_path, Progress(Step.UPLOADING))
    run = sheet.run
    assert sheet.handoff.background_button.isVisible()
    assert sheet.handoff.background_button.text() == "Im Hintergrund fortsetzen"
    sheet.handoff.background_button.click()
    assert run.cancelled == 0 and writer.background == [run] and not sheet.isVisible()


def test_without_einreichen_nothing_goes_on(tmp_path):
    writer = Writer()
    sheet = _sheet(tmp_path, writer)
    sheet.cancel_button.click()
    assert writer.submitted == [] and writer.background == []


def test_the_order_of_the_publications_puts_the_newest_used_first():
    assert recent_first([MAGAZIN, PODCAST, BLOG], [BLOG.address, MAGAZIN.address]) == [
        BLOG, MAGAZIN, PODCAST]


@pytest.mark.parametrize("dark", [False, True])
def test_the_sheet_paints_in_both_themes(tmp_path, dark):
    sheet = _sheet(tmp_path, dark=dark)
    sheet.resize(780, 520)
    assert not sheet.grab().isNull()
    sheet.apply_theme(not dark)
    assert not sheet.grab().isNull()


def test_opened_again_on_a_work_on_its_way_the_sheet_shows_that_run(tmp_path):
    # One run per work (RG-M2b 1): never a second with the same sid.
    writer = Writer()
    first = _sheet(tmp_path, writer)
    first.submit_button.click()
    first.handoff.close_requested.emit()            # closed while it goes on
    again = _sheet(tmp_path, writer)
    assert again.run is writer.on_its_way and again._pages.currentIndex() == 1
    assert again.handoff.headline.text() == "Warte auf deine Signer-App."
    assert len(writer.submitted) == 1 and again.run.started == 1


def test_an_earlier_try_never_sent_is_sent_again_or_discarded(tmp_path):
    # Contract 13: an entry the server has no receipt for is shown as not
    # sent, with Erneut (Submit reuses it) and Verwerfen.
    unsent = object()
    writer = Writer(round_=(1, "", unsent))
    sheet = _sheet(tmp_path, writer)
    assert sheet.unsent.isVisibleTo(sheet) and sheet.submit_button.isEnabled()
    asked = writer.asked
    sheet.discard_button.click()
    assert writer.discarded == [unsent] and writer.asked == asked + 1
    assert not sheet.unsent.isVisibleTo(sheet) and sheet.submit_button.isEnabled()


class HeldWriter(Writer):
    """A writer whose answers come only when the test says so."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.held: List[tuple] = []

    def instance(self, profile, on_operator) -> None:
        self.held.append(("instance", profile, lambda: on_operator("Verein")))

    def destinations(self, profile, on_done, on_failure) -> None:
        listed = list(self._destinations)
        self.held.append(("destinations", profile, lambda: on_done(listed, [])))

    def round_for(self, profile, _publication, _d, on_done) -> None:
        self.held.append(("round", profile, lambda: on_done(RoundAnswer(1))))

    def answer(self, what=None, profile=None) -> None:
        for name, asked, call in list(self.held):
            if (what is None or name == what) and (profile is None or asked is profile):
                self.held.remove((name, asked, call))
                call()


def test_answers_that_come_after_the_sheet_closed_change_nothing(tmp_path):
    # RG-M2b 7: no "C++ object already deleted", nothing painted.
    writer = HeldWriter()
    sheet = _sheet(tmp_path, writer)
    writer.answer("destinations")                    # asks the round
    sheet.reject()
    shiboken6.delete(sheet)                          # gone, as Qt deletes a closed sheet
    assert not shiboken6.isValid(sheet)
    writer.answer()                                  # instance and round, late


def test_after_another_account_is_chosen_the_first_ones_answers_are_dropped(tmp_path):
    writer = HeldWriter()
    sheet = _sheet(tmp_path, writer)
    first = sheet.account.profile()
    other = Profile(user_pubkey="c" * 64, bunker_pubkey="d" * 64, display_name="Max",
                    bunker_relays=["wss://bunker.example"], local_secret_hex="1" * 64)
    sheet._store.upsert(other)
    writer._destinations = []                        # the other account has none
    sheet.account._switch(sheet._store.get(other.user_pubkey))
    writer.answer(profile=first)                     # the first account's, late
    assert sheet.picker.isEnabled() is False and sheet._operator == ""
    writer.answer()
    assert sheet._operator == "Verein" and sheet.status.text() == \
        i18n._("No publication takes submissions right now.")
