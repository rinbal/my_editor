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
from nostr import failures
from nostr.standup import drafting, words
from nostr.standup.drafting import ArticleDraft
from nostr.standup.submitting import MessageStatus, Progress, Step
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
        self.loads = 0
        self.bound_asked = 0
        self.reloaded = 0
        self.fail_destinations = False
        self.destinations_error = None      # what StandupApi gives: a StandupError

    def signer_bound(self, _profile, done) -> None:
        self.bound_asked += 1
        done()

    def instance(self, _profile, on_operator) -> None:
        if self.operator:
            on_operator(self.operator)

    def destinations(self, _profile, on_done, on_failure) -> None:
        self.loads += 1
        if self.fail_destinations:
            from nostr.standup.errors import Code, StandupError
            on_failure(self.destinations_error or StandupError(Code.OFFLINE))
        else:
            on_done(self._destinations, self._used)

    def reload(self, _profile) -> None:
        self.reloaded += 1

    def running(self, _profile, _d, _publication=""):
        return self.on_its_way

    def round_for(self, _profile, _publication, _d, on_done) -> None:
        self.asked += 1
        on_done(self.round if isinstance(self.round, RoundAnswer) else RoundAnswer(*self.round))

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
        self.let_go_ = 0
        self.behind: List[object] = []
        self.progress = Progress(Step.SIGNING)

    def behind_the_sheet(self, on_change) -> None:
        self.behind.append(on_change)

    def let_go(self) -> None:
        self.let_go_ += 1

    def start(self) -> None:
        self.started += 1
        self.changed.emit(self.progress)

    def cancel(self) -> None:
        self.cancelled += 1
        self.progress = Progress(Step.CANCELLED)


DRAFT = ArticleDraft(d="ein-jahr", title="Ein Jahr mit Lightning im Café",
                     summary="Wie ein Café in Leipzig ein Jahr lang mit Bitcoin bezahlt wurde.",
                     content="Ein Text.")


def _sheet(tmp_path, writer=None, *, draft=DRAFT, dark=False, private=(),
           public=0, relays=("wss://bunker.example",)) -> SubmitSheet:
    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = Profile(user_pubkey=WRITER, bunker_pubkey="b" * 64, display_name="Jana Roth",
                      bunker_relays=list(relays), local_secret_hex="0" * 64)
    store.upsert(profile)
    return SubmitSheet(writer=writer or Writer(), draft=draft, cover=QPixmap(),
                       byline="Jana Roth · Artikel · 1 Bild", private_names=private,
                       profile=profile, store=store, avatars=AvatarStore(),
                       request_logo=lambda *_a: None, is_dark=dark, public_pictures=public)


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


def test_a_message_the_signer_connection_cannot_carry_is_said_before_submitting(tmp_path):
    # P1, contract 12.5: sealed, it is more than relay.nip46.com carries,
    # though within what STANDUP takes.
    message = "ж" * 14_500
    sheet = _sheet(tmp_path, Writer(destinations=(BLOG,)), relays=["wss://relay.nip46.com"])
    sheet.rights.setChecked(True)
    sheet.message.setPlainText(message)
    assert not sheet.submit_button.isEnabled()
    assert sheet.status.text() == i18n._(drafting.MESSAGE_TOO_LONG_FOR_CONNECTION)
    (tmp_path / "open").mkdir()
    sheet = _sheet(tmp_path / "open", Writer(destinations=(BLOG,)))
    sheet.rights.setChecked(True)
    sheet.message.setPlainText(message)
    assert sheet.submit_button.isEnabled()


