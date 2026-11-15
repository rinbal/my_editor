# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The delivery of a submission in motion (nostr/ui/standup_handoff.py,
plan 10.3; the owner's choice "Einwurf").

Pinned here: each state's words, the plan's German exactly; that the
article stays above the publication's inbox, hovering right over it
while the request is on its way or unconfirmed, and drops in only in
the DONE phase (which only a matching receipt reaches,
test_standup_submitting.py), where the inbox becomes the receipt that
holds publication and article; that it is back in its place when
nothing was sent; that with Reduce Motion nothing travels and the
states cross-fade in 120 ms; the facts on the article and on the
receipt; that every state is announced once; and the buttons each
state has."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

import i18n
from nostr import failures
from nostr.standup import words
from nostr.standup.submitting import (
    HELD_WORDS, Failure, Held, MessageStatus, Phase, Progress, Step,
)
from nostr.ui import motion, standup_handoff
from nostr.ui.standup_handoff import HandoffView, phase_words

PUBLICATION = "Einundzwanzig Magazin"
TITLE = "Ein Jahr mit Lightning im Café"


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def german():
    i18n.install("de")
    yield
    i18n.install("en")


@pytest.fixture
def view(app):
    view = HandoffView(is_dark=False)
    view.set_work(title=TITLE, cover=standup_handoff.QPixmap(), publication=PUBLICATION,
                  logo=standup_handoff.QPixmap(), pictures=1)
    view.resize(720, 380)
    return view


@pytest.fixture
def still(monkeypatch):
    monkeypatch.setattr(motion, "reduce_motion", lambda: True)


@pytest.fixture
def moving(monkeypatch):
    monkeypatch.setattr(motion, "reduce_motion", lambda: False)


NOT_SENT = Failure(failures.CHECK_CONNECTION, Step.SIGNING, title=words.NOT_SUBMITTED)


@pytest.mark.parametrize("progress, expected", [
    (Progress(Step.KEY), ("Wird verschlüsselt …", "")),
    (Progress(Step.ENCRYPTING), ("Wird verschlüsselt …", "")),
    (Progress(Step.UPLOADING, pictures=1),
     ("Bild wird hochgeladen …", "Verschlüsselt, auf deinen Medienserver.")),
    (Progress(Step.UPLOADING, pictures=2),
     ("Bilder werden hochgeladen …", "Verschlüsselt, auf deinen Medienserver.")),
    (Progress(Step.RECORDING), ("Warten auf deine Signer-App.", "Bestätige dort die Anfrage.")),
    (Progress(Step.SIGNING), ("Warten auf deine Signer-App.", "Bestätige dort die Anfrage.")),
    (Progress(Step.SENDING), ("Wird an Einundzwanzig Magazin gesendet …", "")),
    (Progress(Step.CHECKING), ("Noch nicht bestätigt. Wird geprüft …",
                               "Du kannst das Fenster schließen, MyEditor prüft weiter.")),
    (Progress(Step.SUBMITTED), ("Eingereicht",
                                "Antworten der Redaktion siehst du unter Einreichungen.")),
    (Progress(Step.FAILED, failure=NOT_SENT),
     ("Dein Artikel wurde nicht eingereicht",
      "Prüfe deine Internetverbindung und versuche es erneut.")),
])
def test_each_state_says_the_plans_words(german, progress, expected):
    assert phase_words(progress, PUBLICATION) == expected


@pytest.mark.parametrize("held, sending, checking", [
    (Held.NOTHING, "Wird an Einundzwanzig Magazin gesendet …",
     "Noch nicht bestätigt. Wird geprüft …"),
    (Held.MAYBE, "Wird an Einundzwanzig Magazin gesendet …",
     "Noch nicht bestätigt. Wird geprüft …"),
    (Held.STORED, "Der Server hat deine Einreichung. Sie wird mit dem Bild noch einmal "
     "gesendet …", "Der Server hat deine Einreichung. Das Bild wird geprüft …"),
    (Held.OTHER, "Wird an Einundzwanzig Magazin gesendet …",
     "Noch nicht bestätigt. Wird geprüft …"),
], ids=["nothing", "maybe", "stored", "other"])
def test_on_its_way_and_while_checked_the_words_say_what_is_held(german, held, sending,
                                                                 checking):
    # RG-M2r2a 3: a run whose receipt came (a picture missing) said "Not
    # confirmed yet" once the body went again and its answer was lost.
    assert phase_words(Progress(Step.SENDING, held=held), PUBLICATION)[0] == sending
    assert phase_words(Progress(Step.CHECKING, held=held), PUBLICATION)[0] == checking
    assert set(HELD_WORDS) == set(Held)


