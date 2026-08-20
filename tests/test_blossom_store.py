# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``MediaStore``: upload, mirror, delete and the paged library fetch.

The store had no coverage at all before this file, and it is the piece
that decides how many times a user's signer prompts them. The first
section is characterization: what the current pipeline does, written
down so a later refactor has something to break loudly rather than
quietly.

Nothing here opens a socket, reaches a relay, or signs: the transport is
``FakeNam`` (often driven by ``FakeBlossomServer``) and the signer is
``FakeSessionPool``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl, QUrlQuery
from PySide6.QtWidgets import QApplication

from nostr.blossom import plan as plan_module
from nostr.blossom.client import BlossomClient
from nostr.blossom.errors import ERROR_CODES, friendly_message
from nostr.blossom.settings import BlossomSettings
from nostr.blossom.store import MediaStore
from nostr.ui.thumbnail_loader import ThumbnailLoader
from tests.blossom_fakes import (
    BODY,
    MIRROR,
    OTHER_SHA,
    SERVER,
    SHA,
    FakeBlossomServer,
    FakeNam,
    FakeProfile,
    FakeSessionPool,
    FakeSigner,
    descriptor,
    error_reply,
    json_reply,
)
from tests.media_fakes import PNG_BYTES


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class Ctx:
    """One wired-up store plus everything a test needs to drive it."""

    def __init__(self, store, nam, pool, signer, settings):
        self.store = store
        self.nam = nam
        self.pool = pool
        self.signer = signer
        self.settings = settings
        self.failures = []
        self.finished = []
        self.rerouted = []
        self.mirror_failures = []
        self.failure_codes = []
        store.upload_failed.connect(lambda n, r: self.failures.append((n, r)))
        store.upload_failed_code.connect(
            lambda n, c: self.failure_codes.append((n, c)))
        store.upload_finished.connect(lambda n, m: self.finished.append((n, m)))
        store.upload_rerouted.connect(
            lambda n, f, t: self.rerouted.append((n, f, t)))
        store.mirror_failed.connect(
            lambda n, h, c: self.mirror_failures.append((n, h, c)))

    def settle(self) -> None:
        self.nam.settle()

    def probe(self) -> None:
        """Answer the dedup probes and stop before whatever they cause.

        Every upload now asks each planned server whether it already has
        the blob, so a test that wants to drive the upload itself has to
        get past that first.
        """
        self.nam.settle_verb("head")

    def verbs(self):
        return [verb for verb, _r, _b in self.nam.calls]

    def tokens(self):
        """Every unsigned auth event the signer was asked for."""
        return list(self.signer.requests)


def make_store(tmp_path, *, servers=(SERVER,), replies=None, responder=None,
               signer=None, pool_error=None, profile=True, blob_cache=None,
               entitled_servers=None):
    settings = BlossomSettings(tmp_path / "blossom_servers.json")
    settings.set_custom_servers(list(servers))
    nam = FakeNam(replies, responder=responder)
    signer = signer or FakeSigner()
    pool = FakeSessionPool(signer, error=pool_error)
    store = MediaStore(
        session_pool=pool,
        profile_provider=lambda: FakeProfile() if profile else None,
        settings=settings,
        client=BlossomClient(nam=nam),
        blob_cache=blob_cache,
        entitled_servers=entitled_servers,
    )
    return Ctx(store, nam, pool, signer, settings)


def tag(event, name):
    return [t[1] for t in event["tags"] if t[0] == name]


# --------------------------------------------------------------------------- #
# Characterization: the shape of each pipeline
# --------------------------------------------------------------------------- #

def test_upload_signs_once_then_puts_once(tmp_path):
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER))
    ctx.store.upload_bytes(BODY, name="a.png", mime_type="image/png")
    # Nothing is signed until the server has said it does not have the
    # blob: asking is free, signing costs the user a prompt.
    assert ctx.verbs() == ["head"]
    assert ctx.pool.calls == 0
    ctx.probe()
    assert ctx.pool.calls == 1
    assert ctx.verbs() == ["head", "put"]
    ctx.settle()
    assert ctx.failures == []
    name, media = ctx.finished[0]
    assert name == "a.png"
    assert media.hash == SHA
    assert media.url == f"{SERVER}/{SHA}.png"
    assert ctx.store.files[SHA] is media


