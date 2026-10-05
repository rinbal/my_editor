# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how the media library treats each server, the members' one included.

What must hold:

  Every server reports what it holds (count and bytes), so the library
  can say how full the members' server is.

  A server that cannot be listed keeps the files the library saw there
  last time: a server that is briefly down has deleted nothing.

  Copies are ordered with the configured primary first, so the URL the
  app copies and inserts does not depend on which server answered first.

  A server known to list publicly (the members' server) is listed
  without asking the signer; one that turns out to want a token still
  gets one.

  A new server (a membership that resolved) makes the library stale at
  once, so its files appear without waiting out the freshness window.

  An upload leaves out a server where the file would not fit in the
  allowance, says so, and still succeeds elsewhere.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.blossom.client import BlossomClient  # noqa: E402
from nostr.blossom.settings import BlossomSettings  # noqa: E402
from nostr.blossom.store import MediaStore  # noqa: E402
from nostr.einundzwanzig import PER_USER_BYTES, PER_USER_LABEL  # noqa: E402
from nostr.ui.media_library_dialog import storage_text  # noqa: E402
from nostr.blossom.store import ServerListing  # noqa: E402
from tests.blossom_fakes import (  # noqa: E402
    BODY, MIRROR, SERVER, SHA, FakeBlossomServer, FakeNam, FakeProfile,
    FakeSessionPool, FakeSigner, descriptor, error_reply,
)

E21 = "https://blossom.einundzwanzig.space"
A, B, C = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class Router:
    """Several fake servers behind one transport, by host."""

    def __init__(self, *servers):
        self.by_host = {QUrl(s.origin).host(): s for s in servers}
        self.down = set()
        self.wants_token = set()

    def __call__(self, verb, request, body):
        host = request.url().host()
        if host in self.down and verb == "get":
            return error_reply(503, reason="Down for maintenance.")
        if host in self.wants_token and verb == "get" and \
                not request.hasRawHeader("Authorization"):
            return error_reply(401, reason="Sign in to list.")
        return self.by_host[host](verb, request, body)


def make(tmp_path, router, *, servers=(SERVER, MIRROR), entitled=(), quota=None):
    settings = BlossomSettings(tmp_path / "blossom_servers.json")
    settings.set_custom_servers(list(servers))
    nam = FakeNam(None, responder=router)
    signer = FakeSigner()
    pool = FakeSessionPool(signer)
    entitled_list = list(entitled)
    store = MediaStore(
        session_pool=pool,
        profile_provider=lambda: FakeProfile(),
        settings=settings,
        client=BlossomClient(nam=nam),
        entitled_servers=lambda: entitled_list,
        server_quota=quota,
    )
    return store, nam, signer, entitled_list


def test_each_server_reports_what_it_holds(tmp_path):
    router = Router(
        FakeBlossomServer(SERVER, blobs=[descriptor(A, size=100), descriptor(B, size=50)]),
        FakeBlossomServer(MIRROR, blobs=[descriptor(B, server=MIRROR, size=50)]))
    store, nam, _signer, _ = make(tmp_path, router)
    store.fetch()
    nam.settle()
    listings = store.server_listings()
    assert (listings[SERVER].ok, listings[SERVER].count, listings[SERVER].bytes) == (True, 2, 150)
    assert (listings[MIRROR].count, listings[MIRROR].bytes) == (1, 50)
    assert store.bytes_on(MIRROR) == 50 and store.bytes_on(SERVER) == 150


def test_an_unreachable_server_keeps_its_files_from_last_time(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(MIRROR, blobs=[descriptor(C, server=MIRROR)]))
    store, nam, _signer, _ = make(tmp_path, router)
    store.fetch()
    nam.settle()
    assert set(store.files) == {A, C}

    router.down.add("mirror.example")
    store.fetch(force=True)
    nam.settle()
    assert set(store.files) == {A, C}            # C is not gone, only unconfirmed
    assert not store.server_listings()[MIRROR].ok
    assert store.files[C].urls[0]["server"] == MIRROR


def test_copies_are_ordered_primary_first(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(MIRROR, blobs=[descriptor(A, server=MIRROR)]))
    store, nam, _signer, _ = make(tmp_path, router, servers=(MIRROR, SERVER))
    store.fetch()
    nam.settle()
    media = store.files[A]
    assert [u["server"] for u in media.urls] == [MIRROR, SERVER]
    assert media.url == media.urls[0]["url"]


def test_the_members_server_is_listed_without_a_signer_prompt(tmp_path):
    e21 = FakeBlossomServer(E21, blobs=[descriptor(B, server=E21, size=7)])
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]), e21)
    store, nam, signer, _ = make(tmp_path, router, servers=(SERVER,), entitled=[E21])
    store.fetch()
    nam.settle()
    assert B in store.files
    servers_signed_for = [t for t in signer.requests if ["t", "list"] in t["tags"]]
    assert len(servers_signed_for) == 1          # the configured server only
    assert all("einundzwanzig" not in str(t["tags"]) for t in servers_signed_for)