def test_a_message_waits_for_what_the_signer_connection_carries(tmp_path):
    # Lesson 29: the seal is measured over the relays read as the signer
    # is got; until then the sheet waits, and says for what.
    writer = HeldWriter(destinations=(BLOG,))
    sheet = _sheet(tmp_path, writer, relays=["wss://relay.nip46.com"])
    writer.answer("destinations")
    writer.answer("round")
    sheet.rights.setChecked(True)
    assert sheet.submit_button.isEnabled()                  # no message: nothing to measure
    sheet.message.setPlainText("Hallo Redaktion")
    assert not sheet.submit_button.isEnabled()
    assert sheet.status.text() == i18n._(words.WAITING_FOR_SIGNER)
    writer.answer("bound")
    assert sheet.submit_button.isEnabled() and sheet.status.isHidden()


def test_the_bound_is_asked_for_the_account_the_sheet_shows(tmp_path):
    writer = Writer(destinations=(BLOG,))
    _sheet(tmp_path, writer)
    assert writer.bound_asked == 1


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


def test_what_exactly_is_wrong_is_in_the_tooltip_not_the_words(tmp_path):
    # Lesson 27: the key goes in the tooltip and the accessible description.
    from dataclasses import replace
    sheet = _sheet(tmp_path, draft=replace(DRAFT, authors=(("ff" * 32, ""),)))
    assert sheet.status.text() == i18n._(drafting.COAUTHOR_INVALID)
    assert "ff" * 32 not in sheet.status.text()
    assert "ff" * 32 in sheet.status.toolTip()
    assert sheet.status.accessibleDescription() == sheet.status.toolTip()
    assert not sheet.submit_button.isEnabled()
    clean = _sheet(tmp_path)
    assert clean.status.toolTip() == "" and clean.submit_button.isEnabled()


def test_a_required_point_of_the_publications_checklist_that_is_missing_blocks(tmp_path):
    from dataclasses import replace
    asking = _destination("magazin", "Einundzwanzig Magazin", checklist=[
        {"id": "summary", "label": "Zusammenfassung", "level": "required"}])
    sheet = _sheet(tmp_path, Writer(destinations=(asking,)), draft=replace(DRAFT, summary=""))
    assert sheet.status.text() == i18n._(drafting.REQUIRED_MISSING).format(
        publication="Einundzwanzig Magazin", items="Zusammenfassung")
    assert not sheet.submit_button.isEnabled()


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


def test_pictures_that_stay_public_are_counted_and_never_called_encrypted(tmp_path):
    # Contract 7 (feat/redaktion ee3e5d7): the lines called every picture
    # encrypted, public links too.
    from nostr.standup.drafting import LocalPicture
    one = LocalPicture(key="k", name="a.jpg", mime="image/jpeg", data=b"1")
    words = _sheet(tmp_path, draft=ArticleDraft(d="a", title="T", summary="", content="x",
                                                pictures=(one,)), public=2).privacy_words()
    assert words[2:4] == [
        "1 Bild liegt verschlüsselt auf deinem Medienserver. Er sieht, wann es abgerufen "
        "wird, nie was darin ist.",
        "2 Bilder waren schon öffentlich im Netz und bleiben öffentlich."]
    only_public = _sheet(tmp_path, public=1).privacy_words()
    assert "1 Bild war schon öffentlich im Netz und bleibt öffentlich." in only_public
    assert not any("verschlüsselt auf deinem Medienserver" in line for line in only_public)


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
    assert sheet.handoff.headline.text() == "Warten auf deine Signer-App."
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
    assert again.handoff.headline.text() == "Warten auf deine Signer-App."
    assert len(writer.submitted) == 1 and again.run.started == 1
    assert again.run.behind[-1] is None             # the sheet says it now, not the bar


@pytest.mark.parametrize("message, let_go", [
    (MessageStatus.FAILED, 1),                      # Send Again was there
    (MessageStatus.SIGNING, 0),                     # it goes on, said in the bar
    (MessageStatus.SENDING, 0),
])
def test_closed_on_a_first_message_that_did_not_go_the_run_is_let_go(tmp_path, message, let_go):
    writer = Writer()
    sheet = _sheet(tmp_path, writer)
    sheet.submit_button.click()
    sheet.run.progress = Progress(Step.SUBMITTED, sent=True, receipt=object(), message=message)
    run = sheet.run
    sheet.handoff.close_requested.emit()
    assert run.let_go_ == let_go and writer.background == [run]


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

    def signer_bound(self, profile, done) -> None:
        self.held.append(("bound", profile, done))

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