def test_another_devices_round_has_the_owners_words_in_both_languages(german):
    # Decided by the owner (2026-11-10): the English stays, the German is
    # the contract's.
    assert words.SUBMITTED_ELSEWHERE == "Submitted on another device."
    assert i18n._(words.SUBMITTED_ELSEWHERE) == "Auf einem anderen Gerät eingereicht."


def test_a_kept_copy_that_is_not_the_writers_is_said_once_with_send_again(german, view):
    # 12.4 at bfa8844: not handed in, with "Erneut senden", which sends the
    # next revision; no words about another copy.
    failure = Failure(words.NOT_HANDED_IN, Step.REVISING, title=words.NOT_SUBMITTED)
    view.show_progress(Progress(Step.FAILED, failure=failure, held=Held.OTHER))
    assert view.headline.text() == i18n._(words.NOT_SUBMITTED)
    assert "{publication}" not in view.detail.text() and "Kopie" not in view.detail.text()
    assert _shown(view) == {"close", "retry"} and view.retry_button.text() == "Erneut senden"
    assert view.close_button.text() == "Abbrechen"


def test_another_devices_round_is_said_calmly_with_reload(german, view):
    # Contract 2 at 72d5ddc: "Auf einem anderen Gerät eingereicht." with
    # Erneut laden; not an error, so not in the error colour.
    view.show_progress(Progress(Step.RELOADING, held=Held.OTHER))
    assert (view.headline.text(), view.detail.text()) == (
        "Auf einem anderen Gerät eingereicht.", "Deine Liste der Einreichungen wird gelesen …")
    assert _shown(view) == {"close"}
    waiting = Failure(words.ELSEWHERE_NOT_HERE_YET, Step.RELOADING,
                      title=words.SUBMITTED_ELSEWHERE, error=False)
    view.show_progress(Progress(Step.FAILED, failure=waiting, held=Held.OTHER))
    assert view.headline.text() == "Auf einem anderen Gerät eingereicht."
    assert view.detail.text() == "Die Angaben dazu sind noch nicht auf diesem Gerät."
    assert _shown(view) == {"close", "retry"} and view.retry_button.text() == "Erneut laden"
    assert "color: " + standup_handoff.appearance.roles(False).text in view.headline.styleSheet()
    view.show_progress(Progress(Step.FAILED, failure=NOT_SENT))
    assert view.retry_button.text() == "Erneut versuchen"
    assert "color: " + standup_handoff.appearance.roles(False).error in view.headline.styleSheet()
    arrived = Failure(words.ELSEWHERE_HERE_NOW, None, title=words.SUBMITTED_ELSEWHERE,
                      error=False)
    view.show_progress(Progress(Step.FAILED, failure=arrived, held=Held.OTHER))
    assert view.detail.text() == "Die Angaben dazu sind jetzt auf diesem Gerät."
    assert _shown(view) == {"close"}
    assert "color: " + standup_handoff.appearance.roles(False).text in view.headline.styleSheet()


def test_a_list_that_could_not_be_read_says_it_in_its_own_words(german):
    # Its own words in the sentence's place, under the title of what the
    # server holds (lesson 32, lessons 36): one sentence, never two.
    failure = Failure(words.LIST_NOT_SAVED, Step.RECORDING, said="Deine Liste …",
                      title=words.NOT_SUBMITTED)
    assert phase_words(Progress(Step.FAILED, failure=failure), PUBLICATION) == (
        "Dein Artikel wurde nicht eingereicht", "Deine Liste …")


