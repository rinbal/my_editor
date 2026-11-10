# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The handoff in motion (nostr/ui/standup_handoff.py, plan 10.3).

Pinned here: each state's words, the plan's German exactly; that the
card settles into the publication's inbox only in the DONE phase (which
only a matching receipt reaches, test_standup_submitting.py), holds at
60 percent while the request is on its way or unconfirmed, and glides
back when nothing was sent; that with Reduce Motion nothing travels and
settling is a 120 ms cross-fade; that every state is announced once;
and the buttons each state has."""

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
    view.set_work(title="Ein Jahr mit Lightning im Café", summary="Wie ein Café …",
                  byline="Jana Roth · Artikel · 1 Bild", cover=standup_handoff.QPixmap(),
                  publication=PUBLICATION, logo=standup_handoff.QPixmap())
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
    (Progress(Step.RECORDING), ("Warte auf deine Signer-App.", "Bestätige dort die Anfrage.")),
    (Progress(Step.SIGNING), ("Warte auf deine Signer-App.", "Bestätige dort die Anfrage.")),
    (Progress(Step.SENDING), ("Wird an Einundzwanzig Magazin gesendet …", "")),
    (Progress(Step.CHECKING), ("Noch nicht bestätigt. Wird geprüft …",
                               "Du kannst das Fenster schließen, MyEditor prüft weiter.")),
    (Progress(Step.SUBMITTED), ("Eingereicht. Im Eingang von Einundzwanzig Magazin.",
                                "Der Server hat den Empfang bestätigt.")),
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


@pytest.mark.parametrize("step", [s for s in Step if s is not Step.SUBMITTED])
def test_the_card_settles_only_once_it_is_in(view, moving, step):
    view.show_progress(Progress(step, failure=NOT_SENT if step is Step.FAILED else None))
    assert not view.track.animating("settle") and view.track.value("settle") == 0
    assert not view.track._got


def test_a_receipt_settles_the_card_into_the_inbox(view, moving):
    view.show_progress(Progress(Step.SENDING))
    view.show_progress(Progress(Step.SUBMITTED))
    assert view.track.animating("settle") and view.track.animating("fade")
    assert view.track.target("settle") == 1.0 and view.track.target("travel") == 1.0
    assert view.track._got


def test_on_its_way_the_card_holds_at_sixty_percent_and_glides_back_when_not_sent(view, moving):
    view.show_progress(Progress(Step.SIGNING))
    assert view.track.target("lift") == 1.0
    view.show_progress(Progress(Step.SENDING))
    assert view.track.target("travel") == pytest.approx(0.6)
    assert view.track.animating("glow")                  # it holds, breathing
    view.show_progress(Progress(Step.CHECKING))
    assert view.track.target("travel") == pytest.approx(0.6)
    view.show_progress(Progress(Step.FAILED, failure=NOT_SENT))
    assert view.track.target("travel") == 0.0 and view.track.target("lift") == 0.0
    assert not view.track.animating("glow")


def test_with_reduce_motion_nothing_travels_and_settling_cross_fades(view, still):
    for step in (Step.ENCRYPTING, Step.SIGNING, Step.SENDING, Step.CHECKING):
        view.show_progress(Progress(step))
        assert view.track.value("travel") == 0 and view.track.value("lift") == 0
        assert not view.track.animating("travel") and not view.track.animating("glow")
    view.show_progress(Progress(Step.SUBMITTED))
    assert view.track.value("travel") == 0 and view.track.value("settle") == 0
    fade = view.track._animations["fade"]
    assert fade.duration() == motion.CROSS_FADE_MS and fade.endValue() == 0.0


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
    assert said == ["Uploading picture … Encrypted, to your media server.",
                    "Waiting for your signer app. Confirm the request there.",
                    "Submitted. In the inbox of Einundzwanzig Magazin. "
                    "The server confirmed receipt."]


def test_the_bar_shows_the_upload_and_stays_full_after_it(view):
    view.show_progress(Progress(Step.UPLOADING, pictures=1, uploaded=0.4))
    assert view.track._bar == 0.4
    view.show_progress(Progress(Step.SENDING, pictures=1))
    assert view.track._bar == 1.0
    view.show_progress(Progress(Step.SENDING, pictures=0))
    assert view.track._bar is None


def _shown(view):
    return {name for name in ("background", "cancel", "close", "retry", "done")
            if not getattr(view, f"{name}_button").isHidden()}


@pytest.mark.parametrize("progress, shown, enabled_cancel, default", [
    (Progress(Step.SIGNING), {"background", "cancel"}, True, None),
    (Progress(Step.SENDING), {"background", "cancel"}, False, None),
    (Progress(Step.CHECKING), {"close"}, None, None),
    (Progress(Step.FAILED, failure=NOT_SENT), {"close", "retry"}, None, "retry"),
    (Progress(Step.FAILED, failure=Failure(words.ANOTHER_VERSION, None,
                                           title=words.VERSION_NOT_SUBMITTED)),
     {"close"}, None, None),
    (Progress(Step.SUBMITTED), {"done"}, None, "done"),
])
def test_each_state_has_its_buttons(view, progress, shown, enabled_cancel, default):
    view.show_progress(progress)
    assert _shown(view) == shown
    if enabled_cancel is not None:
        assert view.cancel_button.isEnabled() is enabled_cancel
    defaults = {n for n in ("cancel", "close", "retry", "done")
                if getattr(view, f"{n}_button").isDefault()}
    assert defaults == ({default} if default else set())


def test_a_message_that_was_not_sent_can_be_sent_again_from_the_handoff(view):
    asked = []
    view.retry_message_requested.connect(lambda: asked.append(True))
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.SIGNING))
    assert view.send_again.isHidden() and view.message.text() == words.MESSAGE_SIGNING
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.SENDING))
    assert view.send_again.isHidden() and view.message.text() == words.MESSAGE_SENDING
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.FAILED))
    assert not view.send_again.isHidden()
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
    view.show_progress(Progress(Step.FAILED, failure=_limited(_at(14, 29, 30))))
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
    assert view.detail.text() == (f"You’ve sent {PUBLICATION} as much as it takes for now: "
                                  "try again later.")
    assert not view._wait.isActive()


def test_a_wait_past_midnight_says_tomorrow(german, view, monkeypatch):
    _clock(monkeypatch, _at(23, 50))
    view.show_progress(Progress(Step.FAILED, failure=_limited(_at(0, 5, days=1))))
    assert view.headline.text() == "Dein Artikel wurde nicht eingereicht"
    assert view.detail.text() == ("Du hast Einundzwanzig Magazin vorerst so viel gesendet, wie "
                                  "es annimmt: Versuche es morgen um 00:05 erneut.")


def test_send_again_waits_for_the_limit_of_messages(view, monkeypatch):
    now = _clock(monkeypatch, _at(9, 0))
    asked = []
    view.retry_message_requested.connect(lambda: asked.append(True))
    view.show_progress(Progress(Step.SUBMITTED, message=MessageStatus.FAILED,
                                message_retry_at=_at(9, 1)))
    assert not view.send_again.isHidden() and not view.send_again.isEnabled()
    assert view.message.text().startswith(words.MESSAGE_NOT_SENT + " You can send it again at ")
    view.send_again.click()
    assert asked == []
    now["at"] = _at(9, 1)
    view._wait.timeout.emit()
    assert view.send_again.isEnabled() and view.message.text() == words.MESSAGE_NOT_SENT
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