# -- every wait has its way out (RG-M2r 4, lesson 12) ----------------------------

NOT_CHECKED = "MyEditor konnte nicht prüfen, ob du diesen Text schon eingereicht hast"


@pytest.mark.parametrize("answer, from_list", [
    ((None, "Deine Liste der Einreichungen konnte nicht gelesen werden.", None, True), True),
    ((None, "Deine Signer-App hat nicht geantwortet.", None, True), True),     # blocked
    ((None, i18n._(failures.CHECK_CONNECTION), None, True), True),             # in part
    ((None, i18n._(failures.CHECK_CONNECTION)), False),                       # no connection
    ((None, i18n._(drafting.ROUND_UNKNOWN)), False),
], ids=["list unreadable", "list blocked", "list in part", "no connection", "round unknown"])
def test_a_round_that_could_not_be_told_offers_try_again(tmp_path, answer, from_list):
    # Said once (lessons 36): what did not happen, then the one sentence.
    writer = Writer(round_=answer)
    sheet = _sheet(tmp_path, writer)
    assert not sheet.retry_load.isHidden()
    assert sheet.status.shown == (NOT_CHECKED, answer[1])
    assert not sheet.submit_button.isEnabled()
    asked = writer.asked
    writer.round = (1, "")
    sheet.retry_load.click()
    # The list is read again only when it could not tell; the round is
    # asked again either way.
    assert (writer.reloaded, writer.asked) == (int(from_list), asked + 1)
    assert sheet.retry_load.isHidden() and sheet.submit_button.isEnabled()


def test_a_round_another_device_stored_is_said_calmly_with_reload_and_never_offered(tmp_path):
    # RG-M2r3 1, contract 2 at 0456312: no Submit, no earlier try to send
    # again or discard; "Erneut laden" reads the list again.
    elsewhere = object()
    writer = Writer(round_=RoundAnswer(None, i18n._(words.ELSEWHERE_NOT_HERE_YET),
                                       from_list=True, elsewhere=elsewhere,
                                       title=words.SUBMITTED_ELSEWHERE))
    sheet = _sheet(tmp_path, writer)
    assert sheet.status.shown == (i18n._(words.SUBMITTED_ELSEWHERE),
                                  i18n._(words.ELSEWHERE_NOT_HERE_YET))
    assert not sheet.status._error                  # no error: it is submitted
    assert sheet.retry_load.text() == "Erneut laden" and not sheet.retry_load.isHidden()
    assert sheet.unsent.isHidden() and not sheet.submit_button.isEnabled()
    asked = writer.asked
    sheet.retry_load.click()
    assert (writer.reloaded, writer.asked) == (1, asked + 1)
    # Any other answer: Try Again again, said in the error color.
    writer.round = RoundAnswer(None, i18n._(drafting.ROUND_UNKNOWN))
    sheet.retry_load.click()
    assert sheet.retry_load.text() == "Erneut versuchen" and sheet.status._error


@pytest.mark.parametrize("code, sentence", [
    ("offline", "Prüfe deine Internetverbindung und versuche es erneut."),
    ("timeout", "Prüfe deine Internetverbindung und versuche es erneut."),
    # RG-M2r3 6: a server that answered is never blamed on the connection.
    ("server", "SERVER"),
    ("bad_response", "SERVER"),
])
def test_publications_that_could_not_be_loaded_offer_try_again(tmp_path, code, sentence):
    from nostr.standup.errors import StandupError
    writer = Writer()
    writer.fail_destinations = True
    writer.destinations_error = StandupError(code)
    sheet = _sheet(tmp_path, writer)
    assert not sheet.retry_load.isHidden()
    assert sheet.status.shown == ("Die Publikationen konnten nicht geladen werden",
                                  i18n._(failures.SERVER_TROUBLE) if sentence == "SERVER"
                                  else sentence)
    writer.fail_destinations = False
    sheet.retry_load.click()
    assert writer.loads == 2 and sheet.retry_load.isHidden()
    assert sheet.submit_button.isEnabled()