def test_upload_without_a_profile_never_touches_the_network(tmp_path):
    ctx = make_store(tmp_path, profile=False)
    ctx.store.upload_bytes(BODY, name="a.png")
    assert ctx.nam.calls == []
    assert ctx.pool.calls == 0
    assert ctx.failures == [("a.png", "Connect a Nostr signer first.")]


def test_an_empty_body_is_refused(tmp_path):
    ctx = make_store(tmp_path)
    ctx.store.upload_bytes(b"", name="a.png")
    assert ctx.nam.calls == []
    assert ctx.failures == [("a.png", "Nothing to upload.")]


def test_a_signer_refusal_keeps_its_exact_wording(tmp_path):
    """``nostr/media/manager.py`` classifies signer failures by matching
    this prefix. Rewording it silently reclassifies every one of them."""
    ctx = make_store(tmp_path, signer=FakeSigner(failure="user declined"))
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    assert ctx.verbs() == ["head"]        # refused before the PUT
    assert ctx.failures[0][1] == (
        "signer rejected the Blossom auth event: user declined"
    )
    assert ctx.failure_codes == [("a.png", ERROR_CODES.SIGNER_REJECTED)]


def test_delete_targets_every_server_the_blob_lives_on(tmp_path):
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER))
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()

    deleted = []
    ctx.store.file_deleted.connect(deleted.append)
    ctx.store.delete_file(SHA)
    assert [v for v, _r, _b in ctx.nam.calls][-1] == "delete"
    ctx.settle()
    assert deleted == [SHA]
    assert SHA not in ctx.store.files


def test_delete_auth_carries_the_hash_and_the_bare_domain(tmp_path):
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER))
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    ctx.store.delete_file(SHA)
    token = ctx.tokens()[-1]
    assert tag(token, "t") == ["delete"]
    assert tag(token, "x") == [SHA]
    assert tag(token, "server") == ["good.example"]


def test_fetch_populates_the_library_and_is_freshness_gated(tmp_path):
    blobs = [descriptor(f"{i:064x}", uploaded=100 + i) for i in range(3)]
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER, blobs=blobs))
    ctx.store.fetch()
    ctx.settle()
    assert sorted(ctx.store.files) == sorted(b["sha256"] for b in blobs)

    before = len(ctx.nam.calls)
    ctx.store.fetch()
    assert len(ctx.nam.calls) == before      # inside the freshness window


def test_a_401_on_list_retries_once_without_auth(tmp_path):
    ctx = make_store(tmp_path, replies=[error_reply(401), json_reply([])])
    ctx.store.fetch()
    ctx.nam.issued[0].finish()
    assert len(ctx.nam.calls) == 2
    request = ctx.nam.calls[1][1]
    assert not request.hasRawHeader("Authorization")
    ctx.nam.issued[1].finish()
    assert ctx.pool.calls == 1


# --------------------------------------------------------------------------- #
# The mirror leg
# --------------------------------------------------------------------------- #

def test_mirror_auth_carries_the_required_x_tag(tmp_path):
    """BUD-11: ``PUT /mirror`` takes ``t=upload`` and REQUIRES an ``x``
    tag holding the sha256 of the mirrored blob. Omitting it is why
    replication silently did not happen."""
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        responder=_two_server_responder(),
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    mirror_token = ctx.tokens()[1]
    assert tag(mirror_token, "t") == ["upload"]
    assert tag(mirror_token, "x") == [SHA]
    assert tag(mirror_token, "server") == ["mirror.example"]


def test_a_failing_mirror_is_reported_and_the_upload_still_commits(tmp_path):
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        replies=[json_reply(descriptor(), status=201),
                 error_reply(429, reason="Slow down")],
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    assert ctx.mirror_failures == [("a.png", "mirror.example",
                                    ERROR_CODES.RATE_LIMITED)]
    assert ctx.failures == []
    name, media = ctx.finished[0]
    assert media.hash == SHA
    assert [u["server"] for u in media.urls] == [SERVER]


def test_a_mirror_descriptor_for_another_blob_is_refused(tmp_path):
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        replies=[json_reply(descriptor(), status=201),
                 json_reply(descriptor(OTHER_SHA, server=MIRROR))],
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    assert ctx.mirror_failures == [("a.png", "mirror.example",
                                    ERROR_CODES.HASH_MISMATCH)]
    _name, media = ctx.finished[0]
    assert [u["server"] for u in media.urls] == [SERVER]


def test_a_successful_mirror_joins_the_url_list(tmp_path):
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        responder=_two_server_responder(),
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    _name, media = ctx.finished[0]
    assert [u["server"] for u in media.urls] == [SERVER, MIRROR]
    assert ctx.mirror_failures == []