def _settled(stage):
    """The stage where its motion ends."""
    for name in ("hover", "settle", "accent", "steps"):
        stage._set(name, stage.target(name))


@pytest.mark.parametrize("step", [s for s in Step if s is not Step.SUBMITTED])
def test_the_article_stays_above_the_inbox_until_the_server_confirmed_it(view, moving, step):
    view.show_progress(Progress(step, failure=NOT_SENT if step is Step.FAILED else None))
    stage = view.stage
    assert not stage.received and stage.target("settle") == 0
    _settled(stage)
    assert stage.row_rect().bottom() < stage.inbox_rect().top()     # never on it
    assert "In the inbox" not in stage.accessibleName()


def test_a_receipt_drops_the_article_in_and_the_inbox_is_the_receipt_of_both(view, moving):
    stage = view.stage
    view.show_progress(Progress(Step.SENDING))
    waiting = stage.full_height()
    view.show_progress(Progress(Step.SUBMITTED))
    assert stage.received and stage.animating("settle") and stage.target("settle") == 1.0
    _settled(stage)
    # The article and the way closed up; what is left is the receipt,
    # which holds the publication, the check and the article.
    assert stage.full_height() == waiting - 52 and view.stage.height() == stage.full_height()
    assert stage.inbox_rect().height() == 118
    for said in (PUBLICATION, TITLE, "Article · Version 1 · 1 picture", "In the inbox"):
        assert said in stage.accessibleName()
    assert not view.grab().isNull()


def test_on_its_way_the_article_hovers_over_the_inbox_and_goes_back_when_not_sent(view, moving):
    stage = view.stage
    view.show_progress(Progress(Step.SIGNING))
    assert stage.target("hover") == 0.0 and stage.target("steps") == 2.0
    assert stage.animating("breath")                    # the step that waits breathes
    view.show_progress(Progress(Step.SENDING))
    assert stage.target("hover") == 1.0 and stage.target("accent") == 1.0
    assert not stage.animating("breath")
    view.show_progress(Progress(Step.CHECKING))
    assert stage.target("hover") == 1.0 and stage.target("steps") == 3.0     # sent
    view.show_progress(Progress(Step.FAILED, failure=NOT_SENT))
    assert (stage.target("hover"), stage.target("steps"), stage.target("accent")) == (0, 0, 0)


def test_with_reduce_motion_nothing_travels_and_the_states_cross_fade(view, still):
    stage = view.stage
    view.show()
    for step in (Step.ENCRYPTING, Step.SIGNING, Step.SENDING, Step.CHECKING):
        view.show_progress(Progress(step))
        assert stage.value("hover") == 0 and not stage.animating("hover")
        assert not stage.animating("breath")
    view.show_progress(Progress(Step.SUBMITTED))
    assert stage.value("settle") == 1.0 and not stage.animating("settle")    # at once
    fade = stage._animations["mix"]
    assert fade.duration() == motion.CROSS_FADE_MS and fade.endValue() == 1.0
    view.hide()


def test_the_words_stay_the_same_with_reduce_motion(german, view, still):
    view.show_progress(Progress(Step.SENDING))
    assert view.headline.text() == "Wird an Einundzwanzig Magazin gesendet …"


def test_every_state_is_announced_once(view, monkeypatch):
    said = []
    monkeypatch.setattr(standup_handoff.announce, "announce", lambda _w, text: said.append(text))
    view.show_progress(Progress(Step.UPLOADING, pictures=1, uploaded=0.2))
    view.show_progress(Progress(Step.UPLOADING, pictures=1, uploaded=0.6))
    view.show_progress(Progress(Step.SIGNING))
    view.show_progress(Progress(Step.SUBMITTED))
    assert said == ["Uploading picture… Encrypted, to your media server.",
                    "Waiting for your signer app. Confirm the request there.",
                    "Submitted. The server confirmed receipt. "
                    "You see the editorial team’s answers under Submissions."]


def test_the_bar_shows_only_while_pictures_upload(view):
    view.show_progress(Progress(Step.UPLOADING, pictures=1, uploaded=0.4))
    assert view.stage._bar_shown and view.stage.target("bar") == 0.4
    view.show_progress(Progress(Step.SENDING, pictures=1))
    assert not view.stage._bar_shown
    view.show_progress(Progress(Step.UPLOADING, pictures=0))
    assert not view.stage._bar_shown


