# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private records are read where they are written.

The contract: a draft saved from device A must be found by device B on
the same account. The old code chose the publish set and the read set
with two different functions that only mostly agreed. Now one function
(policy.private_relays, asked through the relay directory) answers both:
reading asks every relay writing goes to, plus the fallback relays that
records saved while the account's list was unknown went to. These tests
pin the set itself and that every writer and the reader really use it.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from nostr.draft_store import DraftStore  # noqa: E402
from nostr.draft_sync import DraftSync  # noqa: E402
from nostr.drafts import build_inner_event  # noqa: E402
from nostr.outbox import ask_private_relays, defaults  # noqa: E402
from nostr.outbox.policy import LookupState, RelayList, private_relays  # noqa: E402
from nostr.publisher import DraftDeleteJob, DraftPublishJob  # noqa: E402
from tests.outbox_fakes import FakeRelayDirectory, settle  # noqa: E402

PK = "a" * 64
BUNKER = "wss://bunker.example"
MEMBER_RELAY = "wss://members.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


def own(write=(), read=()):
    return RelayList(write=list(write), read=list(read), state=LookupState.FOUND)


# --------------------------------------------------------------------------- #
# The set                                                                     #
# --------------------------------------------------------------------------- #

def test_asymmetric_read_and_write_relays_are_both_used():
    # Common real-world shape: a paid read-only relay and free write relays.
    relays = private_relays(own(write=["wss://w1.example", "wss://shared.example"],
                                read=["wss://paid-read.example"]))
    assert relays == ["wss://w1.example", "wss://shared.example", "wss://paid-read.example"]


def test_a_list_with_only_read_relays_still_has_a_home():
    assert private_relays(own(read=["wss://only-read.example"])) == ["wss://only-read.example"]


def test_drafts_kept_on_the_signer_relays_stay_readable():
    # Earlier versions stored drafts on the relays a profile was paired
    # through, so they stay in the set, after the account's own.
    relays = private_relays(own(write=["wss://w.example"]), legacy=[BUNKER + "/"])
    assert relays == ["wss://w.example", BUNKER]


def test_an_unknown_list_falls_back_to_the_curated_relays():
    relays = private_relays(RelayList(), legacy=[BUNKER])
    assert relays == [*defaults.FALLBACK_RELAYS, BUNKER]


def test_a_known_list_is_not_padded_with_relays_the_user_never_chose():
    relays = private_relays(own(write=["wss://w.example"]))
    assert not set(relays) & set(defaults.FALLBACK_RELAYS)


def test_a_members_relay_comes_after_the_users_own():
    relays = private_relays(own(write=["wss://w.example"], read=["wss://r.example"]),
                            entitled=[MEMBER_RELAY], legacy=[BUNKER])
    assert relays == ["wss://w.example", "wss://r.example", MEMBER_RELAY, BUNKER]


def test_duplicates_collapse_ignoring_case_and_trailing_slash():
    relays = private_relays(own(write=["wss://X.Example/", "wss://x.example"],
                                read=["WSS://x.example/"]))
    assert relays == ["wss://x.example"]


def test_the_set_is_capped():
    relays = private_relays(own(write=[f"wss://w{i}.example" for i in range(50)]))
    assert len(relays) == defaults.PRIVATE_CAP


# --------------------------------------------------------------------------- #
# Every writer and the reader use it                                          #
# --------------------------------------------------------------------------- #

def _profile():
    profile = MagicMock()
    profile.user_pubkey = PK
    profile.bunker_relays = [BUNKER]
    return profile


def _directory():
    return FakeRelayDirectory({PK: own(write=["wss://w.example"], read=["wss://r.example"])})


def _signer():
    client = MagicMock()
    client.nip44_encrypt_self.side_effect = (
        lambda plaintext, on_success, on_failure: on_success("CIPHERTEXT"))
    client.sign_event.side_effect = (
        lambda unsigned, on_success, on_failure: on_success(
            {**unsigned, "id": "e" * 64, "sig": "s" * 128}))
    session_pool = MagicMock()
    session_pool.get.side_effect = (
        lambda profile, on_ready, on_error: on_ready(client))
    return session_pool


def _published_to(job_class, **kwargs):
    relay_pool = MagicMock()
    job = job_class(relay_pool=relay_pool, relay_directory=_directory(),
                    session_pool=_signer(), profile=_profile(),
                    entitled_relays=[MEMBER_RELAY], **kwargs)
    job.start()
    settle()
    (relays, _event), _kw = relay_pool.publish.call_args
    return relays


def _read_from():
    relay_pool = MagicMock()
    sync = DraftSync(relay_pool=relay_pool, relay_directory=_directory(),
                     session_pool=_signer(), store=DraftStore(),
                     entitled_relays=lambda: [MEMBER_RELAY])
    sync.start_for(_profile())
    settle()
    (relays, _filters), _kw = relay_pool.subscribe.call_args
    return relays


def test_a_saved_draft_lands_where_drafts_are_read():
    written = _published_to(
        DraftPublishJob, identifier="d1",
        inner_event=build_inner_event(kind=1, content="hi", pubkey_hex=PK))
    assert written == ["wss://w.example", "wss://r.example", MEMBER_RELAY, BUNKER]
    assert _read_from() == [*written, *defaults.FALLBACK_RELAYS]


def test_a_deletion_lands_wherever_the_draft_may_be():
    written = _published_to(DraftDeleteJob, identifier="d1", inner_kind=1)
    assert written == _read_from()


# --------------------------------------------------------------------------- #
# Written while the list was unknown, still found once it is known            #
# --------------------------------------------------------------------------- #

def test_a_draft_saved_before_the_list_was_known_is_still_read():
    unknown_then = private_relays(RelayList(), legacy=[BUNKER])
    known_now = private_relays(own(write=["wss://w.example"]), legacy=[BUNKER],
                               reading=True)
    assert set(unknown_then) <= set(known_now)


def test_reading_asks_every_relay_writing_goes_to():
    for author in (RelayList(), own(write=["wss://w.example"], read=["wss://r.example"]),
                   own(write=[f"wss://w{i}.example" for i in range(50)])):
        written = private_relays(author, entitled=[MEMBER_RELAY], legacy=[BUNKER])
        read = private_relays(author, entitled=[MEMBER_RELAY], legacy=[BUNKER], reading=True)
        assert read[:len(written)] == written


def test_a_long_list_never_pushes_out_the_members_or_the_signer_relays():
    relays = private_relays(own(write=[f"wss://w{i}.example" for i in range(50)]),
                            entitled=[MEMBER_RELAY], legacy=[BUNKER])
    assert len(relays) == defaults.PRIVATE_CAP
    assert relays[-2:] == [MEMBER_RELAY, BUNKER]
    assert relays[0] == "wss://w0.example"


def test_entitled_relays_may_be_asked_for_when_needed():
    directory = FakeRelayDirectory({PK: own(write=["wss://w.example"])})
    membership = []
    got = []
    ask_private_relays(directory, _profile(), got.append, entitled=lambda: list(membership))
    membership.append(MEMBER_RELAY)
    ask_private_relays(directory, _profile(), got.append, entitled=lambda: list(membership))
    settle()
    assert MEMBER_RELAY not in got[0] and MEMBER_RELAY in got[1]