def _two_server_responder():
    """Route by host so one fake stands in for the whole configuration."""
    primary = FakeBlossomServer(SERVER)
    mirror = FakeBlossomServer(MIRROR)

    def respond(verb, request, body):
        host = request.url().host().lower()
        return (mirror if host == "mirror.example" else primary)(verb, request, body)

    return respond


# --------------------------------------------------------------------------- #
# BUD-08 nip94 capture
# --------------------------------------------------------------------------- #

def test_nip94_from_the_upload_response_lands_on_the_record(tmp_path):
    pairs = [["m", "image/png"], ["x", SHA], ["size", "3"]]
    ctx = make_store(tmp_path,
                     replies=[json_reply(descriptor(nip94=pairs), status=201)])
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    _name, media = ctx.finished[0]
    assert media.nip94 == pairs


# --------------------------------------------------------------------------- #
# Upload failover
# --------------------------------------------------------------------------- #

def test_a_transport_failure_reaches_the_second_server(tmp_path):
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        replies=[error_reply(0), json_reply(descriptor(server=MIRROR),
                                            status=201)],
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.nam.settle_one()                     # the primary's PUT fails
    assert ctx.verbs() == ["head", "head", "put", "put"]
    assert ctx.nam.calls[3][1].url().host() == "mirror.example"
    # Each attempt signs a fresh token, scoped to the server it goes to.
    assert tag(ctx.tokens()[1], "server") == ["mirror.example"]
    ctx.nam.settle_one()
    assert ctx.failures == []
    assert ctx.finished[0][1].hash == SHA


def test_a_403_produces_no_second_attempt(tmp_path):
    """401 and 403 usually mean the signer or the clock, so failing over
    buys a second prompt and a second refusal."""
    ctx = make_store(tmp_path, servers=(SERVER, MIRROR),
                     replies=[error_reply(403)])
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.nam.settle_one()
    assert ctx.verbs() == ["head", "head", "put"]
    assert ctx.pool.calls == 1
    assert ctx.failure_codes == [("a.png", ERROR_CODES.AUTH_REJECTED)]


def test_a_413_does_fail_over(tmp_path):
    """The one case the size planner cannot know: a server whose real
    cap is below its published one."""
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        replies=[error_reply(413), json_reply(descriptor(server=MIRROR),
                                              status=201)],
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.nam.settle_one()
    assert ctx.verbs() == ["head", "head", "put", "put"]
    ctx.nam.settle_one()
    assert ctx.failures == []


def test_failover_stops_at_the_attempt_cap(tmp_path):
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR, "https://third.example"),
        replies=[error_reply(0), error_reply(0), json_reply(descriptor())],
    )
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.nam.settle_one()
    ctx.nam.settle_one()
    assert ctx.verbs().count("put") == 2
    assert len(ctx.failures) == 1
    assert ctx.failures[0][1] == friendly_message(ERROR_CODES.NETWORK_UNAVAILABLE)


def test_all_attempts_failing_emits_upload_failed_once(tmp_path):
    ctx = make_store(tmp_path, servers=(SERVER, MIRROR),
                     replies=[error_reply(0), error_reply(0)])
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    assert len(ctx.failures) == 1
    assert len(ctx.failure_codes) == 1


def test_failover_never_contacts_a_server_the_planner_skipped(tmp_path, monkeypatch):
    """A server skipped for size must stay unreachable, including by a
    retry: contacting it would push a file it already said it cannot
    take."""
    monkeypatch.setattr(plan_module, "BLOSSOM_SERVER_INFO", {
        "good.example": _info(free_max_file=64),
        "mirror.example": _info(free_max_file=8),
    })
    body = b"x" * 32
    ctx = make_store(tmp_path, servers=(SERVER, MIRROR),
                     replies=[error_reply(0)])
    ctx.store.upload_bytes(body, name="big.png")
    ctx.settle()
    # Not even a dedup probe: a server that cannot take the file has no
    # business being asked whether it already has it.
    assert [r.url().host() for _v, r, _b in ctx.nam.calls] == [
        "good.example", "good.example",
    ]
    assert len(ctx.failures) == 1


def _desc_for(body: bytes, server: str = SERVER, **kwargs) -> dict:
    """A descriptor that agrees with the bytes: the client verifies the
    hash, so a mismatched fixture would read as a hostile server."""
    return descriptor(hashlib.sha256(body).hexdigest(), server=server, **kwargs)