class _Receipt:
    received_at = 0


def test_the_facts_say_what_goes_and_once_it_is_in_when_and_how(german, view, monkeypatch):
    assert standup_handoff.work_facts(1, 3) == "Artikel · Fassung 1 · 3 Bilder"
    assert standup_handoff.work_facts(2) == "Artikel · Fassung 2"       # no pictures: not said
    assert view.stage.facts == "Artikel · Fassung 1 · 1 Bild"
    view.show_progress(Progress(Step.SENDING))
    assert view.facts.isHidden()
    receipt = _Receipt()
    receipt.received_at = int(_at(3, 21))
    monkeypatch.setattr(standup_handoff, "_now", lambda: _at(3, 22))
    view.show_progress(Progress(Step.SUBMITTED, receipt=receipt))
    assert not view.facts.isHidden()
    assert view.facts.text() == "Heute, 03:21 · Ende-zu-Ende verschlüsselt"
    # A message that went with it is one of the facts, with no line of its own.
    view.show_progress(Progress(Step.SUBMITTED, receipt=receipt, message=MessageStatus.SENT))
    assert view.facts.text() == ("Heute, 03:21 · Ende-zu-Ende verschlüsselt · "
                                 "mit deiner Nachricht")
    assert view.message.isHidden()
    # A time that is not known is left out.
    view.show_progress(Progress(Step.SUBMITTED))
    assert view.facts.text() == "Ende-zu-Ende verschlüsselt"


def _shown(view):
    return {name for name in ("background", "cancel", "close", "retry", "done")
            if not getattr(view, f"{name}_button").isHidden()}


@pytest.mark.parametrize("progress, shown, enabled_cancel, default", [
    (Progress(Step.SIGNING), {"background", "cancel"}, True, None),
    (Progress(Step.SENDING), {"background", "cancel"}, False, None),
    # UX-M1: Close standing alone answers Return, as a sheet's one button does.
    (Progress(Step.CHECKING), {"close"}, None, "close"),
    (Progress(Step.FAILED, failure=NOT_SENT), {"close", "retry"}, None, "retry"),
    (Progress(Step.FAILED, failure=Failure(words.ANOTHER_VERSION, None,
                                           title=words.VERSION_NOT_SUBMITTED)),
     {"close"}, None, "close"),
    (Progress(Step.SUBMITTED), {"done"}, None, "done"),
])
def test_each_state_has_its_buttons(view, progress, shown, enabled_cancel, default):
    view.show_progress(progress)
    assert _shown(view) == shown
    if enabled_cancel is not None:
        assert view.cancel_button.isEnabled() is enabled_cancel
    defaults = {n for n in ("background", "cancel", "close", "retry", "done")
                if getattr(view, f"{n}_button").isDefault()}
    assert defaults == ({default} if default else set())


def test_the_buttons_stand_together_at_the_trailing_end_the_default_one_last(app, view):
    # UX-M1: Continue in Background stood alone at the leading end, Cancel at
    # the trailing one; View Submission and Done in opposite corners.
    from PySide6.QtGui import QGuiApplication
    import theme
    before = app.style().name() or "Fusion"
    app.setStyle(theme.AppStyle("Fusion"))          # the app's own: the Mac's order everywhere
    try:
        view.buttons.resize(700, 40)
        view.buttons.show()
        view.show_progress(Progress(Step.SIGNING))
        QGuiApplication.processEvents()
        cancel, background = view.cancel_button.geometry(), view.background_button.geometry()
        assert cancel.right() < background.left() and background.right() > 600
        assert background.left() - cancel.right() < 20     # side by side
        view.show_progress(Progress(Step.SUBMITTED))
        QGuiApplication.processEvents()
        see, done = view.view_button.geometry(), view.done_button.geometry()
        assert see.right() < done.left() and done.right() > 600
        assert done.left() - see.right() < 20 and view.done_button.isDefault()
    finally:
        view.buttons.hide()
        app.setStyle(before)