def test_a_total_failure_is_one_status_with_one_action_said_once(tmp_path, monkeypatch):
    # Lessons 36: a title, one sentence, Try Again; announced once however
    # often the sheet refreshes.
    from nostr.ui import publish_sheet
    said = []
    monkeypatch.setattr(publish_sheet.announce, "announce", lambda _w, text: said.append(text))
    writer = Writer()
    writer.fail_destinations = True
    sheet = _sheet(tmp_path, writer)
    sheet._refresh()
    sheet._refresh()
    assert said == ["Die Publikationen konnten nicht geladen werden. Prüfe deine "
                    "Internetverbindung und versuche es erneut."]
    assert sheet.status.text().startswith("<b>Die Publikationen konnten nicht geladen "
                                          "werden</b><br>")
    assert not sheet.retry_load.isHidden() and not sheet.submit_button.isEnabled()
    assert sheet.status._error                     # in the error color, as it failed


NOW = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    """The sheet's clock, moved by the test (lesson 34: no wall-clock wait)."""
    from nostr.ui import standup_submit_sheet
    now = [NOW]
    monkeypatch.setattr(standup_submit_sheet, "_now", lambda: now[0])
    return now


def _wait_over(sheet, clock, seconds) -> None:
    """The wait passes: the sheet's timer fires, as Qt would fire it."""
    assert sheet._wait.isActive() and sheet._wait.interval() == seconds * 1000
    clock[0] += seconds
    sheet._wait.timeout.emit()


def test_a_round_lookup_met_a_limit_says_its_wait_and_try_again_waits(tmp_path, clock,
                                                                      monkeypatch):
    # RG-M2r2a 5: a 429 read as a round that cannot be told, and Try
    # Again asked again at once, one more counted request each time.
    from nostr.ui.standup_handoff import wait_words
    writer = Writer(round_=RoundAnswer(None, limited=True, retry_at=NOW + 120))
    sheet = _sheet(tmp_path, writer)
    clock_time = wait_words(NOW + 120, NOW, "{time}", "{time}")
    assert sheet.status.shown == (NOT_CHECKED, "Du hast Einundzwanzig Magazin vorerst so viel "
                                  f"gesendet, wie dort angenommen wird: Versuche es um {clock_time} "
                                  "erneut.")
    assert not sheet.retry_load.isHidden() and not sheet.retry_load.isEnabled()
    assert not sheet.submit_button.isEnabled()
    asked = writer.asked
    sheet._try_again()                              # Return, a shortcut: still waits
    assert writer.asked == asked
    writer.round = (1, "")
    from nostr.ui import publish_sheet
    said = []
    monkeypatch.setattr(publish_sheet.announce, "announce", lambda _w, text: said.append(text))
    _wait_over(sheet, clock, 120)
    assert sheet.retry_load.isEnabled() and not sheet._wait.isActive()
    # RG-M2r3 14: Try Again works now, and the words say so, unannounced.
    assert sheet.status.shown == (NOT_CHECKED, "Die Wartezeit bei Einundzwanzig Magazin ist "
                                  "vorbei: Du kannst es jetzt erneut versuchen.")
    assert said == []
    sheet.retry_load.click()
    assert writer.asked == asked + 1 and sheet.submit_button.isEnabled()


