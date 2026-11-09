# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one limit helper (nostr/signer_limits.py): what a signer app can
sign and answer (contract 12.5, feat/redaktion d5cc9a7), said in words
before anything is asked, with the key kept here offered when there is
one. The draft side of the same helper is pinned in
test_publisher_draft.py, the measure against nak in
test_bunker_request_size.py.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from nostr import crypto, signer_limits
from nostr.publisher import PublishJob
from tests.outbox_fakes import (
    PK, FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool, Profile, settle,
)

# 65,143 bytes of content: the signed event's answer is exactly 65,535 bytes.
LARGEST = 65_143
TOO_LONG = "This is too long to be signed through your signer app."
OFFER = "Your key is also kept on this computer: signed with it here, this limit doesn't apply."


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication(sys.argv)


def note(size: int, content: str = "x") -> dict:
    return {"kind": 1, "pubkey": PK, "content": content * size, "tags": [], "created_at": 1}


REMOTE = SimpleNamespace(user_pubkey=PK, signer="remote")
LOCAL = SimpleNamespace(user_pubkey=PK, signer="local")


def contract_answer(event: dict) -> int:
    """Contract 12.5's formula, as JSON.stringify writes it."""
    signed = {"kind": event["kind"], "content": event["content"], "tags": event["tags"],
              "created_at": event["created_at"], "id": "0" * 64, "pubkey": "0" * 64,
              "sig": "0" * 128}
    inner = json.dumps(signed, ensure_ascii=False, separators=(",", ":"))
    return len(json.dumps({"id": "0" * 16, "result": inner}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


# -- the measure -------------------------------------------------------------------

def test_the_answer_is_measured_as_the_contract_computes_it():
    for event in (note(10), note(LARGEST), note(100, 'ä"\\\n<&')):
        assert signer_limits.sign_answer_size(event) == contract_answer(event)


def test_backspace_and_form_feed_are_measured_as_nak_writes_them():
    # json-iterator and easyjson write them as \u0008 and \u000c: four
    # bytes more each, twice escaped or once.
    event = note(10, "\b")
    assert signer_limits.sign_answer_size(event) == contract_answer(event) + 10 * 4


def test_the_largest_event_a_signer_app_can_answer():
    assert signer_limits.sign_answer_size(note(LARGEST)) == crypto.NIP44_MAX_PLAINTEXT_LEN
    assert signer_limits.signable(REMOTE, note(LARGEST))
    assert not signer_limits.signable(REMOTE, note(LARGEST + 1))
    # With the key here nothing is answered.
    assert signer_limits.signable(LOCAL, note(200_000))


# -- the words ---------------------------------------------------------------------

def test_an_event_too_long_for_the_signer_app_is_said_plainly():
    asked = []

    def key_here():
        asked.append(True)
        return False

    words = signer_limits.size_problem(REMOTE, event=note(LARGEST + 1), key_here=key_here)
    assert words.startswith(TOO_LONG) and OFFER not in words
    assert signer_limits.size_problem(REMOTE, event=note(LARGEST), key_here=key_here) is None
    assert len(asked) == 1                    # only asked when there is a problem


def test_the_key_kept_here_is_offered():
    words = signer_limits.size_problem(REMOTE, event=note(LARGEST + 1),
                                       key_here=lambda: True)
    assert words == TOO_LONG + " Shorten it, or split it into parts. " + OFFER


def test_with_the_key_here_there_is_nothing_to_say():
    assert signer_limits.size_problem(LOCAL, event=note(200_000),
                                      key_here=lambda: True) is None


def test_the_words_are_german_too():
    import i18n
    i18n.install("de")
    try:
        words = signer_limits.size_problem(REMOTE, event=note(LARGEST + 1),
                                           key_here=lambda: True)
    finally:
        i18n.install("en")
    assert words == ("Das ist zu lang, um über deine Signer-App signiert zu werden. Kürze "
                     "es oder teile es in Teile auf. Dein Schlüssel liegt auch auf diesem "
                     "Computer: Mit ihm hier signiert, gilt diese Grenze nicht.")


# -- publishing --------------------------------------------------------------------

class CountingPool(FakeSessionPool):
    def __init__(self, client, *, key_here=False):
        super().__init__(client)
        self.asked = 0
        self.kept = key_here

    def get(self, profile, on_ready, on_error):
        self.asked += 1
        super().get(profile, on_ready, on_error)

    def key_here(self, pubkey):
        return self.kept


def publish(event, *, profile=None, key_here=False):
    client = FakeClient()
    pool = CountingPool(client, key_here=key_here)
    job = PublishJob(relay_pool=FakePool(), relay_directory=FakeRelayDirectory(),
                     session_pool=pool, profile=profile or Profile(), unsigned_event=event)
    failed = []
    job.failed.connect(failed.append)
    job.start()
    assert failed == []                       # never inside the call
    settle()
    return failed, pool, client


def test_a_note_too_long_for_the_signer_app_fails_before_the_app_is_asked():
    failed, pool, client = publish(note(LARGEST + 1), key_here=True)
    assert failed == [TOO_LONG + " Shorten it, or split it into parts. " + OFFER]
    assert pool.asked == 0 and client.requests == []


def test_a_note_that_fits_is_signed():
    failed, pool, client = publish(note(LARGEST))
    assert failed == [] and pool.asked == 1 and len(client.requests) == 1


def test_with_the_key_here_a_long_note_is_signed():
    profile = Profile()
    profile.signer = "local"
    failed, pool, client = publish(note(LARGEST + 1), profile=profile)
    assert failed == [] and len(client.requests) == 1