def test_a_message_that_was_not_sent_can_be_sent_again_from_the_handoff(view, monkeypatch):
    said, asked = [], []
    monkeypatch.setattr(standup_handoff.announce, "announce", lambda _w, text: said.append(text))
    view.retry_message_requested.connect(lambda: asked.append(True))
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.SIGNING))
    assert view.send_again.isHidden() and view.message.text() == words.MESSAGE_SIGNING
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.SENDING))
    assert view.send_again.isHidden() and view.message.text() == words.MESSAGE_SENDING
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.FAILED,
                                message_failure=Failure(failures.CHECK_CONNECTION,
                                                        Step.SUBMITTED,
                                                        title=words.MESSAGE_NOT_SENT)))
    assert not view.send_again.isHidden()
    # RG-M2r3 7: its title, and one sentence with the cause and what to do,
    # shown and said.
    assert view.message.text() == failures.spoken(words.MESSAGE_NOT_SENT,
                                                  failures.CHECK_CONNECTION)
    assert said[-1].endswith(failures.spoken(words.MESSAGE_NOT_SENT, failures.CHECK_CONNECTION))
    view.send_again.click()
    assert asked == [True]


def test_painting_every_state_in_both_themes_does_not_fail(view):
    for dark in (False, True):
        view.set_dark(dark)
        for step in Step:
            view.show_progress(Progress(step, pictures=1, failure=NOT_SENT))
            assert not view.grab().isNull()
    assert Phase.DONE.value == "done"


# -- a limit's wait (RG-M2r 7) --------------------------------------------------

def _at(hour: int, minute: int, second: int = 0, *, days: int = 0) -> float:
    from PySide6.QtCore import QDate, QDateTime, QTime
    moment = QDateTime(QDate(2026, 11, 9).addDays(days), QTime(hour, minute, second))
    return float(moment.toSecsSinceEpoch())


def _clock(monkeypatch, at):
    now = {"at": at}
    monkeypatch.setattr(standup_handoff, "_now", lambda: now["at"])
    return now


def _limited(retry_at):
    return Failure(words.refusal("rate_limited"), Step.SIGNING, title=words.NOT_SUBMITTED,
                   retry_at=retry_at)


def test_try_again_waits_for_the_limit_and_the_words_say_until_when(view, monkeypatch):
    now = _clock(monkeypatch, _at(14, 20))
    said = []
    monkeypatch.setattr(standup_handoff.announce, "announce", lambda _w, text: said.append(text))
    view.show_progress(Progress(Step.FAILED, failure=_limited(_at(14, 29, 30))))
    assert len(said) == 1
    assert not view.retry_button.isHidden() and not view.retry_button.isEnabled()
    assert not view.retry_button.isDefault()
    clock = standup_handoff.wait_words(_at(14, 29, 30), now["at"], "{time}", "{time}")
    assert clock in ("2:30\u202fPM", "2:30 PM", "14:30")       # the minute shown, rounded up
    # One sentence: the limit, and when trying again helps (lessons 36).
    assert view.detail.text() == (f"You’ve sent {PUBLICATION} as much as it takes for now: "
                                  f"try again at {clock}.")
    assert view._wait.isActive()
    now["at"] = _at(14, 29, 30)
    view._wait.timeout.emit()
    assert view.retry_button.isEnabled() and view.retry_button.isDefault()
    # RG-M2r3 14: the words say Try Again works now (they said "try again
    # later" beside an enabled Try Again), shown and not announced again.
    assert view.detail.text() == f"The wait for {PUBLICATION} is over: you can try again now."
    assert not view._wait.isActive() and len(said) == 1
    view.show_progress(Progress(Step.FAILED, failure=_limited(_at(14, 29, 30))))
    assert len(said) == 1                           # the same state, said once


def test_a_wait_past_midnight_says_tomorrow(german, view, monkeypatch):
    _clock(monkeypatch, _at(23, 50))
    view.show_progress(Progress(Step.FAILED, failure=_limited(_at(0, 5, days=1))))
    assert view.headline.text() == "Dein Artikel wurde nicht eingereicht"
    # RG-M2r3 15: no "es" for a publication whose gender is not known.
    assert view.detail.text() == ("Du hast Einundzwanzig Magazin vorerst so viel gesendet, wie "
                                  "dort angenommen wird: Versuche es morgen um 00:05 erneut.")


