# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins reading the user's encrypted media library off their relays.

The shapes here are the ones that actually arrive: a healthy record, a
tombstone, the same file written twice, and the four ways a record can
be junk. None of them may raise, and none of them may cost the user the
rest of their library.

The other half of this file is about the keys. Every per-file key that
lands in memory is the whole secret of that file, so the tests assert
what must never happen to one: reaching a status line, reaching a
failure message, or surviving a profile switch.

Nothing here touches a relay, a signer, a network or a real home
directory. The query, the signer session and the clock are all fakes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.media.private_library import (
    PRIVATE_FILE_KIND,
    LibraryFailure,
    PrivateLibrary,
    parse_file_record,
)

from tests.imports_fakes import FakeRelayListCache


PUBKEY = "ab" * 32
OTHER_PUBKEY = "cd" * 32

PROFILE = SimpleNamespace(user_pubkey=PUBKEY, bunker_relays=["wss://bunker.example"])
OTHER_PROFILE = SimpleNamespace(
    user_pubkey=OTHER_PUBKEY, bunker_relays=["wss://bunker.example"])

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
KEY = "1e" * 32
OTHER_KEY = "2f" * 32

NOW = 1_700_000_000


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    # QApplication, not QCoreApplication: once a bare QCoreApplication
    # exists, every other test file's ``QApplication.instance() or
    # QApplication(...)`` finds it, no QApplication is ever built, and
    # the next QWidget aborts the interpreter.
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# --------------------------------------------------------------------- #
# Fakes                                                                 #
# --------------------------------------------------------------------- #

class FakeQuery:
    """The addressable-query seam, answering a scripted event list."""

    def __init__(self, events=None):
        self.events = list(events or [])
        self.calls = []

    def addressable(self, relays, filters, on_done):
        self.calls.append((list(relays), list(filters)))
        on_done(list(self.events))


class ParkedQuery:
    """Query that holds its callback so a test controls when it lands."""

    def __init__(self):
        self.pending = []

    def addressable(self, relays, filters, on_done):
        self.pending.append(on_done)


class FakeBunkerClient:
    """Self-decrypt as string surgery: ``ENC[x]`` opens to ``x``."""

    def __init__(self, fail_on=(), failure="the signer refused"):
        self.calls = []
        self.fail_on = set(fail_on)
        self.failure = failure

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **_kw):
        self.calls.append(ciphertext)
        if ciphertext in self.fail_on:
            on_failure(self.failure)
            return
        if ciphertext.startswith("ENC[") and ciphertext.endswith("]"):
            on_success(ciphertext[4:-1])
        else:
            on_failure("not something this signer wrote")


class ManualBunkerClient:
    """Parks every decrypt, so the signer cadence is the test's choice."""

    def __init__(self):
        self.pending = []
        self.calls = []

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **_kw):
        self.calls.append(ciphertext)
        self.pending.append((ciphertext, on_success, on_failure))

    def settle_next(self):
        ciphertext, on_success, _on_failure = self.pending.pop(0)
        on_success(ciphertext[4:-1])


class FakeSessionPool:
    def __init__(self, client=None, error=None):
        self.client = client if client is not None else FakeBunkerClient()
        self.error = error
        self.calls = 0

    def get(self, profile, on_ready=None, on_error=None):
        self.calls += 1
        if self.error:
            on_error(self.error)
        else:
            on_ready(self.client)


# --------------------------------------------------------------------- #
# Builders                                                              #
# --------------------------------------------------------------------- #

def record(hash_hex=HASH_A, *, key=KEY, **overrides):
    """A file record shaped the way the reference implementation writes it."""
    payload = {
        "name": "beach.jpg",
        "hash": hash_hex,
        "size": 4096,
        "type": "image/jpeg",
        "folder": "/holiday",
        "uploadedAt": 1_699_000_000,
        "server": "https://cdn.example",
        "servers": ["https://cdn.example", "https://mirror.example"],
        "encryptionKey": key,
    }
    if key is None:
        payload.pop("encryptionKey")
    payload.update(overrides)
    return payload


