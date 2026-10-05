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
    parse_roster_records,
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
    assert b.per_user_bytes == PER_USER_BYTES
    assert MAX_FILE_BYTES == 1024 ** 3


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

from nostr.outbox import RelayList, defaults, plan_publish, private_relays
from nostr.outbox.policy import LookupState


def _own(*write):
    return RelayList(write=list(write), read=[], state=LookupState.FOUND)


def test_a_members_relay_follows_the_authors_own_relays():
    # The author's write relays are where their readers look, so they
    # lead; the members' relay is added right behind them.
    plan = plan_publish(_own("wss://mine.example", "wss://mine2.example"),
                        entitled=[MEMBER_RELAY])
    assert list(plan.author) == ["wss://mine.example", "wss://mine2.example",
                                 MEMBER_RELAY]


def test_a_non_member_publishes_exactly_as_before():
    # No entitlement must mean no change at all to the existing behaviour.
    own = _own("wss://mine.example", "wss://mine2.example")
    assert plan_publish(own) == plan_publish(own, entitled=[])


def test_the_cap_cannot_drop_the_entitled_relay():
    # Being silently trimmed would cost a member the benefit they paid for.
    crowded = [f"wss://r{i}.example" for i in range(defaults.WRITE_CAP * 3)]
    plan = plan_publish(_own(*crowded), entitled=[MEMBER_RELAY])
    assert MEMBER_RELAY in plan.author


def test_an_entitled_relay_already_configured_is_not_duplicated():
    plan = plan_publish(_own(MEMBER_RELAY + "/", "wss://mine.example"),
                        entitled=[MEMBER_RELAY])
    assert list(plan.author) == [MEMBER_RELAY, "wss://mine.example"]


def test_drafts_reach_the_members_relay_but_after_the_users_own():
    out = private_relays(_own("wss://mine.example"), entitled=[MEMBER_RELAY],
                         legacy=["wss://bunker.example"])
    assert out == ["wss://mine.example", MEMBER_RELAY, "wss://bunker.example"]


def test_drafts_are_unchanged_without_an_entitlement():
    own = _own("wss://mine.example")
    assert private_relays(own) == private_relays(own, entitled=[])


# --------------------------------------------------------------------- #
# Names, confirmations and a steady answer                              #
# --------------------------------------------------------------------- #

def test_the_roster_remembers_each_members_nostr_address():
    nam = FakeNam({2026: roster_bytes(MEMBER, OTHER)})
    directory, _clock = _directory(nam)
    _resolve(directory, MEMBER)
    assert directory.cached_handle(MEMBER) == "u0"
    assert directory.cached_handle(OTHER) == "u1"
    assert directory.cached_handle("f" * 64) is None


def test_a_handle_that_cannot_be_an_address_is_dropped_not_the_member():
    bad = {"pubkey": "c" * 64, "nip05_handle": "Not A Handle!"}
    records = parse_roster_records(roster_bytes(MEMBER, extra=[bad]))
    assert records["c" * 64] is None
    assert records[MEMBER.lower()] == "u0"


def test_a_confirmed_member_is_a_member_before_the_roster_knows():
    nam = FakeNam({2026: roster_bytes(OTHER)})
    directory, _clock = _directory(nam)
    _resolve(directory, MEMBER)
    assert directory.cached_membership(MEMBER) is False
    directory.confirm_member(MEMBER)
    assert directory.cached_membership(MEMBER) is True
    assert directory.cached_benefits(MEMBER).is_member


def test_only_a_real_pubkey_can_be_confirmed():
    directory, _clock = _directory(FakeNam({}))
    directory.confirm_member("not a key")
    assert directory.last_known_membership("not a key") is None


def test_a_stale_roster_keeps_its_last_answer_for_steady_benefits():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    directory, clock = _directory(nam)
    _resolve(directory, MEMBER)
    clock["t"] += 60 * 60          # well past the refresh interval
    assert directory.cached_membership(MEMBER) is None      # not fresh
    assert directory.last_known_membership(MEMBER) is True  # but still known
    assert directory.last_known_membership(OTHER) is False


def test_nothing_known_yet_is_none_not_false():
    directory, _clock = _directory(FakeNam({}))
    assert directory.last_known_membership(MEMBER) is None


# --------------------------------------------------------------------- #
# A refresh that fails, a forced refresh, and what the window saved     #
# --------------------------------------------------------------------- #