def test_send_again_waits_for_the_limit_of_messages(view, monkeypatch):
    now = _clock(monkeypatch, _at(9, 0))
    asked = []
    view.retry_message_requested.connect(lambda: asked.append(True))
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.FAILED,
                                message_failure=Failure(words.MESSAGE_LIMIT_LATER,
                                                        Step.SUBMITTED,
                                                        title=words.MESSAGE_NOT_SENT,
                                                        retry_at=_at(9, 1))))
    assert not view.send_again.isHidden() and not view.send_again.isEnabled()
    clock = standup_handoff.wait_words(_at(9, 1), now["at"], "{time}", "{time}")
    assert view.message.text() == failures.spoken(
        words.MESSAGE_NOT_SENT, "The server takes no more messages from you right now: send it "
        f"again at {clock}.")
    view.send_again.click()
    assert asked == []
    said = []
    monkeypatch.setattr(standup_handoff.announce, "announce", lambda _w, text: said.append(text))
    now["at"] = _at(9, 1)
    view._wait.timeout.emit()
    assert view.send_again.isEnabled()
    assert view.message.text() == failures.spoken(
        words.MESSAGE_NOT_SENT, "The wait for the server is over: you can send your message again "
        "now.")
    assert said == []                               # shown, not announced (RG-M2r3 14)
    view.send_again.click()
    assert asked == [True]


# -- a total failure speaks once (lessons 36) -----------------------------------------

def _visible_words(view):
    return [label.text() for label in (view.headline, view.detail, view.message, view.details)
            if not label.isHidden() and label.text()]


def test_a_total_failure_is_one_title_one_sentence_and_try_again_with_cancel(german, view):
    view.show_progress(Progress(Step.FAILED, failure=NOT_SENT))
    assert _visible_words(view) == ["Dein Artikel wurde nicht eingereicht",
                                    "Prüfe deine Internetverbindung und versuche es erneut."]
    assert _shown(view) == {"close", "retry"}
    assert (view.close_button.text(), view.retry_button.text()) == ("Abbrechen",
                                                                   "Erneut versuchen")
    assert view.details_toggle.isHidden()           # nothing technical to show


def test_a_failure_with_nothing_to_try_again_offers_close_alone(german, view):
    view.show_progress(Progress(Step.FAILED, failure=Failure(
        words.ANOTHER_VERSION, None, title=words.VERSION_NOT_SUBMITTED)))
    assert _shown(view) == {"close"} and view.close_button.text() == "Schließen"


def test_what_the_servers_answered_is_only_behind_details(view):
    failure = Failure(words.UPLOAD_FAILED, Step.UPLOADING, title=words.NOT_SUBMITTED,
                      details="https://media.example: 413 too large")
    view.show_progress(Progress(Step.FAILED, failure=failure))
    assert all("media.example" not in text for text in _visible_words(view))
    assert not view.details_toggle.isHidden() and view.details.isHidden()
    view.details_toggle.click()
    assert not view.details.isHidden() and view.details.text() == failure.details
    view.show_progress(Progress(Step.FAILED, failure=NOT_SENT))     # another failure
    assert view.details_toggle.isHidden() and view.details.isHidden()


def test_a_failure_is_announced_once_however_often_it_is_shown(view, monkeypatch):
    said = []
    monkeypatch.setattr(standup_handoff.announce, "announce", lambda _w, text: said.append(text))
    for _ in range(3):
        view.show_progress(Progress(Step.FAILED, failure=NOT_SENT))
    assert len(said) == 1