def test_a_public_listing_that_wants_a_token_gets_one(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(E21, blobs=[descriptor(B, server=E21)]))
    router.wants_token.add("blossom.einundzwanzig.space")
    store, nam, signer, _ = make(tmp_path, router, servers=(SERVER,), entitled=[E21])
    store.fetch()
    nam.settle()
    assert B in store.files
    assert store.server_listings()[E21].ok


def test_a_new_server_makes_the_library_stale_at_once(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(E21, blobs=[descriptor(B, server=E21)]))
    store, nam, _signer, entitled = make(tmp_path, router, servers=(SERVER,))
    store.fetch()
    nam.settle()
    assert set(store.files) == {A}
    calls = len(nam.calls)
    store.fetch()
    assert len(nam.calls) == calls               # nothing changed: fresh, no request

    entitled.append(E21)                         # the membership resolved
    store.fetch()
    nam.settle()
    assert set(store.files) == {A, B}


def test_a_full_server_is_left_out_of_an_upload_and_it_says_so(tmp_path):
    router = Router(FakeBlossomServer(SERVER), FakeBlossomServer(E21))
    store, nam, _signer, _ = make(
        tmp_path, router, servers=(SERVER,), entitled=[E21],
        quota=lambda origin: 1 if origin == E21 else None)
    store.fetch()
    nam.settle()                                 # usage is known: it listed fine
    skipped, finished = [], []
    store.server_skipped.connect(lambda n, h, r: skipped.append((n, h, r)))
    store.upload_finished.connect(lambda n, m: finished.append(m))
    store.upload_bytes(BODY, name="photo.png", mime_type="image/png")
    nam.settle()
    assert skipped == [("photo.png", "blossom.einundzwanzig.space", "full")]
    assert finished and finished[0].hash == SHA
    assert all("einundzwanzig" not in u["server"] for u in finished[0].urls)


def test_no_room_anywhere_fails_plainly(tmp_path):
    router = Router(FakeBlossomServer(E21))
    store, nam, _signer, _ = make(tmp_path, router, servers=(E21,),
                                  quota=lambda origin: 1)
    store.fetch()
    nam.settle()
    calls = len(nam.calls)
    failed = []
    store.upload_failed.connect(lambda n, r: failed.append(r))
    store.upload_bytes(BODY, name="photo.png", mime_type="image/png")
    assert failed and "enough space" in failed[0]
    assert len(nam.calls) == calls               # nothing left the machine


@pytest.mark.parametrize("listed", ["never", "failed"])
def test_a_server_is_not_left_out_on_a_guess(tmp_path, listed):
    # Usage counted without a good listing is a guess: the server decides.
    e21 = FakeBlossomServer(E21, blobs=[descriptor(A, server=E21, size=50)])
    router = Router(FakeBlossomServer(SERVER), e21)
    store, nam, _signer, _ = make(
        tmp_path, router, servers=(SERVER,), entitled=[E21],
        quota=lambda origin: 60 if origin == E21 else None)
    if listed == "failed":
        store.fetch()
        nam.settle()
        assert store.bytes_on(E21) == 50
        router.down.add("blossom.einundzwanzig.space")
        store.fetch(force=True)
        nam.settle()
        router.down.clear()
        assert not store.server_listings()[E21].ok
    skipped = []
    store.server_skipped.connect(lambda n, h, r: skipped.append(h))
    store.upload_bytes(b"x" * 20, name="clip.bin")
    nam.settle()
    assert skipped == []


