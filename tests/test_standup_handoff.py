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
from nostr.standup import words
from nostr.standup.submitting import Failure, MessageStatus, Phase, Progress, Step
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


NOT_SENT = Failure(words.NOT_SENT, Step.SIGNING)


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
     ("Nicht gesendet. Keine Verbindung zum Server.",
      "Nichts ist eingereicht, dein Text bleibt im Editor.")),
])
def test_each_state_says_the_plans_words(german, progress, expected):
    assert phase_words(progress, PUBLICATION) == expected


def test_a_list_that_could_not_be_read_says_it_in_its_own_words(german):
    failure = Failure(words.LIST_NOT_SAVED, Step.RECORDING, said="Deine Liste …")
    assert phase_words(Progress(Step.FAILED, failure=failure), PUBLICATION) == (
        "Deine Liste der Einreichungen konnte nicht gespeichert werden.", "Deine Liste …")


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
    return {name for name in ("cancel", "close", "retry", "done")
            if not getattr(view, f"{name}_button").isHidden()}


@pytest.mark.parametrize("progress, shown, enabled_cancel, default", [
    (Progress(Step.SIGNING), {"cancel"}, True, None),
    (Progress(Step.SENDING), {"cancel"}, False, None),
    (Progress(Step.CHECKING), {"close"}, None, None),
    (Progress(Step.FAILED, failure=NOT_SENT), {"close", "retry"}, None, "retry"),
    (Progress(Step.FAILED, failure=Failure(words.ANOTHER_VERSION, None)), {"close"}, None,
     None),
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
