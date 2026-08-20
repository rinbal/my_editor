# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""BUD-03 recovery: a dead blob URL is not a dead blob (ADR AD-13.2).

When the address stored for an image stops answering, the hash is still
good, so the sibling servers that hold the same blob are worth asking.
BUD-03 lines 71 to 74 describe exactly that ladder. These tests pin the
three things that make it safe rather than merely useful: it sends no
credential anywhere, it verifies the bytes it gets back, and it changes
nothing about the document or the record when it succeeds.

Offline throughout. The transport is ``FakeNam`` and the cache is a
``tmp_path`` directory, so no request leaves the process and no test
writes to a real home directory.
"""

from __future__ import annotations

import hashlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.blossom.hashes import blob_url
from nostr.media.assets import AssetIndex, AssetState, DocumentAsset
from nostr.media.manager import AssetManager
from nostr.ui.thumbnail_loader import ThumbnailLoader

from tests.blossom_fakes import FakeNam
from tests.media_fakes import (
    PNG_BYTES,
    PNG_SHA,
    FakeBlobStore,
    FakeUploader,
    fake_decoder,
)


DEAD = "https://dead.example"
CONFIRMED = "https://confirmed.example"
CONFIGURED = "https://configured.example"
PUBLISHED = "https://published.example"

DEAD_URL = blob_url(DEAD, PNG_SHA)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def make_manager(tmp_path, *, provider=None, servers=(), store=None):
    """A manager holding one uploaded asset whose URL is about to die."""
    blob_store = store if store is not None else FakeBlobStore(tmp_path / "cache")
    index = AssetIndex(path=tmp_path / "media_assets.json")
    asset = DocumentAsset(
        sha256=PNG_SHA,
        mime="image/png",
        size=len(PNG_BYTES),
        upload_state=AssetState.COMPLETE,
        remote_url=DEAD_URL,
        servers=list(servers),
    )
    index.put(asset)
    manager = AssetManager(
        blob_store=blob_store,
        uploader=FakeUploader(),
        profile_provider=lambda: object(),
        decoder=fake_decoder,
        index=index,
        recovery_provider=provider,
    )
    return SimpleNamespace(
        manager=manager, store=blob_store, index=index, asset=asset
    )


def provider_of(*origins):
    """A recovery provider that records what it was asked about."""
    asked: list = []

    def _provide(sha256: str):
        asked.append(sha256)
        return list(origins)

    _provide.asked = asked
    return _provide


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #

def test_confirmed_servers_come_before_the_provider(tmp_path):
    # Trust order: servers that confirmed this blob, then the user's own
    # configuration, then whatever the author published.
    ctx = make_manager(
        tmp_path,
        servers=[CONFIRMED],
        provider=provider_of(CONFIGURED, PUBLISHED),
    )
    ctx.store.refuse(PNG_SHA)
    ctx.store.refuse(PNG_SHA)
    ctx.store.refuse(PNG_SHA)

    assert [url for _sha, url in ctx.store.loads] == [
        blob_url(CONFIRMED, PNG_SHA),
        blob_url(CONFIGURED, PNG_SHA),
        blob_url(PUBLISHED, PNG_SHA),
    ]


def test_the_ladder_is_sequential(tmp_path):
    # One request at a time. A fan-out would be a load problem for the
    # servers and a privacy problem for the user.
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED, PUBLISHED))
    ctx.store.refuse(PNG_SHA)
    assert len(ctx.store.loads) == 1
    ctx.store.refuse(PNG_SHA)
    assert len(ctx.store.loads) == 2


def test_a_successful_candidate_ends_the_ladder(tmp_path):
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED, PUBLISHED))
    ctx.store.refuse(PNG_SHA)
    ctx.store.deliver(PNG_SHA, PNG_BYTES)
    assert len(ctx.store.loads) == 1


def test_the_dead_url_is_never_retried(tmp_path):
    # It just failed, and it is candidate one of the BUD-03 ladder.
    ctx = make_manager(tmp_path, servers=[DEAD], provider=provider_of(DEAD))
    ctx.store.refuse(PNG_SHA)
    assert ctx.store.loads == []


def test_a_candidate_the_media_policy_refuses_is_never_requested(tmp_path):
    ctx = make_manager(
        tmp_path,
        provider=provider_of("http://public.example", "file:///etc", CONFIGURED),
    )
    ctx.store.refuse(PNG_SHA)
    assert [url for _sha, url in ctx.store.loads] == [blob_url(CONFIGURED, PNG_SHA)]


def test_at_most_four_candidates_are_tried(tmp_path):
    ctx = make_manager(
        tmp_path,
        provider=provider_of(*[f"https://s{i}.example" for i in range(9)]),
    )
    for _ in range(9):
        ctx.store.refuse(PNG_SHA)
    assert len(ctx.store.loads) == 4


def test_the_ladder_runs_once_per_hash_per_session(tmp_path):
    # A repaint asks for every unresolved image again, so a negative
    # outcome has to be remembered or one broken picture re-issues its
    # whole ladder on every keystroke.
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    ctx.store.refuse(PNG_SHA)
    ctx.store.refuse(PNG_SHA)
    assert len(ctx.store.loads) == 1

    ctx.manager.resolve_image(ctx.asset.key)
    ctx.store.refuse(PNG_SHA)
    assert [url for _sha, url in ctx.store.loads] == [
        blob_url(CONFIGURED, PNG_SHA),
        DEAD_URL,
    ]


def test_nothing_is_requested_past_the_last_known_candidate(tmp_path):
    # BUD-03 step 4, falling back to a well-known popular server, is
    # declined: it broadcasts a hash the user is interested in to a
    # server neither party named, and the spec makes it a MAY.
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    ctx.store.refuse(PNG_SHA)
    ctx.store.refuse(PNG_SHA)
    assert len(ctx.store.loads) == 1


def test_a_blob_that_is_already_cached_starts_no_ladder(tmp_path):
    # "Not an image" is a rendering failure, not a missing blob, and
    # content addressing means every sibling holds the same bytes.
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    ctx.store.cache_path(PNG_SHA).write_bytes(PNG_BYTES)
    ctx.store.refuse(PNG_SHA, "not an image")
    assert ctx.store.loads == []


def test_an_asset_with_no_remote_url_starts_no_ladder(tmp_path):
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    ctx.asset.remote_url = ""
    ctx.store.refuse(PNG_SHA)
    assert ctx.store.loads == []


def test_a_provider_that_raises_cannot_break_resolution(tmp_path):
    def _boom(_sha):
        raise RuntimeError("relay went away")

    ctx = make_manager(tmp_path, servers=[CONFIRMED], provider=_boom)
    ctx.store.refuse(PNG_SHA)
    assert [url for _sha, url in ctx.store.loads] == [blob_url(CONFIRMED, PNG_SHA)]


def test_without_a_provider_nothing_changes(tmp_path):
    # The seam is opt in: until the policy object is wired up in the
    # window, a failed blob behaves exactly as it did before.
    ctx = make_manager(tmp_path, servers=[CONFIRMED], provider=None)
    ctx.store.refuse(PNG_SHA)
    assert ctx.store.loads == []
    assert ctx.manager._fetching == set()


# --------------------------------------------------------------------------- #
# Recovery is read only
# --------------------------------------------------------------------------- #

def test_recovery_never_repoints_the_stored_url(tmp_path):
    # ``remote_url`` is what the save path and the publish path
    # serialize. Repointing it at whichever server happened to answer
    # would silently rewrite the user's file and their next published
    # event, which is the rewrite AD-4 exists to prevent.
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    ctx.store.refuse(PNG_SHA)
    ctx.store.deliver(PNG_SHA, PNG_BYTES)

    assert ctx.asset.remote_url == DEAD_URL
    assert ctx.index.get(PNG_SHA).remote_url == DEAD_URL


def test_recovery_does_not_append_to_the_confirmed_servers(tmp_path):
    # ``servers`` is what imeta fallbacks are built from, so a server
    # that merely served a copy must not end up claimed as a mirror.
    ctx = make_manager(tmp_path, servers=[CONFIRMED], provider=provider_of(PUBLISHED))
    ctx.store.refuse(PNG_SHA)
    ctx.store.refuse(PNG_SHA)
    ctx.store.deliver(PNG_SHA, PNG_BYTES)
    assert ctx.asset.servers == [CONFIRMED]


def test_a_failed_ladder_leaves_the_asset_untouched(tmp_path):
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    before = ctx.asset.to_record()
    ctx.store.refuse(PNG_SHA)
    ctx.store.refuse(PNG_SHA)
    assert ctx.index.get(PNG_SHA).to_record() == before


def test_a_recovered_asset_still_serializes_the_original_reference(tmp_path):
    ctx = make_manager(tmp_path, provider=provider_of(CONFIGURED))
    ctx.store.refuse(PNG_SHA)
    ctx.store.deliver(PNG_SHA, PNG_BYTES)
    view = ctx.manager.export_view(ctx.asset.key)
    assert view.remote_url == DEAD_URL
    assert view.data == PNG_BYTES


# --------------------------------------------------------------------------- #
# On the wire: no credential, and the bytes have to hash right
# --------------------------------------------------------------------------- #

def wired(tmp_path, *, provider):
    """The real loader on a fake transport, driving a real manager."""
    nam = FakeNam()
    loader = ThumbnailLoader(cache_dir=tmp_path / "cache", nam=nam)
    ctx = make_manager(tmp_path, provider=provider, store=loader)
    return SimpleNamespace(nam=nam, loader=loader, manager=ctx.manager,
                           index=ctx.index, asset=ctx.asset)


def test_no_recovery_request_carries_an_authorization_header(tmp_path):
    # A recovery GET is a plain content fetch, and the ladder contacts
    # servers the user never chose. The bytes are trusted because they
    # hash to the name that was asked for, never because of who served
    # them, so there is nothing for a credential to buy here (I2).
    ctx = wired(tmp_path, provider=provider_of(CONFIGURED, PUBLISHED))
    ctx.loader.load(PNG_SHA, DEAD_URL)
    ctx.nam.issued[0].finish()          # the stored URL is gone
    ctx.nam.issued[1].finish()          # first candidate refuses too

    assert len(ctx.nam.calls) == 3
    for _verb, request, _body in ctx.nam.calls:
        assert not request.hasRawHeader("Authorization")
        assert bytes(request.rawHeader("Authorization")) == b""


def test_a_recovered_blob_whose_bytes_hash_wrong_is_not_adopted(tmp_path):
    ctx = wired(tmp_path, provider=provider_of(CONFIGURED))
    ctx.loader.load(PNG_SHA, DEAD_URL)
    ctx.nam.issued[0].finish()

    impostor = b"not the picture that was asked for"
    assert hashlib.sha256(impostor).hexdigest() != PNG_SHA
    ctx.nam.issued[1]._body = impostor
    ctx.nam.issued[1].finish()

    assert not ctx.loader.has(PNG_SHA)
    assert ctx.manager.resolve_bytes(ctx.asset.key) is None
    assert ctx.index.get(PNG_SHA).remote_url == DEAD_URL


def test_a_recovered_blob_that_hashes_right_is_cached(tmp_path):
    ctx = wired(tmp_path, provider=provider_of(CONFIGURED))
    ctx.loader.load(PNG_SHA, DEAD_URL)
    ctx.nam.issued[0].finish()
    ctx.nam.issued[1]._body = PNG_BYTES
    ctx.nam.issued[1].finish()

    assert ctx.loader.has(PNG_SHA)
    assert ctx.manager.resolve_bytes(ctx.asset.key) == PNG_BYTES
    assert ctx.nam.calls[1][1].url().toString() == blob_url(CONFIGURED, PNG_SHA)


def test_the_recovery_request_asks_for_the_canonical_address(tmp_path):
    ctx = wired(tmp_path, provider=provider_of(CONFIGURED))
    ctx.loader.load(PNG_SHA, DEAD_URL)
    ctx.nam.issued[0].finish()
    assert ctx.nam.calls[1][1].url().toString() == f"{CONFIGURED}/{PNG_SHA}"