def test_the_members_server_accepts_files_up_to_a_gigabyte():
    from nostr.blossom.plan import get_effective_max_file, lists_publicly
    assert get_effective_max_file(E21) == 1024 ** 3
    assert lists_publicly(E21) and not lists_publicly(SERVER)


def test_the_display_bound_is_not_an_upload_ceiling():
    # The comments once called 100 MiB the largest file the app uploads.
    # The planner lets the members' server take what it publishes.
    from nostr.blossom.plan import BLOSSOM_MAX_FILE_SIZE, plan_upload
    big = BLOSSOM_MAX_FILE_SIZE * 2
    assert plan_upload(big, [E21]).primary == E21


def test_the_published_limit_and_the_server_registry_agree():
    # Two places name the members' per-file limit; they must not drift.
    from nostr.blossom.plan import get_effective_max_file
    from nostr.einundzwanzig import MAX_FILE_BYTES, MEMBER_BLOSSOM
    assert get_effective_max_file(MEMBER_BLOSSOM) == MAX_FILE_BYTES


# -- a walk that cannot loop, cannot leak, and cannot drop a server ---------------------

def test_a_server_refusing_both_ways_is_asked_twice_and_marked_failed(tmp_path):
    # A public-list server that refuses unsigned and signed alike: one
    # fallback, then a failed listing and a finished fetch, never a loop.
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(E21, fail_with=403, fail_reason="No."))
    store, nam, signer, _ = make(tmp_path, router, servers=(SERVER,), entitled=[E21])
    finished = []
    store.fetch_finished.connect(lambda: finished.append(True))
    store.fetch()
    nam.settle()
    asked_e21 = [r for _v, r, _b in nam.calls
                 if r.url().host() == "blossom.einundzwanzig.space"]
    assert len(asked_e21) == 2                     # unsigned, then signed once
    assert sum(r.hasRawHeader("Authorization") for r in asked_e21) == 1
    assert finished == [True]
    listing = store.server_listings()[E21]
    assert not listing.ok and listing.error_code
    assert A in store.files                        # the other server still counts


def test_a_signed_refusal_falls_back_once_and_stops(tmp_path):
    router = Router(FakeBlossomServer(SERVER, fail_with=401, fail_reason="No."))
    store, nam, _signer, _ = make(tmp_path, router, servers=(SERVER,))
    store.fetch()
    nam.settle()
    assert len(nam.requests_to("/list/" + FakeProfile().user_pubkey)) == 2
    assert not store.server_listings()[SERVER].ok


class SwitchableProfile:
    def __init__(self, pubkey):
        self.user_pubkey = pubkey
        self.bunker_relays = []


def make_switchable(tmp_path, router, *, servers=(SERVER,)):
    settings = BlossomSettings(tmp_path / "blossom_servers.json")
    settings.set_custom_servers(list(servers))
    nam = FakeNam(None, responder=router)
    active = {"profile": FakeProfile()}
    store = MediaStore(
        session_pool=FakeSessionPool(FakeSigner()),
        profile_provider=lambda: active["profile"],
        settings=settings,
        client=BlossomClient(nam=nam),
    )
    return store, nam, active


def test_a_walk_for_the_account_being_left_never_lands(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]))
    store, nam, active = make_switchable(tmp_path, router)
    store.fetch()                                  # in flight for account one
    active["profile"] = SwitchableProfile("cd" * 32)
    store.clear()
    store.fetch()                                  # account two gets its own walk
    lists = nam.requests_to("/list/" + "cd" * 32)
    assert len(lists) == 1, "the new account's fetch was folded into the old walk"
    old = nam.issued[0]
    old.finish()                                   # the old answer arrives late
    assert store.files == {} and store.server_listings() == {}
    nam.settle()
    assert set(store.files) == {A}                 # the new walk's own answer


