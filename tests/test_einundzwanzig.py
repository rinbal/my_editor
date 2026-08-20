# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins membership resolution and the benefits it unlocks.

The roster is a third-party document fetched over the network, so the
interesting cases are all the ways it can be wrong: absent, truncated,
malformed, empty at a year boundary, or enormous. Every one of them must
end at "not a member" rather than at an exception or a hang.

The direction of the failure matters. A member who briefly loses a
benefit sees nothing worse than a benefit that is not there yet. A
non-member handed a members-only relay writes events that the relay
rejects, which looks like the app is broken and cannot be acted on.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QCoreApplication, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from nostr.einundzwanzig import (
    JOIN_URL,
    MAX_FILE_BYTES,
    MEMBER_BLOSSOM,
    MEMBER_RELAY,
    NO_BENEFITS,
    PER_USER_BYTES,
    Benefits,
    MembershipDirectory,
    benefits_for,
    parse_roster,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


MEMBER = "a" * 64
OTHER = "b" * 64


def roster_bytes(*pubkeys, extra=()):
    records = [
        {"id": i, "npub": f"npub1{i}", "pubkey": pk, "nip05_handle": f"u{i}"}
        for i, pk in enumerate(pubkeys)
    ]
    records.extend(extra)
    return json.dumps(records).encode("utf-8")


# --------------------------------------------------------------------- #
# Fake network                                                          #
# --------------------------------------------------------------------- #

class FakeReply(QObject):
    finished = Signal()
    downloadProgress = Signal(int, int)

    def __init__(self, body=b"", error=QNetworkReply.NoError):
        super().__init__()
        self._body = body
        self._error = error
        self.aborted = False

    def error(self):
        return self._error

    def readAll(self):
        return QByteArray(self._body)

    def abort(self):
        self.aborted = True

    def deleteLater(self):
        pass


class FakeNam(QObject):
    """Serves a scripted response per requested year."""

    def __init__(self, by_year=None, error=None):
        super().__init__()
        self.by_year = by_year or {}
        self.error = error
        self.requested = []
        self.replies = []
        self.last_request = None

    def get(self, request: QNetworkRequest):
        url = request.url().toString()
        self.last_request = request
        year = int(url.rstrip("/").rsplit("/", 1)[-1])
        self.requested.append(year)
        if self.error is not None:
            reply = FakeReply(b"", self.error)
        else:
            reply = FakeReply(self.by_year.get(year, b"[]"))
        self.replies.append(reply)
        return reply


def _directory(nam, *, now=1000.0, year=2026):
    clock = {"t": now}
    d = MembershipDirectory(nam=nam, clock=lambda: clock["t"])
    d._current_year = lambda: year
    return d, clock


def _resolve(directory, pubkey, *, year=None):
    """Resolve and settle every queued reply, returning the emitted answers."""
    seen = []
    directory.resolved.connect(lambda pk, ok: seen.append((pk, ok)))
    directory.resolve(pubkey, year)
    # Fire replies until the directory stops making requests.
    fired = 0
    while fired < len(directory._nam.replies):
        directory._nam.replies[fired].finished.emit()
        fired += 1
    return seen


# --------------------------------------------------------------------- #
# Roster parsing                                                        #
# --------------------------------------------------------------------- #

def test_parses_the_shape_the_association_publishes():
    assert parse_roster(roster_bytes(MEMBER, OTHER)) == {MEMBER, OTHER}


def test_pubkeys_are_lowercased():
    assert parse_roster(roster_bytes("A" * 64)) == {"a" * 64}


def test_one_bad_record_does_not_cost_everyone_their_benefits():
    body = roster_bytes(MEMBER, extra=[
        {"pubkey": "not-hex"},
        {"pubkey": 12345},
        {"no_pubkey_at_all": True},
        "a bare string",
        None,
    ])
    assert parse_roster(body) == {MEMBER}


@pytest.mark.parametrize("body", [
    b"", b"null", b"{}", b'{"members": []}', b"[", b"not json at all",
    b"\xff\xfe\x00", json.dumps({"pubkey": MEMBER}).encode(),
])
def test_malformed_rosters_yield_nobody(body):
    assert parse_roster(body) == set()


def test_a_truncated_roster_is_not_partially_trusted():
    # A cut-off document is invalid JSON, so it must yield nothing rather
    # than the members that happened to arrive before the cut.
    full = roster_bytes(MEMBER, OTHER)
    assert parse_roster(full[: len(full) // 2]) == set()


# --------------------------------------------------------------------- #
# Benefits                                                              #
# --------------------------------------------------------------------- #

def test_a_member_gets_the_relay_and_the_media_server():
    b = benefits_for(True)
    assert b.is_member
    assert b.relay == MEMBER_RELAY
    assert b.blossom_server == MEMBER_BLOSSOM
    assert b.max_file_bytes == MAX_FILE_BYTES
    assert b.per_user_bytes == PER_USER_BYTES


def test_a_non_member_gets_nothing_to_branch_on():
    b = benefits_for(False)
    assert not b.is_member
    assert b.relay == "" and b.blossom_server == ""
    assert b == NO_BENEFITS


def test_benefits_are_immutable():
    with pytest.raises(Exception):
        benefits_for(True).relay = "wss://elsewhere.example"


def test_the_relay_is_not_the_groups_relay():
    # The NIP-29 groups relay does not answer for an author's ordinary
    # notes, so naming it in a relay list would point readers nowhere.
    assert "group.einundzwanzig.space" not in MEMBER_RELAY
    assert MEMBER_RELAY.startswith("wss://")
    assert MEMBER_BLOSSOM.startswith("https://")
    assert JOIN_URL.startswith("https://")


# --------------------------------------------------------------------- #
# Resolving                                                             #
# --------------------------------------------------------------------- #

def test_a_member_resolves_true():
    nam = FakeNam({2026: roster_bytes(MEMBER, OTHER)})
    d, _ = _directory(nam)
    assert _resolve(d, MEMBER) == [(MEMBER, True)]


def test_a_stranger_resolves_false():
    nam = FakeNam({2026: roster_bytes(OTHER)})
    d, _ = _directory(nam)
    assert _resolve(d, MEMBER) == [(MEMBER, False)]


def test_a_network_failure_resolves_false_rather_than_raising():
    nam = FakeNam(error=QNetworkReply.HostNotFoundError)
    d, _ = _directory(nam)
    assert _resolve(d, MEMBER) == [(MEMBER, False)]
    # A refused request is not a year-boundary problem, so no fallback.
    assert nam.requested == [2026]


def test_a_malformed_pubkey_never_reaches_the_network():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    assert _resolve(d, "nonsense") == [("nonsense", False)]
    assert nam.requested == []


def test_an_empty_pubkey_never_reaches_the_network():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    assert _resolve(d, "") == [("", False)]
    assert nam.requested == []


# --------------------------------------------------------------------- #
# The year boundary                                                     #
# --------------------------------------------------------------------- #

def test_an_empty_new_year_falls_back_to_last_year():
    # In January the new year's roster can exist but be empty. Last
    # year's members have not stopped being members.
    nam = FakeNam({2026: b"[]", 2025: roster_bytes(MEMBER)})
    d, _ = _directory(nam, year=2026)
    assert _resolve(d, MEMBER) == [(MEMBER, True)]
    assert nam.requested == [2026, 2025]


def test_the_fallback_is_tried_once_and_never_loops():
    nam = FakeNam({})  # every year answers empty
    d, _ = _directory(nam, year=2026)
    assert _resolve(d, MEMBER) == [(MEMBER, False)]
    assert nam.requested == [2026, 2025]


def test_a_populated_current_year_does_not_ask_for_last_year():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam, year=2026)
    _resolve(d, MEMBER)
    assert nam.requested == [2026]


# --------------------------------------------------------------------- #
# Caching                                                               #
# --------------------------------------------------------------------- #

def test_a_fresh_roster_is_reused_without_refetching():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    _resolve(d, MEMBER)
    assert _resolve(d, MEMBER) == [(MEMBER, True)]
    assert nam.requested == [2026]


def test_a_second_account_is_answered_from_the_same_roster():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    _resolve(d, MEMBER)
    assert _resolve(d, OTHER) == [(OTHER, False)]
    assert nam.requested == [2026]


def test_a_stale_roster_is_refetched():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, clock = _directory(nam)
    _resolve(d, MEMBER)
    clock["t"] += 16 * 60
    _resolve(d, MEMBER)
    assert nam.requested == [2026, 2026]


def test_cached_membership_is_unknown_before_the_roster_arrives():
    # Unknown must not read as "not a member", or a member sees a join
    # prompt flash at them while the roster loads.
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    assert d.cached_membership(MEMBER) is None
    assert d.cached_benefits(MEMBER) is None
    _resolve(d, MEMBER)
    assert d.cached_membership(MEMBER) is True
    assert d.cached_benefits(MEMBER).relay == MEMBER_RELAY


def test_invalidate_forces_a_refetch():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    _resolve(d, MEMBER)
    d.invalidate()
    assert d.cached_membership(MEMBER) is None
    _resolve(d, MEMBER)
    assert nam.requested == [2026, 2026]


def test_concurrent_resolves_share_one_request():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    seen = []
    d.resolved.connect(lambda pk, ok: seen.append((pk, ok)))
    d.resolve(MEMBER)
    d.resolve(OTHER)
    nam.replies[0].finished.emit()
    assert nam.requested == [2026]
    assert sorted(seen) == [(MEMBER, True), (OTHER, False)]


# --------------------------------------------------------------------- #
# Request hygiene                                                       #
# --------------------------------------------------------------------- #

def test_the_request_cannot_hang_or_wander():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    _resolve(d, MEMBER)
    req = nam.last_request
    assert req.transferTimeout() > 0
    assert req.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute) == (
        QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy
    )


def test_our_pubkey_is_never_sent_to_the_association():
    # The whole roster is matched locally, so the host learns that the app
    # was opened but not by whom. Worth keeping if the API ever changes.
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    _resolve(d, MEMBER)
    assert MEMBER not in nam.last_request.url().toString()


def test_an_oversized_roster_is_aborted_and_resolves_false():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    d, _ = _directory(nam)
    seen = []
    d.resolved.connect(lambda pk, ok: seen.append((pk, ok)))
    d.resolve(MEMBER)
    reply = nam.replies[0]
    reply.downloadProgress.emit(8 * 1024 * 1024, 8 * 1024 * 1024)
    assert reply.aborted
    reply.finished.emit()
    assert seen == [(MEMBER, False)]
    # An aborted fetch says nothing about which year is current, so it must
    # not spend a second request retrying last year.
    assert nam.requested == [2026]


# --------------------------------------------------------------------- #
# The entitled relay reaching the publish targets                       #
# --------------------------------------------------------------------- #

from nostr.outbox import (
    RELAY_CAP, RelayList, select_draft_publish_relays, select_publish_relays,
)


def test_a_members_relay_leads_the_publish_targets():
    out = select_publish_relays(
        ["wss://mine.example"],
        base=["wss://base.example"],
        entitled=[MEMBER_RELAY],
    )
    assert out[0] == MEMBER_RELAY
    assert "wss://mine.example" in out and "wss://base.example" in out


def test_a_non_member_publishes_exactly_as_before():
    # No entitlement must mean no change at all to the existing behaviour.
    args = (["wss://mine.example"],)
    kwargs = dict(base=["wss://base.example"])
    assert select_publish_relays(*args, **kwargs) == select_publish_relays(
        *args, entitled=[], **kwargs
    )


def test_the_cap_cannot_drop_the_entitled_relay():
    # Being silently trimmed would cost a member the benefit they paid for.
    crowded = [f"wss://r{i}.example" for i in range(RELAY_CAP * 2)]
    out = select_publish_relays(crowded, base=crowded, entitled=[MEMBER_RELAY])
    assert out[0] == MEMBER_RELAY
    assert len(out) == RELAY_CAP


def test_an_entitled_relay_already_configured_is_not_duplicated():
    out = select_publish_relays(
        [MEMBER_RELAY + "/"], base=[], entitled=[MEMBER_RELAY],
    )
    assert out == [MEMBER_RELAY]


def test_drafts_reach_the_members_relay_but_after_the_users_own():
    out = select_draft_publish_relays(
        RelayList(write=["wss://mine.example"], read=[]),
        base=["wss://base.example"],
        entitled=[MEMBER_RELAY],
    )
    assert out.index("wss://mine.example") < out.index(MEMBER_RELAY)
    assert out.index(MEMBER_RELAY) < out.index("wss://base.example")


def test_drafts_are_unchanged_without_an_entitlement():
    rl = RelayList(write=["wss://mine.example"], read=[])
    assert select_draft_publish_relays(rl, base=["wss://b.example"]) == (
        select_draft_publish_relays(rl, base=["wss://b.example"], entitled=[])
    )