def event(payload, *, d=None, created_at=1000, pubkey=PUBKEY, event_id=None):
    """One kind-34578 event carrying ``payload`` as self-encrypted content."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    identifier = d
    if identifier is None:
        identifier = payload.get("hash", HASH_A) if isinstance(payload, dict) else HASH_A
    return {
        "id": event_id or ("e" * 63 + "0"),
        "kind": PRIVATE_FILE_KIND,
        "pubkey": pubkey,
        "created_at": created_at,
        "tags": [["d", identifier], ["client", "lotus"], ["encrypted", "nip44"]],
        "content": "ENC[" + text + "]",
    }


def make_library(events=None, *, client=None, session_pool=None, query=None,
                 clock=None):
    query = query if query is not None else FakeQuery(events)
    pool = session_pool if session_pool is not None else FakeSessionPool(client)
    library = PrivateLibrary(
        session_pool=pool,
        relay_list_cache=FakeRelayListCache(),
        query=query,
        clock=clock or (lambda: NOW),
    )
    return library, query, pool


def loaded(events, **kw):
    """Bind a profile and settle, returning the library."""
    library, _query, _pool = make_library(events, **kw)
    library.bind_profile(PROFILE)
    return library


# --------------------------------------------------------------------- #
# Parsing one record                                                    #
# --------------------------------------------------------------------- #

def test_a_healthy_record_becomes_a_private_blob():
    parsed = parse_file_record(json.dumps(record()), identifier=HASH_A)

    assert parsed.reason == ""
    assert parsed.tombstone is False
    blob = parsed.blob
    assert blob.sha256 == HASH_A
    assert blob.key_hex == KEY
    assert blob.name == "beach.jpg"
    assert blob.mime == "image/jpeg"
    assert blob.size == 4096
    assert blob.uploaded_at == 1_699_000_000
    # The primary server leads, mirrors follow, and no server is listed
    # twice even though the record names the primary in both fields.
    assert blob.servers == ["https://cdn.example", "https://mirror.example"]


def test_a_deleted_record_is_a_tombstone_not_a_file():
    parsed = parse_file_record(
        json.dumps(record(deleted=True)), identifier=HASH_A)

    assert parsed.tombstone is True
    assert parsed.blob is None
    assert parsed.reason == ""


def test_a_record_with_no_key_is_skipped_rather_than_shown_unopenable():
    parsed = parse_file_record(json.dumps(record(key=None)), identifier=HASH_A)

    assert parsed.blob is None
    assert parsed.tombstone is False
    assert "key" in parsed.reason.lower()


def test_a_key_that_is_not_a_key_is_refused_before_it_is_ever_used():
    parsed = parse_file_record(
        json.dumps(record(key="not-hex-and-far-too-short")), identifier=HASH_A)

    assert parsed.blob is None
    # The offending value must not travel with the complaint.
    assert "not-hex" not in parsed.reason


def test_truncated_json_is_reported_not_raised():
    truncated = json.dumps(record())[:40]

    parsed = parse_file_record(truncated, identifier=HASH_A)

    assert parsed.blob is None
    assert parsed.reason
    # Whatever survived the truncation stays out of the message: the
    # fragment can contain the start of a key.
    assert truncated not in parsed.reason
    assert "encryptionKey" not in parsed.reason


def test_a_payload_that_is_not_a_record_at_all_is_skipped():
    for text in ("[1, 2, 3]", '"just a string"', "null", "17"):
        parsed = parse_file_record(text, identifier=HASH_A)
        assert parsed.blob is None, text
        assert parsed.reason, text


def test_a_hash_that_does_not_look_like_a_hash_is_skipped():
    for bad in ("", "beach.jpg", "zz" * 32, HASH_A[:-1], HASH_A + "aa"):
        parsed = parse_file_record(
            json.dumps(record(hash=bad)), identifier=HASH_A)
        assert parsed.blob is None, bad
        assert parsed.reason, bad


def test_a_record_filed_under_a_different_hash_is_skipped():
    # The d-tag is the content hash. A record claiming a different one is
    # not something to act on: publishing it would upload bytes under a
    # name that does not match them.
    parsed = parse_file_record(json.dumps(record(HASH_B)), identifier=HASH_A)

    assert parsed.blob is None
    assert parsed.reason


def test_an_uppercase_hash_is_accepted_and_normalised():
    parsed = parse_file_record(
        json.dumps(record(HASH_A.upper())), identifier=HASH_A.upper())

    assert parsed.blob is not None
    assert parsed.blob.sha256 == HASH_A


def test_junk_in_the_number_fields_falls_back_to_zero():
    parsed = parse_file_record(
        json.dumps(record(size="huge", uploadedAt=None, servers="not a list")),
        identifier=HASH_A,
    )

    assert parsed.blob.size == 0
    assert parsed.blob.uploaded_at == 0
    assert parsed.blob.servers == ["https://cdn.example"]


def test_a_public_copy_pointer_is_carried_through():
    parsed = parse_file_record(
        json.dumps(record(publicCopy={
            "hash": HASH_C,
            "servers": ["https://cdn.example"],
            "uploadedAt": 1_699_500_000,
        })),
        identifier=HASH_A,
    )

    assert parsed.blob.public_copy_hash == HASH_C


def test_a_junk_public_copy_pointer_is_dropped_not_believed():
    parsed = parse_file_record(
        json.dumps(record(publicCopy={"hash": "nonsense"})), identifier=HASH_A)

    assert parsed.blob is not None
    assert parsed.blob.public_copy_hash == ""


def test_the_thumbnail_key_is_not_carried_into_memory():
    # PrivateBlob has one key field because there is one key we need. A
    # second secret with no home is a second secret to leak.
    parsed = parse_file_record(
        json.dumps(record(thumbHash=HASH_B, thumbKey=OTHER_KEY)),
        identifier=HASH_A,
    )

    assert OTHER_KEY not in repr(parsed.blob)


# --------------------------------------------------------------------- #
# Reading the library                                                   #
# --------------------------------------------------------------------- #

def test_a_healthy_library_lists_its_files():
    library = loaded([event(record(HASH_A)), event(record(HASH_B))])

    assert len(library) == 2
    assert {b.sha256 for b in library.files} == {HASH_A, HASH_B}
    assert library.failures == []


def test_the_query_asks_only_for_this_users_own_library():
    library, query, _pool = make_library([event(record())])
    library.bind_profile(PROFILE)

    _relays, filters = query.calls[0]
    assert filters == [{"kinds": [PRIVATE_FILE_KIND], "authors": [PUBKEY]}]


def test_an_event_from_another_author_is_ignored():
    # A relay can answer with anything. Adopting a foreign record would
    # put someone else's key in this user's library.
    library = loaded([event(record(HASH_A), pubkey=OTHER_PUBKEY)])

    assert len(library) == 0


def test_a_tombstone_removes_the_file_from_the_listing():
    library = loaded([
        event(record(HASH_A), created_at=1000),
        event(record(HASH_A, deleted=True), created_at=2000),
    ])

    assert len(library) == 0
    assert library.get(HASH_A) is None
    # A tombstone is the user's own decision, not a fault to report.
    assert library.failures == []


def test_the_newer_event_for_one_d_tag_replaces_the_older():
    library = loaded([
        event(record(HASH_A, name="old.jpg"), created_at=1000),
        event(record(HASH_A, name="new.jpg"), created_at=2000),
    ])

    assert len(library) == 1
    assert library.get(HASH_A).name == "new.jpg"


def test_an_older_event_arriving_last_still_loses():
    library = loaded([
        event(record(HASH_A, name="new.jpg"), created_at=2000),
        event(record(HASH_A, name="old.jpg"), created_at=1000),
    ])

    assert library.get(HASH_A).name == "new.jpg"


def test_at_an_equal_timestamp_the_lower_event_id_wins():
    # NIP-01's tie-break. Without it two devices that saved in the same
    # second would show different libraries.
    library = loaded([
        event(record(HASH_A, name="second.jpg"), created_at=1000,
              event_id="f" * 64),
        event(record(HASH_A, name="first.jpg"), created_at=1000,
              event_id="0" * 64),
    ])

    assert library.get(HASH_A).name == "first.jpg"


def test_an_empty_library_settles_and_says_so():
    library, _query, _pool = make_library([])
    seen = []
    library.status_changed.connect(seen.append)
    library.bind_profile(PROFILE)

    assert len(library) == 0
    assert library.loading is False
    assert any("empty" in line.lower() for line in seen)


def test_a_library_of_two_hundred_files_arrives_whole():
    events = [event(record(f"{i:064x}")) for i in range(200)]

    library = loaded(events)

    assert len(library) == 200
    assert len(library.files) == 200
    assert library.failures == []


def test_two_hundred_files_cost_one_repaint():
    # One signal per decrypted file would rebuild the panel two hundred
    # times for one load.
    events = [event(record(f"{i:064x}")) for i in range(200)]
    library, _query, _pool = make_library(events)
    repaints = []
    library.library_changed.connect(lambda: repaints.append(1))

    library.bind_profile(PROFILE)

    assert len(repaints) == 1


def test_the_signer_is_asked_for_one_file_at_a_time():
    # One prompt at a time is the cost model the signer imposes; firing
    # all of them at once is how a user gets two hundred dialogs.
    manual = ManualBunkerClient()
    library, _query, _pool = make_library(
        [event(record(HASH_A)), event(record(HASH_B)), event(record(HASH_C))],
        client=manual,
    )
    library.bind_profile(PROFILE)

    assert len(manual.calls) == 1
    manual.settle_next()
    assert len(manual.calls) == 2
    manual.settle_next()
    assert len(manual.calls) == 3
    manual.settle_next()
    assert len(library) == 3


def test_the_files_are_listed_newest_first():
    # Real second-precision stamps, because a value too small to be a
    # date is now read as no date at all and would not order anything.
    library = loaded([
        event(record(HASH_A, uploadedAt=1_699_000_100)),
        event(record(HASH_B, uploadedAt=1_699_000_300)),
        event(record(HASH_C, uploadedAt=1_699_000_200)),
    ])

    assert [b.sha256 for b in library.files] == [HASH_B, HASH_C, HASH_A]


def test_a_file_with_no_usable_date_still_appears_in_the_listing():
    # Losing its place in the order is acceptable. Disappearing is not.
    library = loaded([
        event(record(HASH_A, uploadedAt=1_699_000_100)),
        event(record(HASH_B, uploadedAt="whenever")),
    ])

    assert {b.sha256 for b in library.files} == {HASH_A, HASH_B}


def test_the_injected_clock_stamps_when_the_listing_was_read():
    library = loaded([event(record())], clock=lambda: 424242)

    assert library.loaded_at == 424242


# --------------------------------------------------------------------- #
# One failure is one failure                                            #
# --------------------------------------------------------------------- #

def test_one_decrypt_failure_does_not_cost_the_rest():
    doomed = event(record(HASH_B))
    client = FakeBunkerClient(fail_on=[doomed["content"]])
    library = loaded(
        [event(record(HASH_A)), doomed, event(record(HASH_C))], client=client)

    assert {b.sha256 for b in library.files} == {HASH_A, HASH_C}
    assert [f.identifier for f in library.failures] == [HASH_B]
    assert isinstance(library.failures[0], LibraryFailure)
    assert "signer" in library.failures[0].reason.lower()


def test_one_unreadable_record_does_not_cost_the_rest():
    library = loaded([
        event(record(HASH_A)),
        event(json.dumps(record(HASH_B))[:30], d=HASH_B),
        event(record(HASH_C)),
    ])

    assert {b.sha256 for b in library.files} == {HASH_A, HASH_C}
    assert [f.identifier for f in library.failures] == [HASH_B]


def test_a_failure_message_never_carries_the_ciphertext_or_a_key():
    # A signer that echoes its input is not hypothetical, and a reason
    # string ends up in the UI and in the clipboard from there.
    doomed = event(record(HASH_B, key=OTHER_KEY))
    client = FakeBunkerClient(
        fail_on=[doomed["content"]],
        failure=f"could not decrypt {doomed['content']} with {OTHER_KEY}",
    )
    library = loaded([doomed], client=client)

    reason = library.failures[0].reason
    assert OTHER_KEY not in reason
    assert doomed["content"] not in reason
    assert "ENC[" not in reason


def test_the_part_of_a_signer_reason_worth_reading_survives():
    # Laundering the reason must not launder away the sentence that
    # tells the user what to do about it.
    doomed = event(record(HASH_B))
    client = FakeBunkerClient(
        fail_on=[doomed["content"]], failure="the user rejected the request")
    library = loaded([doomed], client=client)

    assert "the user rejected the request" in library.failures[0].reason


def test_a_status_line_never_carries_a_key():
    seen = []
    library, _query, _pool = make_library([event(record(key=OTHER_KEY))])
    library.status_changed.connect(seen.append)
    library.bind_profile(PROFILE)

    assert seen
    assert not any(OTHER_KEY in line for line in seen)


def test_failures_are_counted_in_the_status_line():
    doomed = event(record(HASH_B))
    client = FakeBunkerClient(fail_on=[doomed["content"]])
    seen = []
    library, _query, _pool = make_library(
        [event(record(HASH_A)), doomed], client=client)
    library.status_changed.connect(seen.append)
    library.bind_profile(PROFILE)

    assert any("1 could not be opened" in line for line in seen)


def test_a_missing_signer_is_reported_once_not_once_per_file():
    pool = FakeSessionPool(error="Amber is not paired")
    seen = []
    library, _query, _pool = make_library(
        [event(record(HASH_A)), event(record(HASH_B))], session_pool=pool)
    library.status_changed.connect(seen.append)
    library.bind_profile(PROFILE)

    assert len(library) == 0
    assert library.failures == []
    assert sum(1 for line in seen if "Amber is not paired" in line) == 1
    assert library.loading is False


def test_a_second_refresh_starts_from_a_clean_failure_list():
    doomed = event(record(HASH_B))
    client = FakeBunkerClient(fail_on=[doomed["content"]])
    library, _query, _pool = make_library([doomed], client=client)
    library.bind_profile(PROFILE)
    assert len(library.failures) == 1

    library.refresh()

    assert len(library.failures) == 1  # reported once, not twice over


# --------------------------------------------------------------------- #
# The keys live for the session and no longer                           #
# --------------------------------------------------------------------- #

def test_switching_profiles_drops_every_key():
    library, _query, _pool = make_library([event(record(key=OTHER_KEY))])
    library.bind_profile(PROFILE)
    assert library.get(HASH_A).key_hex == OTHER_KEY

    library.bind_profile(OTHER_PROFILE)

    assert len(library) == 0
    assert library.files == []
    assert library.get(HASH_A) is None


def test_clearing_the_profile_drops_every_key():
    library, _query, _pool = make_library([event(record())])
    library.bind_profile(PROFILE)

    library.bind_profile(None)

    assert len(library) == 0
    assert library.active_profile is None


def test_a_decryption_landing_after_a_profile_switch_is_ignored():
    # The signer answers whenever it answers. By then the user may be on
    # another identity, and this key does not belong to it.
    manual = ManualBunkerClient()
    library, _query, _pool = make_library([event(record())], client=manual)
    library.bind_profile(PROFILE)
    assert manual.pending

    library.bind_profile(OTHER_PROFILE)
    manual.settle_next()

    assert len(library) == 0


def test_a_relay_answer_landing_after_a_profile_switch_is_ignored():
    query = ParkedQuery()
    library, _query, _pool = make_library(query=query)
    library.bind_profile(PROFILE)
    assert query.pending

    library.bind_profile(None)
    query.pending[0]([event(record())])

    assert len(library) == 0


def test_an_answer_from_the_previous_load_does_not_join_the_new_one():
    # A refresh while the signer is still chewing on the last load. The
    # late answer belongs to a listing that no longer exists.
    manual = ManualBunkerClient()
    library, _query, _pool = make_library([event(record(HASH_A))], client=manual)
    library.bind_profile(PROFILE)
    assert len(manual.pending) == 1

    library.refresh()
    assert len(manual.pending) == 2
    ciphertext, on_success, _on_failure = manual.pending.pop(0)
    on_success(ciphertext[4:-1])

    assert len(library) == 0
    manual.settle_next()
    assert len(library) == 1


def test_binding_the_same_profile_again_does_not_re_ask_the_signer():
    library, query, pool = make_library([event(record())])
    library.bind_profile(PROFILE)
    calls = pool.calls

    library.bind_profile(PROFILE)

    assert pool.calls == calls
    assert len(query.calls) == 1


def test_the_library_holds_no_file_path_to_persist_a_key_to():
    # Keys are session state. The day this object grows somewhere on
    # disk is the day to think again about what is being written there.
    library, _query, _pool = make_library([event(record())])
    library.bind_profile(PROFILE)

    assert [v for v in vars(library).values() if isinstance(v, Path)] == []


# --------------------------------------------------------------------- #
# What the listing is entitled to say                                   #
# --------------------------------------------------------------------- #
#
# "Not in this library" is two answers wearing one face: not private, or
# not read yet. Serving the second as the first is how a file the user
# keeps encrypted gets its ciphertext address written into a signed
# event, so the tests below are about the second answer existing at all.

def test_a_library_that_has_never_been_read_vouches_for_nothing():
    library, _query, _pool = make_library([event(record())])

    assert library.settled is False
    assert library.vouches_for(HASH_A) is False
    assert library.vouches_for(HASH_C) is False


def test_a_finished_load_vouches_for_a_file_it_never_saw():
    library = loaded([event(record(HASH_A))])

    assert library.settled is True
    # HASH_C is genuinely not in the library, and after a finished load
    # saying so is an answer rather than a guess. That is the sentence
    # the whole flow rests on being trustworthy.
    assert library.vouches_for(HASH_C) is True


def test_a_load_waiting_on_the_signer_vouches_for_nothing_yet():
    manual = ManualBunkerClient()
    library, _query, _pool = make_library(
        [event(record(HASH_A)), event(record(HASH_B))], client=manual)
    library.bind_profile(PROFILE)

    # The prompt is on screen and neither record is open. Both files are
    # private, and this library cannot yet say which.
    assert library.loading is True
    assert library.settled is False
    assert library.vouches_for(HASH_A) is False
    assert library.vouches_for(HASH_B) is False

    manual.settle_next()
    manual.settle_next()

    # Both are accounted for now, which is what vouching means: HASH_A is
    # accounted for as private, and a hash the library never saw is
    # accounted for as not its business.
    assert library.settled is True
    assert library.get(HASH_A) is not None
    assert library.vouches_for(HASH_A) is True
    assert library.vouches_for(HASH_C) is True


def test_a_file_still_queued_is_not_vouched_for_while_another_opens():
    manual = ManualBunkerClient()
    library, _query, _pool = make_library(
        [event(record(HASH_A)), event(record(HASH_B))], client=manual)
    library.bind_profile(PROFILE)
    manual.settle_next()

    # One is open, one is still behind the prompt. The open one is known
    # private; the queued one is not yet anything, and the listing as a
    # whole is not finished.
    assert library.get(HASH_A) is not None
    assert library.settled is False
    assert library.vouches_for(HASH_B) is False


def test_a_signer_that_never_answers_vouches_for_nothing_it_listed():
    pool = FakeSessionPool(error="bunker relay refused the connection")
    library, _query, _pool = make_library(
        [event(record(HASH_A)), event(record(HASH_B))], session_pool=pool)
    library.bind_profile(PROFILE)

    # Permanent for the session, not a race: nothing will open these.
    assert library.loading is False
    assert library.settled is False
    assert library.vouches_for(HASH_A) is False
    assert library.vouches_for(HASH_B) is False


def test_a_closed_library_still_vouches_for_files_it_never_listed():
    # The relays did answer, so the set of private records is known even
    # though none of them could be opened. A blob outside that set is
    # public, and refusing to say so would make an ordinary library
    # unusable every time a signer is unplugged.
    pool = FakeSessionPool(error="Amber is not paired")
    library, _query, _pool = make_library([event(record(HASH_A))],
                                          session_pool=pool)
    library.bind_profile(PROFILE)

    assert library.vouches_for(HASH_A) is False
    assert library.vouches_for(HASH_C) is True


def test_a_record_that_could_not_be_opened_is_not_vouched_for():
    doomed = event(record(HASH_B))
    client = FakeBunkerClient(fail_on=[doomed["content"]])
    library, _query, _pool = make_library(
        [event(record(HASH_A)), doomed], client=client)
    library.bind_profile(PROFILE)

    assert len(library.failures) == 1
    assert library.settled is False
    assert library.vouches_for(HASH_B) is False
    # The one that opened is unaffected, and so is a file neither names.
    assert library.get(HASH_A) is not None
    assert library.vouches_for(HASH_C) is True


def test_a_record_with_no_content_is_not_vouched_for():
    blank = event(record(HASH_B))
    blank["content"] = ""
    library = loaded([event(record(HASH_A)), blank])

    assert library.vouches_for(HASH_B) is False
    assert library.vouches_for(HASH_C) is True


def test_an_unopenable_record_filed_under_junk_poisons_every_answer():
    # Its hash is inside the ciphertext, so a failure to open it leaves
    # no way to tell which file it was. Vouching for anything else would
    # be a guess, and the guess is the one that publishes a private file.
    doomed = event(record(HASH_B), d="holiday-photos")
    client = FakeBunkerClient(fail_on=[doomed["content"]])
    library, _query, _pool = make_library([doomed], client=client)
    library.bind_profile(PROFILE)

    assert library.vouches_for(HASH_C) is False


def test_a_tombstone_resolves_rather_than_lingering_as_unknown():
    library = loaded([event(record(HASH_A, deleted=True))])

    assert library.settled is True
    assert library.vouches_for(HASH_A) is True


def test_a_refresh_stops_vouching_until_the_relays_answer_again():
    parked = ParkedQuery()
    library, _query, _pool = make_library(query=parked)
    library.bind_profile(PROFILE)
    parked.pending.pop(0)([event(record(HASH_A))])
    assert library.settled is True

    library.refresh()

    # The previous load's coverage does not carry over, or a refresh
    # would open a window in which private files read as public.
    assert library.settled is False
    assert library.vouches_for(HASH_C) is False


def test_switching_profiles_stops_vouching_for_the_previous_identity():
    library = loaded([event(record(HASH_A))])
    assert library.vouches_for(HASH_C) is True

    library.bind_profile(None)

    assert library.settled is False
    assert library.vouches_for(HASH_C) is False

# --------------------------------------------------------------------- #
# The upload timestamp comes off a relay, so it is not trusted          #
# --------------------------------------------------------------------- #

def test_a_timestamp_in_seconds_is_kept():
    # What the reference implementation actually writes at every site:
    # Math.floor(Date.now() / 1000).
    parsed = parse_file_record(
        json.dumps(record(uploadedAt=1_755_000_000)), identifier=HASH_A,
    )
    assert parsed.blob is not None and parsed.blob.uploaded_at == 1_755_000_000


def test_a_timestamp_in_milliseconds_is_dropped_rather_than_shown():
    # A record written in milliseconds would render as a date tens of
    # thousands of years away. Unknown reads as unknown; confidently
    # wrong reads as a broken app, and the user cannot tell which.
    parsed = parse_file_record(
        json.dumps(record(uploadedAt=1_755_000_000_000)), identifier=HASH_A,
    )
    assert parsed.blob is not None and parsed.blob.uploaded_at == 0


@pytest.mark.parametrize("value", [-1, 0, 1, 999, "not a number", None, [], {}])
def test_an_implausible_timestamp_becomes_unknown(value):
    parsed = parse_file_record(
        json.dumps(record(uploadedAt=value)), identifier=HASH_A,
    )
    assert parsed.blob is not None and parsed.blob.uploaded_at == 0


def test_the_file_is_still_usable_without_a_date():
    # A missing or unreadable date must not cost the user the file.
    parsed = parse_file_record(
        json.dumps(record(uploadedAt=None)), identifier=HASH_A,
    )
    assert parsed.blob is not None
    assert parsed.blob.sha256 == HASH_A and parsed.blob.key_hex == KEY