def test_a_late_answer_after_clear_alone_is_ignored(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]))
    store, nam, _active = make_switchable(tmp_path, router)
    finished = []
    store.fetch_finished.connect(lambda: finished.append(True))
    store.fetch()
    store.clear()
    nam.settle()
    assert store.files == {} and finished == []


def test_a_server_added_during_a_walk_is_listed_when_it_ends(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(E21, blobs=[descriptor(B, server=E21)]))
    store, nam, _signer, entitled = make(tmp_path, router, servers=(SERVER,))
    store.fetch()
    entitled.append(E21)                           # the membership resolved mid-walk
    store.fetch()                                  # coalesced, but remembered
    nam.settle()
    assert set(store.files) == {A, B}
    assert E21 in store.server_listings()


def test_a_request_during_a_walk_with_the_same_servers_costs_nothing(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]))
    store, nam, signer, _ = make(tmp_path, router, servers=(SERVER,))
    store.fetch()
    store.fetch()
    nam.settle()
    assert len(signer.requests) == 1


def test_a_membership_check_that_changed_nothing_does_not_refetch(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(E21, blobs=[descriptor(B, server=E21)]))
    store, nam, signer, entitled = make(tmp_path, router, servers=(SERVER,))
    assert store.refetch_if_targets_changed() is False
    assert nam.calls == []                         # never fetched: stays that way
    store.fetch()
    nam.settle()
    store._last_fetch_at -= 3600                   # long past the freshness window
    prompts = len(signer.requests)
    assert store.refetch_if_targets_changed() is False
    assert len(signer.requests) == prompts         # no signer prompt for nothing

    entitled.append(E21)                           # membership flipped
    assert store.refetch_if_targets_changed() is True
    nam.settle()
    assert set(store.files) == {A, B}


def test_a_target_change_during_a_walk_is_picked_up_by_refetch(tmp_path):
    router = Router(FakeBlossomServer(SERVER, blobs=[descriptor(A)]),
                    FakeBlossomServer(E21, blobs=[descriptor(B, server=E21)]))
    store, nam, _signer, entitled = make(tmp_path, router, servers=(SERVER,))
    store.fetch()
    entitled.append(E21)
    assert store.refetch_if_targets_changed() is False
    nam.settle()
    assert set(store.files) == {A, B}


# -- the storage meter's words ---------------------------------------------------------

GB = 1024 ** 3          # binary, the unit allowances are stated in
MB = 1024 ** 2


def test_storage_text_says_the_number_and_warns_in_words():
    ok = ServerListing(origin=E21, ok=True)
    text, fraction = storage_text(int(1.2 * GB), PER_USER_BYTES, ok)
    assert text == "1.2 GB of 5 GB used" and fraction == pytest.approx(0.24)
    text, _ = storage_text(int(4.6 * GB), PER_USER_BYTES, ok)
    assert text.startswith("Almost full:")
    text, fraction = storage_text(6 * GB, PER_USER_BYTES, ok)
    assert text.startswith("Full:") and fraction == 1.0
    assert storage_text(640 * MB, PER_USER_BYTES, ok)[0] == "640 MB of 5 GB used"


def test_the_members_allowance_reads_as_the_association_states_it():
    # 5 x 1024^3 bytes once read "5.4 GB" against a "5 GB" promise.
    ok = ServerListing(origin=E21, ok=True)
    text, _ = storage_text(PER_USER_BYTES, PER_USER_BYTES, ok)
    assert text == "Full: 5 GB of 5 GB used"
    assert PER_USER_LABEL == "5 GB"


def test_storage_text_rounds_up_to_a_gigabyte_rather_than_1024_megabytes():
    ok = ServerListing(origin=E21, ok=True)
    assert storage_text(GB - 1, PER_USER_BYTES, ok)[0].startswith("1 GB of")


def test_storage_text_is_honest_about_what_it_does_not_know():
    assert storage_text(0, 5 * GB, None)[0] == "Checking…"
    down = ServerListing(origin=E21, ok=False)
    assert storage_text(0, 5 * GB, down)[0] == "Usage unavailable right now."
    cut = ServerListing(origin=E21, ok=True, truncated=True)
    assert storage_text(GB, 5 * GB, cut)[0].startswith("At least")
