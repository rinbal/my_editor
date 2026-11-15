# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one limit helper (nostr/signer_limits.py): what a signer app can
sign and answer (contract 12.5, feat/redaktion 9c3447a), said in words
before anything is asked, with the key kept here offered when there is
one. The draft side of the same helper is pinned in
test_publisher_draft.py, the measure against nak in
test_bunker_request_size.py.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from nostr import answer_size, signer_limits
from nostr.publisher import PublishJob
from tests.standup_vectors import VECTORS
from tests.outbox_fakes import (
    PK, FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool, Profile, settle,
)

# 65,063 bytes of content: the signed event's answer is exactly 65,471
# bytes, the most a signer app's answer may hold (contract 12.5).
LARGEST = 65_063
TOO_LONG = ("This is too long to be signed through your signer app: shorten it or split it "
            "into parts.")
OFFERED = ("This is too long to be signed through your signer app: shorten it, or sign it "
           "with your key kept on this computer.")
ANSWER = VECTORS["signer_answer"]


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication(sys.argv)


def note(size: int, content: str = "x") -> dict:
    return {"kind": 1, "pubkey": PK, "content": content * size, "tags": [], "created_at": 1}


REMOTE = SimpleNamespace(user_pubkey=PK, signer="remote")
LOCAL = SimpleNamespace(user_pubkey=PK, signer="local")


# -- the measure (contract 12.5 "Answer size", vector signer_answer) ---------------

def test_the_escaper_is_the_contracts_worst_case():
    assert answer_size.escaped(ANSWER["sign_event"]["event"]["content"]) == \
        ANSWER["escaped_content"]
    assert answer_size.escaped('a"b\\c\n') == '"a\\"b\\\\c\\n"'
    assert answer_size.escaped("<&>\u2028\u2029\b\f\x01") == (
        '"\\u003c\\u0026\\u003e\\u2028\\u2029\\u0008\\u000c\\u0001"')
    assert answer_size.escaped("ä ❤️") == '"ä ❤️"'
    assert answer_size.REQUEST_ID == ANSWER["request_id"]
    assert answer_size.SIGNER_ANSWER_MAX == ANSWER["max_answer_bytes"] == 65_471


def test_the_vector_answers_are_measured_exactly():
    assert signer_limits.sign_answer_size(ANSWER["sign_event"]["event"]) == \
        ANSWER["sign_event"]["answer_bytes"]
    assert signer_limits.decrypt_answer_size(ANSWER["nip44_decrypt"]["plaintext"]) == \
        ANSWER["nip44_decrypt"]["answer_bytes"]
    # JSON.stringify, which keeps <, > and & raw, would say less.
    assert ANSWER["sign_event"]["json_stringify_bytes"] < ANSWER["sign_event"]["answer_bytes"]


def test_the_largest_event_a_signer_app_can_answer():
    assert signer_limits.sign_answer_size(note(LARGEST)) == answer_size.SIGNER_ANSWER_MAX
    assert signer_limits.signable(REMOTE, note(LARGEST))
    assert not signer_limits.signable(REMOTE, note(LARGEST + 1))
    # A < is six bytes in the answer, escaped twice: seven.
    assert not signer_limits.signable(REMOTE, note(LARGEST // 6, "<"))
    # With the key here nothing is answered.
    assert signer_limits.signable(LOCAL, note(200_000))


# -- the words ---------------------------------------------------------------------

def test_an_event_too_long_for_the_signer_app_is_said_plainly():
    asked = []

    def key_here():
        asked.append(True)
        return False

    words = signer_limits.size_problem(REMOTE, event=note(LARGEST + 1), key_here=key_here)
    assert words == TOO_LONG
    assert signer_limits.size_problem(REMOTE, event=note(LARGEST), key_here=key_here) is None
    assert len(asked) == 1                    # only asked when there is a problem


def test_the_key_kept_here_is_offered():
    words = signer_limits.size_problem(REMOTE, event=note(LARGEST + 1),
                                       key_here=lambda: True)
    assert words == OFFERED


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
    assert words == ("Das ist zu lang, um über deine Signer-App signiert zu werden: Kürze "
                     "es, oder signiere es mit deinem Schlüssel auf diesem Computer.")


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
    assert failed == [OFFERED]
    assert pool.asked == 0 and client.requests == []


@pytest.mark.parametrize("client, words", [
    (FakeClient(fail="timed out waiting for signer"),
     "Your signer app didn’t answer: open it and try again."),
    (FakeClient(fail="user rejected"),
     "Your signer app declined the request: allow it there and try again."),
    (FakeClient(tamper=lambda event: dict(event, content="something else")),
     "Your signer app signed something other than what was asked: nothing was sent, so try "
     "again."),
])
def test_a_publish_the_signer_did_not_do_holds_one_plain_sentence(client, words):
    """Lessons 36 and 26: the reason is for Details; what is shown is one
    sentence with its cause and what to do."""
    job = PublishJob(relay_pool=FakePool(), relay_directory=FakeRelayDirectory(),
                     session_pool=FakeSessionPool(client), profile=Profile(),
                     unsigned_event=note(10))
    failed = []
    job.failed.connect(failed.append)
    job.start()
    settle()
    assert len(failed) == 1 and job.failure_words == words
    assert failed[0] and failed[0] not in words


def test_a_note_that_fits_is_signed():
    failed, pool, client = publish(note(LARGEST))
    assert failed == [] and pool.asked == 1 and len(client.requests) == 1


def test_with_the_key_here_a_long_note_is_signed():
    profile = Profile()
    profile.signer = "local"
    failed, pool, client = publish(note(LARGEST + 1), profile=profile)
    assert failed == [] and len(client.requests) == 1