def _info(**overrides):
    base = {
        "free": True, "paid": False, "requires_auth": True,
        "free_max_file": None, "paid_max_file": None,
        "confidence": "documented", "notes": None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Reroute and the size message
# --------------------------------------------------------------------------- #

def test_the_size_message_names_the_server_cap_not_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "BLOSSOM_SERVER_INFO", {
        "good.example": _info(free_max_file=1024 * 1024),
    })
    ctx = make_store(tmp_path)
    ctx.store.upload_bytes(b"x" * (2 * 1024 * 1024), name="big.png")
    assert ctx.nam.calls == []
    reason = ctx.failures[0][1]
    assert "(1 MiB)" in reason
    assert "2 MiB" not in reason


def test_reroute_is_announced_only_after_a_server_confirms(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "BLOSSOM_SERVER_INFO", {
        "good.example": _info(free_max_file=8),
        "mirror.example": _info(free_max_file=1024),
    })
    ctx = make_store(
        tmp_path, servers=(SERVER, MIRROR),
        replies=[json_reply(_desc_for(b"x" * 32, MIRROR), status=201)],
    )
    ctx.store.upload_bytes(b"x" * 32, name="a.png")
    assert ctx.rerouted == []            # nothing confirmed yet
    ctx.settle()
    assert ctx.rerouted == [("a.png", "good.example", "mirror.example")]


def test_a_failed_reroute_is_never_announced(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "BLOSSOM_SERVER_INFO", {
        "good.example": _info(free_max_file=8),
        "mirror.example": _info(free_max_file=1024),
    })
    ctx = make_store(tmp_path, servers=(SERVER, MIRROR),
                     replies=[error_reply(403)])
    ctx.store.upload_bytes(b"x" * 32, name="a.png")
    ctx.settle()
    assert ctx.rerouted == []
    assert len(ctx.failures) == 1


def test_no_reroute_when_the_primary_takes_the_file(tmp_path):
    ctx = make_store(tmp_path, servers=(SERVER, MIRROR),
                     responder=_two_server_responder())
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.settle()
    assert ctx.rerouted == []


# --------------------------------------------------------------------------- #
# BUD-12 pagination, driven from the store
# --------------------------------------------------------------------------- #

def _many(count: int):
    return [descriptor(f"{i:064x}", uploaded=1000 + i) for i in range(count)]


def _cursors(nam):
    out = []
    for _verb, request, _body in nam.calls:
        query = QUrlQuery(QUrl(request.url()).query())
        out.append(query.queryItemValue("cursor"))
    return out


def test_a_short_first_page_ends_the_walk(tmp_path):
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER, blobs=_many(5)))
    ctx.store.fetch()
    ctx.settle()
    assert len(ctx.nam.calls) == 1
    assert len(ctx.store.files) == 5


def test_a_full_page_asks_for_the_next_one_with_a_cursor(tmp_path):
    from nostr.blossom import store as store_module

    page = store_module._LIST_PAGE_SIZE
    blobs = _many(page + 3)
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER, blobs=blobs))
    ctx.store.fetch()
    ctx.nam.issued[0].finish()
    assert len(ctx.nam.calls) == 2

    # BUD-12: the cursor is the sha256 of the LAST blob of the previous
    # page, and the next page must not repeat it.
    first_page_last = f"{len(blobs) - page:064x}"
    assert _cursors(ctx.nam) == ["", first_page_last]
    query = QUrlQuery(QUrl(ctx.nam.calls[1][1].url()).query())
    assert query.queryItemValue("limit") == str(page)

    ctx.nam.issued[1].finish()
    assert len(ctx.store.files) == len(blobs)
    assert first_page_last not in [
        b["sha256"] for b in json.loads(bytes(ctx.nam.issued[1]._body))
    ]


def test_a_paged_walk_costs_exactly_one_signature(tmp_path):
    """BUD-11 scopes a list token to a server and gives it no ``x`` tag,
    so one token covers every page. Anything else prompts the user once
    per page."""
    from nostr.blossom import store as store_module

    blobs = _many(store_module._LIST_PAGE_SIZE + 3)
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER, blobs=blobs))
    ctx.store.fetch()
    ctx.nam.issued[0].finish()
    ctx.nam.issued[1].finish()
    assert len(ctx.nam.calls) == 2
    assert ctx.pool.calls == 1
    assert len(ctx.signer.requests) == 1