class FlakyNam(FakeNam):
    """Answers from ``by_year`` until ``down`` is set, then fails."""

    def __init__(self, by_year):
        super().__init__(by_year)
        self.down = False

    def get(self, request):
        self.error = QNetworkReply.HostNotFoundError if self.down else None
        return super().get(request)


def test_a_failed_refresh_keeps_the_roster_and_the_names():
    nam = FlakyNam({2026: roster_bytes(MEMBER)})
    directory, clock = _directory(nam)
    _resolve(directory, MEMBER)
    clock["t"] += 16 * 60                          # stale: the next resolve fetches
    nam.down = True
    seen = []
    directory.resolved.connect(lambda pk, ok: seen.append((pk, ok)))
    directory.resolve(MEMBER)
    nam.replies[-1].finished.emit()                # the refresh fails
    assert seen == [(MEMBER, True)]                # waiters hear the last known answer
    assert directory.last_known_membership(MEMBER) is True
    assert directory.cached_handle(MEMBER) == "u0"
    assert nam.requested == [2026, 2026]


def test_a_failed_refresh_is_retried_on_the_next_resolve():
    nam = FlakyNam({2026: roster_bytes(MEMBER)})
    directory, clock = _directory(nam)

    def resolve_once():
        directory.resolve(MEMBER)
        nam.replies[-1].finished.emit()

    resolve_once()
    clock["t"] += 16 * 60
    nam.down = True
    resolve_once()
    assert directory.cached_membership(MEMBER) is None   # kept, but still stale
    nam.down = False
    resolve_once()
    assert nam.requested == [2026, 2026, 2026]
    assert directory.cached_membership(MEMBER) is True


def test_a_failure_with_no_roster_is_still_not_a_member():
    nam = FakeNam(error=QNetworkReply.HostNotFoundError)
    directory, _clock = _directory(nam)
    assert _resolve(directory, MEMBER) == [(MEMBER, False)]
    assert directory.last_known_membership(MEMBER) is None


def test_a_forced_refresh_fetches_a_fresh_roster_and_keeps_answering_meanwhile():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    directory, _clock = _directory(nam)
    _resolve(directory, MEMBER)
    nam.by_year[2026] = roster_bytes(OTHER)
    seen = []
    directory.resolved.connect(lambda pk, ok: seen.append((pk, ok)))
    directory.resolve(MEMBER, force=True)
    assert nam.requested == [2026, 2026]
    assert directory.cached_membership(MEMBER) is True       # until the answer lands
    nam.replies[-1].finished.emit()
    assert seen == [(MEMBER, False)]
    assert directory.cached_membership(MEMBER) is False


def test_a_saved_name_is_shown_before_the_roster_lists_it():
    nam = FakeNam({2026: roster_bytes(MEMBER)})
    directory, clock = _directory(nam)
    _resolve(directory, MEMBER)
    directory.record_handle(MEMBER, "satoshi")
    assert directory.cached_handle(MEMBER) == "satoshi"
    clock["t"] += 16 * 60
    _resolve(directory, MEMBER)                    # the roster still says "u0"
    assert directory.cached_handle(MEMBER) == "satoshi"


def test_only_a_real_name_is_saved():
    directory, _clock = _directory(FakeNam({}))
    directory.record_handle(MEMBER, "not a name")
    directory.record_handle("nonsense", "satoshi")
    assert directory.cached_handle(MEMBER) is None
    assert directory.cached_handle("nonsense") is None


def test_forgetting_drops_the_confirmation_and_the_saved_name():
    nam = FakeNam({2026: roster_bytes(OTHER)})
    directory, _clock = _directory(nam)
    _resolve(directory, MEMBER)
    directory.confirm_member(MEMBER)
    directory.record_handle(MEMBER, "satoshi")
    directory.forget(MEMBER)
    assert directory.cached_membership(MEMBER) is False
    assert directory.cached_handle(MEMBER) is None


def test_the_roster_and_the_api_agree_on_what_a_name_is():
    from nostr.einundzwanzig_api import NIP05_HANDLE_MAX_LENGTH
    too_long = "a" * (NIP05_HANDLE_MAX_LENGTH + 1)
    records = parse_roster_records(json.dumps([
        {"pubkey": MEMBER, "nip05_handle": "fine_name-1"},
        {"pubkey": OTHER, "nip05_handle": too_long},
    ]).encode())
    assert records[MEMBER] == "fine_name-1"
    assert records[OTHER] is None