def test_in_a_low_window_the_column_is_drawn_smaller_and_the_words_stay_under_it(view):
    # The words were painted over the card and the inbox.
    scroll = view.scrolling()
    scroll.resize(400, 230)
    scroll.show()
    view.show_progress(Progress(Step.SIGNING))
    QApplication.processEvents()
    stage = view.stage
    assert standup_handoff._LEAST_ZOOM <= stage.zoom() < 1
    assert stage.inbox_rect().bottom() <= stage.height()
    assert stage.geometry().bottom() < view.headline.mapTo(view, view.headline.rect().topLeft()).y()
    assert scroll.verticalScrollBar().maximum() == 0        # all of it in view
    scroll.resize(400, 420)
    QApplication.processEvents()
    assert stage.zoom() == 1 and stage.height() == stage.full_height()
    scroll.hide()


def test_a_title_and_a_name_too_long_are_cut_and_whole_in_a_tooltip(app, view):
    title = "Ein Jahr mit Lightning im Café in Leipzig, und was ich dabei über Geld gelernt habe"
    name = "Zeitschrift für Wirtschaftsinformatik und Kryptowährungsforschung Leipzig"
    stage = view.stage
    view.resize(420, 380)
    view.set_work(title=title, cover=standup_handoff.QPixmap(), publication=name,
                  logo=standup_handoff.QPixmap())
    view.show_progress(Progress(Step.SIGNING))
    assert not view.grab().isNull()
    assert stage.tip_at(stage.row_rect().center()) == title
    assert stage.tip_at(stage.inbox_rect().center()) == name
    view.set_work(title=TITLE, cover=standup_handoff.QPixmap(), publication=PUBLICATION,
                  logo=standup_handoff.QPixmap())
    view.grab()
    assert stage.tip_at(stage.row_rect().center()) == ""       # whole: nothing to add
    view.show_progress(Progress(Step.SUBMITTED))
    _settled(stage)
    view.grab()
    # The check stands for what the words no longer say.
    assert stage.tip_at(stage._zoomed(stage._badge).center()) == words.SUBMITTED_DETAIL


def test_the_card_cuts_its_words_as_the_sheets_card_does(app):
    from PySide6.QtGui import QFontMetrics
    metrics = QFontMetrics(app.font())
    text = "Ein Jahr mit Lightning im Café in Leipzig und anderswo"
    cut = standup_handoff.cut_after_a_word(metrics, text, 120, 1)
    assert cut.endswith("…") and not cut.endswith(" …") and metrics.horizontalAdvance(cut) <= 120
    assert text.startswith(cut[:-1])
    # A long next word would leave the line mostly empty: as much of it as fits.
    name = "Zeitschrift für Wirtschaftsinformatik und Kryptowährungsforschung"
    width = metrics.horizontalAdvance("Zeitschrift für Wirtschaftsinfor")
    cut = standup_handoff.cut_after_a_word(metrics, name, width, 1)
    assert cut.startswith("Zeitschrift für Wirtsch") and cut.endswith("…")
    assert metrics.horizontalAdvance(cut) <= width


def test_an_account_whose_key_is_kept_here_is_never_told_to_wait_for_a_signer_app(german, view):
    # The first account most people have is made in MyEditor, its key kept
    # on this computer: it was told "Warten auf deine Signer-App.
    # Bestätige dort die Anfrage." while its request was signed here.
    assert phase_words(Progress(Step.SIGNING), PUBLICATION, signer_app=False) == (
        "Wird an Einundzwanzig Magazin gesendet …", "")
    assert phase_words(Progress(Step.RECORDING), PUBLICATION, signer_app=False) == (
        "Wird an Einundzwanzig Magazin gesendet …", "")
    signing = Progress(Step.SUBMITTED, message=MessageStatus.SIGNING)
    assert standup_handoff.message_words(signing, 0.0, signer_app=False) == (
        "Deine Nachricht wird gesendet …", "")
    assert standup_handoff.message_words(signing, 0.0) == (
        "Warten auf deine Signer-App: Bestätige dort deine Nachricht.", "")
    view.set_work(title="T", cover=standup_handoff.QPixmap(), publication=PUBLICATION,
                  logo=standup_handoff.QPixmap(), signer_app=False)
    view.show_progress(Progress(Step.SIGNING))
    assert view.headline.text() == "Wird an Einundzwanzig Magazin gesendet …"
    assert view.detail.isHidden() and not view.stage.animating("breath")