def test_a_limit_without_a_wait_is_said_and_try_again_may_ask_at_once(tmp_path, clock):
    writer = Writer(round_=RoundAnswer(None, limited=True))
    sheet = _sheet(tmp_path, writer)
    assert sheet.status.shown == (NOT_CHECKED, i18n._(words.refusal("rate_limited")).format(
        publication="Einundzwanzig Magazin"))
    assert sheet.retry_load.isEnabled() and not sheet._wait.isActive()


def test_publications_that_met_a_limit_wait_for_it_too(tmp_path, clock, monkeypatch):
    from nostr.standup.errors import Code, StandupError
    writer = Writer()
    writer.fail_destinations = True
    writer.destinations_error = StandupError(Code.RATE_LIMITED, status=429, retry_after=600)
    sheet = _sheet(tmp_path, writer)
    title, sentence = sheet.status.shown
    assert title == "Die Publikationen konnten nicht geladen werden"
    assert sentence.startswith("Der Server nimmt gerade nichts mehr von dir an: Versuche es um ")
    assert not sheet.retry_load.isEnabled()
    sheet._try_again()
    assert writer.loads == 1
    writer.fail_destinations = False
    from nostr.ui import publish_sheet
    said = []
    monkeypatch.setattr(publish_sheet.announce, "announce", lambda _w, text: said.append(text))
    _wait_over(sheet, clock, 600)
    assert sheet.status.shown == ("Die Publikationen konnten nicht geladen werden",
                                  "Die Wartezeit beim Server ist vorbei: Du kannst es jetzt "
                                  "erneut versuchen.")
    assert said == []
    sheet.retry_load.click()
    assert writer.loads == 2 and sheet.retry_load.isHidden()


def test_a_round_step_that_met_its_bounds_says_why_and_offers_no_try_again(tmp_path):
    # Contract 2 (bde1274): asking again would meet the same 100 rounds
    # or 32 MiB; nothing is submitted, and the words say why.
    words = i18n._(drafting.ROUND_LIMIT)
    sheet = _sheet(tmp_path, Writer(round_=(None, words, None, False, True)))
    assert sheet.retry_load.isHidden() and not sheet.submit_button.isEnabled()
    assert sheet.status.shown == (NOT_CHECKED, words)


def test_a_submission_whose_pictures_are_missing_elsewhere_is_said_in_the_contracts_words(
        tmp_path, german):
    # Contract 12.4 at 33d92c9: stored with pictures missing, and the
    # app that sent it uploads them; nothing to try again here, and never
    # "eingereicht".
    from nostr.standup import rounds
    writer = Writer(round_=RoundAnswer(None, i18n._(words.PICTURES_IN_THE_APP_THAT_SENT),
                                       final=True, title=words.PICTURE_NOT_IN))
    sheet = _sheet(tmp_path, writer)
    assert sheet.status.shown == ("Deiner Einreichung fehlt ein Bild",
                                  "Öffne die Einreichung in der App, mit der du sie gesendet "
                                  "hast: Sie lädt sie erneut hoch.")
    assert sheet.retry_load.isHidden() and not sheet.submit_button.isEnabled()
    assert "eingereicht" not in " ".join(sheet.status.shown).lower()
    assert rounds.PICTURES_IN_THE_APP_THAT_SENT is words.PICTURES_IN_THE_APP_THAT_SENT


def test_a_round_that_is_told_offers_no_try_again(tmp_path):
    sheet = _sheet(tmp_path, Writer(round_=(1, "")))
    assert sheet.retry_load.isHidden()


def test_the_privacy_lines_are_said_without_a_sheet_before_a_publication_is_chosen():
    # RG-M2r 11: the lines moved out of the sheet (standup_privacy).
    from nostr.ui.standup_privacy import privacy_words
    words = privacy_words("", 0, 0, "")
    assert words[0] == "Lesen können nur du und die Redaktion von der Publikation."
    assert words[-1] == "Der Server bewahrt die Einreichung verschlüsselt auf."
    assert len(words) == 4                          # no pictures: no line about them