def test_a_server_that_ignores_the_cursor_stops_after_two_requests(tmp_path):
    """The guard that matters: an identical page forever would otherwise
    be an endless walk. It merges once and stops, with no error."""
    from nostr.blossom import store as store_module

    blobs = _many(store_module._LIST_PAGE_SIZE)
    ctx = make_store(
        tmp_path,
        responder=FakeBlossomServer(SERVER, blobs=blobs, ignore_cursor=True),
    )
    errors = []
    ctx.store.fetch_error.connect(errors.append)
    ctx.store.fetch()
    ctx.nam.issued[0].finish()
    ctx.nam.issued[1].finish()
    assert len(ctx.nam.calls) == 2
    assert errors == []
    assert len(ctx.store.files) == len(blobs)


def test_a_server_echoing_the_cursor_entry_stops(tmp_path):
    from nostr.blossom import store as store_module

    page = store_module._LIST_PAGE_SIZE
    first = _many(page)
    # Second page repeats the cursor entry and adds one novel blob, so
    # only the echo rule can end this walk.
    second = [first[0]] + [descriptor(f"{9000:064x}", uploaded=1)] * 1
    ctx = make_store(tmp_path, replies=[json_reply(first), json_reply(second)])
    ctx.store.fetch()
    ctx.nam.issued[0].finish()
    assert len(ctx.nam.calls) == 2
    ctx.nam.issued[1].finish()
    assert len(ctx.nam.calls) == 2


def test_the_page_cap_bounds_a_server_with_endless_novel_pages(tmp_path):
    from nostr.blossom import store as store_module

    page = store_module._LIST_PAGE_SIZE
    replies = [
        json_reply([descriptor(f"{p * page + i:064x}", uploaded=p * page + i)
                    for i in range(page)])
        for p in range(store_module._MAX_LIST_PAGES + 5)
    ]
    ctx = make_store(tmp_path, replies=replies)
    ctx.store.fetch()
    ctx.settle()
    assert len(ctx.nam.calls) == store_module._MAX_LIST_PAGES


# --------------------------------------------------------------------------- #
# The fetch merge race
# --------------------------------------------------------------------------- #

def test_an_upload_committed_during_a_fetch_survives_the_merge(tmp_path):
    """A ``/list`` response is authoritative about what the server has,
    and silent about what this process uploaded while it was in flight."""
    ctx = make_store(tmp_path, replies=[
        json_reply([]),                                  # the /list, held open
        json_reply(descriptor(), status=201),            # the upload
    ])
    ctx.store.fetch()
    assert len(ctx.nam.calls) == 1

    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.nam.issued[-1].finish()                          # upload commits first
    assert SHA in ctx.store.files

    ctx.nam.issued[0].finish()                           # then /list lands
    assert SHA in ctx.store.files


def test_a_blob_the_server_dropped_is_still_removed_by_a_fetch(tmp_path):
    """The other half of the same rule: the re-apply is only for records
    written after the walk began, so a real deletion elsewhere still
    disappears from the library."""
    stale = descriptor(OTHER_SHA)
    ctx = make_store(tmp_path, replies=[json_reply([stale]), json_reply([])])
    ctx.store.fetch()
    ctx.settle()
    assert OTHER_SHA in ctx.store.files

    ctx.store.fetch(force=True)
    ctx.nam.issued[1].finish()
    assert OTHER_SHA not in ctx.store.files


# --------------------------------------------------------------------------- #
# Seeding the local blob cache
# --------------------------------------------------------------------------- #

def _cache(tmp_path):
    """A real ``ThumbnailLoader`` on a temp directory, wired to a
    transport that would fail loudly if anything asked it to download."""
    nam = FakeNam()
    return ThumbnailLoader(cache_dir=tmp_path / "cache", nam=nam), nam


def test_an_uploaded_blob_resolves_from_the_cache_with_no_network(tmp_path):
    """The bytes were in memory a moment ago; fetching them back from a
    server to display them is a round trip that also fails offline."""
    cache, cache_nam = _cache(tmp_path)
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER),
                     blob_cache=cache)
    sha = hashlib.sha256(PNG_BYTES).hexdigest()

    ctx.store.upload_bytes(PNG_BYTES, name="a.png", mime_type="image/png")
    ctx.probe()
    ctx.settle()
    assert ctx.failures == []

    ready, failed = [], []
    cache.ready.connect(lambda s, _p, _pix: ready.append(s))
    cache.failed.connect(lambda s, r: failed.append((s, r)))
    cache.load(sha, f"{SERVER}/{sha}.png")

    assert cache_nam.calls == []
    assert ready == [sha]
    assert failed == []


def test_the_bytes_are_cached_before_anything_leaves_the_process(tmp_path):
    cache, _cache_nam = _cache(tmp_path)
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER),
                     blob_cache=cache)
    sha = hashlib.sha256(PNG_BYTES).hexdigest()

    ctx.store.upload_bytes(PNG_BYTES, name="a.png", mime_type="image/png")
    assert cache.has(sha)
    assert ctx.verbs() == ["head"]


def test_an_upload_refused_for_its_size_is_never_cached(tmp_path):
    """The size plan runs first, so nothing is written for a file that
    was never going anywhere."""
    cache, _cache_nam = _cache(tmp_path)
    ctx = make_store(tmp_path, blob_cache=cache)
    huge = b"x" * 16
    monkey = plan_module.get_effective_max_file
    try:
        plan_module.get_effective_max_file = lambda _s: 1
        ctx.store.upload_bytes(huge, name="huge.png")
    finally:
        plan_module.get_effective_max_file = monkey

    assert not cache.has(hashlib.sha256(huge).hexdigest())
    assert ctx.nam.calls == []
    assert ctx.failures


def test_a_cache_that_cannot_be_written_does_not_fail_the_upload(tmp_path):
    """Losing the local copy costs a download later. Losing the upload
    would cost the user their file."""

    class Broken:
        def put_bytes(self, data):
            raise OSError("read-only file system")

    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER),
                     blob_cache=Broken())
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.settle()
    assert ctx.failures == []
    assert ctx.finished[0][1].hash == SHA


def test_a_store_without_a_cache_still_uploads(tmp_path):
    ctx = make_store(tmp_path, responder=FakeBlossomServer(SERVER))
    ctx.store.upload_bytes(BODY, name="a.png")
    ctx.probe()
    ctx.settle()
    assert ctx.failures == []
    assert ctx.finished[0][1].hash == SHA


# --------------------------------------------------------------------------- #
# Servers this account is entitled to beyond the ones it configured
# --------------------------------------------------------------------------- #

E21 = "https://blossom.einundzwanzig.space"


def test_an_entitled_server_is_added_to_the_targets(tmp_path):
    ctx = make_store(tmp_path, entitled_servers=lambda: [E21])
    assert ctx.store._target_servers() == [SERVER, E21]


def test_an_entitled_server_never_becomes_the_primary(tmp_path):
    # A benefit must not quietly start receiving uploads ahead of the
    # server the user chose for themselves.
    ctx = make_store(tmp_path, entitled_servers=lambda: [E21])
    assert ctx.store._target_servers()[0] == SERVER


def test_no_entitlement_leaves_the_targets_exactly_as_configured(tmp_path):
    ctx = make_store(tmp_path)
    assert ctx.store._target_servers() == [SERVER]
    empty = make_store(tmp_path, entitled_servers=lambda: [])
    assert empty.store._target_servers() == [SERVER]


def test_an_already_configured_entitled_server_is_not_duplicated(tmp_path):
    ctx = make_store(tmp_path, servers=(SERVER, E21), entitled_servers=lambda: [E21 + "/"])
    assert ctx.store._target_servers() == [SERVER, E21]


def test_an_unusable_entitled_url_is_refused(tmp_path):
    ctx = make_store(tmp_path, entitled_servers=lambda: [
        "http://not-loopback.example", "file:///etc/passwd", "", "notaurl",
    ])
    assert ctx.store._target_servers() == [SERVER]


def test_a_failing_entitlement_lookup_does_not_break_the_library(tmp_path):
    # Losing a benefit is a small annoyance; losing the media library
    # because a membership check raised would not be.
    def boom():
        raise RuntimeError("roster unavailable")
    ctx = make_store(tmp_path, entitled_servers=boom)
    assert ctx.store._target_servers() == [SERVER]


def test_entitlement_is_resolved_per_call_not_cached(tmp_path):
    # Membership can be resolved after the store is built, so a value
    # captured at construction would leave a member without the benefit
    # for the rest of the session.
    state = {"servers": []}
    ctx = make_store(tmp_path, entitled_servers=lambda: state["servers"])
    assert ctx.store._target_servers() == [SERVER]
    state["servers"] = [E21]
    assert ctx.store._target_servers() == [SERVER, E21]
