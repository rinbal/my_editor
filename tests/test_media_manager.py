# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.media.manager.AssetManager``.

The manager is the contract the editor leans on: an image is in the
document the moment its bytes are hashed, and nothing a server, a
signer or a network does afterwards may take it away. These tests drive
the whole lifecycle on fakes, so no network, relay, signer or real home
directory is involved.
"""

from __future__ import annotations

import itertools
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.media import manager as manager_module
from nostr.media.assets import (
    LEGAL_TRANSITIONS,
    AssetIndex,
    AssetState,
    DocumentAsset,
    asset_key,
)
from nostr.media.manager import AssetErrorCodes, AssetManager

from tests.media_fakes import (
    GIF_BYTES,
    GIF_SHA,
    PNG_BYTES,
    PNG_SHA,
    SVG_BYTES,
    TEXT_BYTES,
    FakeBlobStore,
    FakeUploader,
    fake_decoder,
    make_media,
    sha_of,
)


PNG_URL = f"https://cdn.example/{PNG_SHA}"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class Recorder:
    """Every signal the manager emits, in order."""

    def __init__(self, manager: AssetManager) -> None:
        self.added: list = []
        self.changed: list = []
        self.failed: list = []
        manager.asset_added.connect(self.added.append)
        manager.asset_changed.connect(self.changed.append)
        manager.asset_upload_failed.connect(
            lambda sha, code: self.failed.append((sha, code))
        )


def make_manager(tmp_path, *, store=None, decoder=fake_decoder, profile=object()):
    blob_store = store if store is not None else FakeBlobStore(tmp_path / "cache")
    uploader = FakeUploader()
    index = AssetIndex(path=tmp_path / "media_assets.json")
    manager = AssetManager(
        blob_store=blob_store,
        uploader=uploader,
        profile_provider=lambda: profile,
        decoder=decoder,
        index=index,
    )
    return SimpleNamespace(
        manager=manager,
        store=blob_store,
        uploader=uploader,
        index=index,
        signals=Recorder(manager),
    )


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #

def _asset_in(ctx, state: AssetState) -> DocumentAsset:
    asset = DocumentAsset(sha256=PNG_SHA, upload_state=state)
    ctx.index.put(asset)
    return asset


def test_every_legal_transition_is_allowed(tmp_path):
    for source, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            ctx = make_manager(tmp_path)
            asset = _asset_in(ctx, source)
            assert ctx.manager._set_state(asset, target) is True, (source, target)
            assert asset.upload_state is target
            assert ctx.signals.changed == [PNG_SHA]


def test_illegal_transitions_are_silent_no_ops(tmp_path):
    for source, target in itertools.product(AssetState, AssetState):
        if source is target or target in LEGAL_TRANSITIONS[source]:
            continue
        ctx = make_manager(tmp_path)
        asset = _asset_in(ctx, source)
        assert ctx.manager._set_state(asset, target) is False, (source, target)
        assert asset.upload_state is source
        assert ctx.signals.changed == []
        assert ctx.signals.failed == []


def test_mirrored_never_fails(tmp_path):
    ctx = make_manager(tmp_path)
    asset = _asset_in(ctx, AssetState.MIRRORED)
    assert ctx.manager._set_state(asset, AssetState.FAILED) is False
    assert asset.upload_state is AssetState.MIRRORED


def test_transition_to_the_same_state_is_a_no_op(tmp_path):
    ctx = make_manager(tmp_path)
    asset = _asset_in(ctx, AssetState.QUEUED)
    assert ctx.manager._set_state(asset, AssetState.QUEUED) is False
    assert ctx.signals.changed == []


def test_complete_is_terminal(tmp_path):
    ctx = make_manager(tmp_path)
    for target in AssetState:
        asset = _asset_in(ctx, AssetState.COMPLETE)
        assert ctx.manager._set_state(asset, target) is False
        assert asset.upload_state is AssetState.COMPLETE


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #

def test_adopt_bytes_creates_a_local_asset(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture", caption="hi")

    assert asset is not None
    assert asset.sha256 == PNG_SHA
    assert asset.key == asset_key(PNG_SHA)
    assert asset.mime == "image/png"
    assert asset.size == len(PNG_BYTES)
    assert asset.alt == "a picture"
    assert asset.caption == "hi"
    assert (asset.width, asset.height) == (3, 2)
    assert asset.upload_state is AssetState.LOCAL
    assert asset.remote_url == ""
    assert asset.created_at > 0 and asset.updated_at > 0
    assert ctx.store.puts == [PNG_SHA]
    assert ctx.store.cache_path(PNG_SHA).read_bytes() == PNG_BYTES
    assert ctx.signals.added == [PNG_SHA]
    assert PNG_SHA in ctx.manager


def test_adopt_ignores_the_callers_mime_hint(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, mime="image/jpeg")
    assert asset.mime == "image/png"


def test_adopt_without_a_decoder_skips_dimensions(tmp_path):
    ctx = make_manager(tmp_path, decoder=None)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    assert (asset.width, asset.height) == (0, 0)
    assert asset.upload_state is AssetState.LOCAL


def test_adopt_dedupes_by_hash(tmp_path):
    ctx = make_manager(tmp_path)
    first = ctx.manager.adopt_bytes(PNG_BYTES, alt="first")
    ctx.manager.request_upload(PNG_SHA)
    ctx.uploader.finish(first.key, make_media(PNG_SHA))
    assert first.upload_state is AssetState.COMPLETE

    second = ctx.manager.adopt_bytes(PNG_BYTES, alt="second")
    assert second is first
    assert second.alt == "first"                     # a set alt is never replaced
    assert second.upload_state is AssetState.COMPLETE
    assert second.remote_url == PNG_URL
    assert ctx.store.puts == [PNG_SHA]               # one write, one record
    assert len(ctx.index) == 1
    assert ctx.signals.added == [PNG_SHA]


def test_adopt_fills_only_empty_alt_and_caption(tmp_path):
    ctx = make_manager(tmp_path)
    ctx.manager.adopt_bytes(PNG_BYTES)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="late alt", caption="late caption")
    assert asset.alt == "late alt"
    assert asset.caption == "late caption"


def test_adopt_restores_bytes_a_cleared_cache_lost(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.store.evict(PNG_SHA)
    assert ctx.manager.resolve_bytes(asset.key) is None

    ctx.manager.adopt_bytes(PNG_BYTES)
    assert ctx.manager.resolve_bytes(asset.key) == PNG_BYTES


@pytest.mark.parametrize("data", [b"", SVG_BYTES, TEXT_BYTES, b"\x00\x01\x02"])
def test_adopt_rejects_unsupported_bytes(tmp_path, data):
    ctx = make_manager(tmp_path)
    assert ctx.manager.adopt_bytes(data) is None
    assert ctx.store.puts == []
    assert ctx.signals.added == []


def test_adopt_rejects_oversize_data(tmp_path, monkeypatch):
    assert manager_module.MAX_ASSET_BYTES == 25 * 1024 * 1024
    monkeypatch.setattr(manager_module, "MAX_ASSET_BYTES", 10)
    ctx = make_manager(tmp_path)
    assert ctx.manager.adopt_bytes(PNG_BYTES) is None
    assert ctx.store.puts == []


def test_adopt_survives_an_unwritable_cache(tmp_path):
    ctx = make_manager(tmp_path)
    ctx.store.put_error = OSError("cache is read only")
    assert ctx.manager.adopt_bytes(PNG_BYTES) is None
    assert len(ctx.index) == 0
    assert ctx.signals.added == []


def test_adopt_file_reads_the_bytes_verbatim(tmp_path):
    ctx = make_manager(tmp_path)
    source = tmp_path / "holiday snap.png"
    source.write_bytes(PNG_BYTES)

    asset = ctx.manager.adopt_file(str(source), alt="holiday snap")
    assert asset.sha256 == PNG_SHA
    assert ctx.store.cache_path(PNG_SHA).read_bytes() == PNG_BYTES
    assert asset.alt == "holiday snap"


def test_adopt_file_refuses_a_missing_path(tmp_path):
    ctx = make_manager(tmp_path)
    assert ctx.manager.adopt_file(str(tmp_path / "nothing.png")) is None


def test_adopt_data_uri_returns_the_key(tmp_path):
    import base64

    ctx = make_manager(tmp_path)
    uri = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
    assert ctx.manager.adopt_data_uri(uri) == asset_key(PNG_SHA)
    assert ctx.manager.resolve_bytes(asset_key(PNG_SHA)) == PNG_BYTES


@pytest.mark.parametrize("uri", [
    "",
    "not a uri",
    "https://cdn.example/a.png",
    "data:image/png,notbase64",
    "data:text/plain;base64,aGVsbG8=",     # decodes, but is not an image
    None,
])
def test_adopt_data_uri_refuses_anything_else(tmp_path, uri):
    ctx = make_manager(tmp_path)
    assert ctx.manager.adopt_data_uri(uri) is None


def test_adopt_library_file_enters_complete(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_library_file(
        sha256=PNG_SHA, remote_url=PNG_URL, mime="image/png", size=99, alt="from library"
    )
    assert asset.upload_state is AssetState.COMPLETE
    assert asset.is_uploaded
    assert asset.remote_url == PNG_URL
    assert asset.size == 99
    assert ctx.signals.added == [PNG_SHA]
    assert ctx.uploader.calls == []


def test_adopt_library_file_promotes_a_local_asset(tmp_path):
    ctx = make_manager(tmp_path)
    local = ctx.manager.adopt_bytes(PNG_BYTES)
    again = ctx.manager.adopt_library_file(
        sha256=PNG_SHA, remote_url=PNG_URL, mime="image/png"
    )
    assert again is local
    assert local.upload_state is AssetState.COMPLETE
    assert local.remote_url == PNG_URL


@pytest.mark.parametrize("sha, url", [
    ("nope", PNG_URL),
    (PNG_SHA, "file:///etc/passwd"),
    (PNG_SHA, f"http://cdn.example/{PNG_SHA}"),
    (PNG_SHA, f"https://cdn.example/{GIF_SHA}"),
    (PNG_SHA, ""),
])
def test_adopt_library_file_refuses_bad_input(tmp_path, sha, url):
    ctx = make_manager(tmp_path)
    assert ctx.manager.adopt_library_file(sha256=sha, remote_url=url) is None
    assert len(ctx.index) == 0


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

class ExplodingStore(FakeBlobStore):
    """Proves a name that is not an asset key never reaches the disk."""

    def cache_path(self, sha256):
        raise AssertionError("the filesystem must not be reached")

    def has(self, sha256):
        raise AssertionError("the filesystem must not be reached")


@pytest.mark.parametrize("name", [
    "myeditor-asset:../../etc/passwd",
    "myeditor-asset:" + "A" * 64,
    "/etc/passwd",
    "https://cdn.example/a.png",
    "",
])
def test_hostile_names_never_reach_the_filesystem(tmp_path, name):
    ctx = make_manager(tmp_path, store=ExplodingStore(tmp_path / "cache"))
    assert ctx.manager.resolve_bytes(name) is None
    assert ctx.manager.resolve_image(name) is None
    assert ctx.manager.export_view(name) is None


def test_resolve_bytes_and_image_use_the_cache(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    assert ctx.manager.resolve_bytes(asset.key) == PNG_BYTES
    image = ctx.manager.resolve_image(asset.key)
    assert (image.width(), image.height()) == (3, 2)
    assert ctx.store.loads == []


def test_resolve_image_without_a_decoder_returns_none(tmp_path):
    ctx = make_manager(tmp_path, decoder=None)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    assert ctx.manager.resolve_image(asset.key) is None


def test_resolve_image_fetches_a_missing_blob_once(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_library_file(
        sha256=PNG_SHA, remote_url=PNG_URL, mime="image/png"
    )
    assert ctx.manager.resolve_image(asset.key) is None
    assert ctx.manager.resolve_image(asset.key) is None
    assert ctx.store.loads == [(PNG_SHA, PNG_URL)]

    ctx.store.deliver(PNG_SHA, PNG_BYTES)
    assert (asset.width, asset.height) == (3, 2)
    assert ctx.signals.changed[-1] == PNG_SHA
    assert ctx.manager.resolve_image(asset.key) is not None
    assert len(ctx.store.loads) == 1


def test_a_failed_fetch_can_be_retried(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_library_file(
        sha256=PNG_SHA, remote_url=PNG_URL, mime="image/png"
    )
    ctx.manager.resolve_image(asset.key)
    ctx.store.refuse(PNG_SHA, "network error")
    assert asset.upload_state is AssetState.COMPLETE     # nothing destructive
    assert asset.remote_url == PNG_URL

    ctx.manager.resolve_image(asset.key)
    assert len(ctx.store.loads) == 2


def test_blob_signals_for_other_fetches_are_ignored(tmp_path):
    ctx = make_manager(tmp_path)
    ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.store.ready.emit(PNG_SHA, "/somewhere", None)
    assert ctx.signals.changed == []


def test_export_view_carries_what_an_exporter_needs(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture")
    ctx.manager.request_upload(PNG_SHA)
    ctx.uploader.finish(asset.key, make_media(PNG_SHA))

    view = ctx.manager.export_view(asset.key)
    assert view.sha256 == PNG_SHA
    assert view.data == PNG_BYTES
    assert view.mime == "image/png"
    assert view.remote_url == PNG_URL
    assert view.alt == "a picture"
    assert (view.width, view.height) == (3, 2)


def test_export_view_reports_empty_data_on_a_cache_miss(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.store.evict(PNG_SHA)
    view = ctx.manager.export_view(asset.key)
    assert view.data == b""
    assert view.mime == "image/png"


def test_unuploaded_of_reports_blocked_keys_once(tmp_path):
    ctx = make_manager(tmp_path)
    local = ctx.manager.adopt_bytes(PNG_BYTES)
    hosted = ctx.manager.adopt_library_file(
        sha256=GIF_SHA, remote_url=f"https://cdn.example/{GIF_SHA}", mime="image/gif"
    )
    unknown = asset_key("ee" * 32)

    blocked = ctx.manager.unuploaded_of([
        local.key, local.key, hosted.key, unknown,
        "https://cdn.example/foreign.png", "/tmp/local.png",
    ])
    assert blocked == [local.key, unknown]


def test_display_label_falls_back_to_a_short_hash(tmp_path):
    ctx = make_manager(tmp_path)
    ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture")
    assert ctx.manager.display_label(PNG_SHA) == "a picture"
    assert ctx.manager.display_label(GIF_SHA) == GIF_SHA[:12]


def test_can_upload_reflects_the_profile(tmp_path):
    ctx = make_manager(tmp_path)
    assert ctx.manager.can_upload() is True
    offline = make_manager(tmp_path / "offline", profile=None)
    assert offline.manager.can_upload() is False


# --------------------------------------------------------------------------- #
# The upload queue
# --------------------------------------------------------------------------- #

def test_upload_happy_path(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture")

    ctx.manager.request_upload(PNG_SHA)
    assert asset.upload_state is AssetState.SIGNING
    assert len(ctx.uploader.calls) == 1
    call = ctx.uploader.calls[0]
    assert call.name == asset.key            # never a basename, so no collision
    assert call.mime_type == "image/png"
    assert call.body == PNG_BYTES

    ctx.uploader.status(asset.key, "uploading")
    assert asset.upload_state is AssetState.UPLOADING

    media = make_media(PNG_SHA, servers=("https://cdn.example", "https://mirror.example"))
    ctx.uploader.finish(asset.key, media)

    assert asset.upload_state is AssetState.COMPLETE
    assert asset.remote_url == PNG_URL
    assert asset.servers == ["https://cdn.example", "https://mirror.example"]
    assert asset.is_uploaded
    assert ctx.signals.changed[-1] == PNG_SHA
    assert ctx.signals.failed == []
    assert ctx.manager._inflight is None
    assert list(ctx.manager._queue) == []


def test_upload_completes_without_an_uploading_status(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(PNG_SHA)
    ctx.uploader.finish(asset.key, make_media(PNG_SHA))
    assert asset.upload_state is AssetState.COMPLETE


def test_library_dedup_skips_the_upload_entirely(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.uploader.files[PNG_SHA] = make_media(PNG_SHA)

    ctx.manager.request_upload(PNG_SHA)
    assert ctx.uploader.calls == []
    assert asset.upload_state is AssetState.COMPLETE
    assert asset.remote_url == PNG_URL
    assert asset.servers == ["https://cdn.example"]
    assert ctx.signals.changed[-1] == PNG_SHA


def test_library_dedup_ignores_an_unusable_url(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.uploader.files[PNG_SHA] = make_media(PNG_SHA, url="file:///etc/passwd")

    ctx.manager.request_upload(PNG_SHA)
    assert len(ctx.uploader.calls) == 1
    assert asset.upload_state is AssetState.SIGNING
    assert asset.remote_url == ""


@pytest.mark.parametrize("state", [
    AssetState.QUEUED, AssetState.SIGNING, AssetState.UPLOADING,
    AssetState.MIRRORED, AssetState.COMPLETE,
])
def test_request_upload_ignores_states_that_are_not_eligible(tmp_path, state):
    ctx = make_manager(tmp_path)
    asset = _asset_in(ctx, state)
    ctx.manager.request_upload(asset.sha256)
    assert ctx.uploader.calls == []
    assert asset.upload_state is state


def test_request_upload_ignores_an_unknown_hash(tmp_path):
    ctx = make_manager(tmp_path)
    ctx.manager.request_upload("ff" * 32)
    assert ctx.uploader.calls == []


def test_single_flight_uploads_run_strictly_in_order(tmp_path):
    ctx = make_manager(tmp_path)
    first = ctx.manager.adopt_bytes(PNG_BYTES)
    second = ctx.manager.adopt_bytes(GIF_BYTES)

    ctx.manager.request_upload(first.sha256)
    ctx.manager.request_upload(second.sha256)
    assert [c.name for c in ctx.uploader.calls] == [first.key]
    assert second.upload_state is AssetState.QUEUED

    ctx.uploader.finish(first.key, make_media(first.sha256))
    assert [c.name for c in ctx.uploader.calls] == [first.key, second.key]
    assert second.upload_state is AssetState.SIGNING


def test_signer_failures_are_classified(tmp_path):
    for reason in ("signer rejected the Blossom auth event: user said no",
                   "Connect a Nostr signer first."):
        ctx = make_manager(tmp_path)
        asset = ctx.manager.adopt_bytes(PNG_BYTES)
        ctx.manager.request_upload(asset.sha256)
        ctx.uploader.fail(asset.key, reason)
        assert asset.failure_code == AssetErrorCodes.SIGNER_REJECTED
        assert ctx.signals.failed == [(PNG_SHA, AssetErrorCodes.SIGNER_REJECTED)]


def test_other_failures_are_upload_failures(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.fail(asset.key, "Host unreachable (HTTP 0)")
    assert asset.failure_code == AssetErrorCodes.UPLOAD_FAILED
    assert ctx.signals.failed == [(PNG_SHA, AssetErrorCodes.UPLOAD_FAILED)]


def test_failure_keeps_every_identity_field(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture", caption="a caption")
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.fail(asset.key, "server said no")

    assert asset.upload_state is AssetState.FAILED
    assert asset.can_retry
    assert asset.sha256 == PNG_SHA
    assert asset.mime == "image/png"
    assert asset.size == len(PNG_BYTES)
    assert asset.alt == "a picture"
    assert asset.caption == "a caption"
    assert (asset.width, asset.height) == (3, 2)
    assert asset.failure_reason == "server said no"
    assert ctx.manager.resolve_bytes(asset.key) == PNG_BYTES   # bytes still there


def test_a_failure_stops_the_queue_without_stranding_it(tmp_path):
    ctx = make_manager(tmp_path)
    first = ctx.manager.adopt_bytes(PNG_BYTES)
    second = ctx.manager.adopt_bytes(GIF_BYTES)
    ctx.manager.request_upload(first.sha256)
    ctx.manager.request_upload(second.sha256)

    ctx.uploader.fail(first.key, "server said no")
    assert list(ctx.manager._queue) == []
    assert ctx.manager._inflight is None
    assert len(ctx.uploader.calls) == 1               # no second signer prompt
    assert second.upload_state is AssetState.FAILED
    assert second.can_retry
    assert ctx.signals.failed == [(first.sha256, AssetErrorCodes.UPLOAD_FAILED)]

    ctx.manager.retry_all_failed()
    assert [c.name for c in ctx.uploader.calls] == [first.key, first.key]
    assert second.upload_state is AssetState.QUEUED


def test_retry_reuses_the_cached_bytes(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.fail(asset.key, "server said no")

    ctx.manager.retry(asset.sha256)
    assert asset.upload_state is AssetState.SIGNING
    assert asset.attempts == 1
    assert asset.failure_code == ""
    assert ctx.store.puts == [PNG_SHA]                 # nothing was written again
    assert ctx.uploader.calls[-1].body == PNG_BYTES

    ctx.uploader.finish(asset.key, make_media(PNG_SHA))
    assert asset.upload_state is AssetState.COMPLETE


def test_retry_ignores_assets_that_did_not_fail(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.retry(asset.sha256)
    assert ctx.uploader.calls == []
    assert asset.upload_state is AssetState.LOCAL


def test_a_late_callback_after_a_failure_is_ignored(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.fail(asset.key, "server said no")

    ctx.uploader.finish(asset.key, make_media(PNG_SHA))
    assert asset.upload_state is AssetState.FAILED
    assert asset.remote_url == ""


def test_a_superseded_attempt_cannot_complete_the_asset(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.fail(asset.key, "server said no")
    ctx.manager.retry(asset.sha256)

    # The job the retry replaced answers late, still carrying its own
    # attempt number.
    ctx.manager._jobs[asset.key] = (asset.sha256, asset.attempts - 1)
    ctx.uploader.finish(asset.key, make_media(PNG_SHA))
    assert asset.upload_state is AssetState.SIGNING
    assert asset.remote_url == ""


def test_uploads_this_manager_did_not_start_are_ignored(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.uploader.status("holiday snap.png", "uploading")
    ctx.uploader.finish("holiday snap.png", make_media(PNG_SHA))
    ctx.uploader.fail("holiday snap.png", "server said no")

    assert asset.upload_state is AssetState.LOCAL
    assert ctx.signals.failed == []
    assert ctx.signals.changed == []


def test_a_hostile_finished_url_never_enters_the_index(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.finish(asset.key, make_media(PNG_SHA, url="file:///etc/passwd"))

    assert asset.remote_url == ""
    assert asset.upload_state is AssetState.FAILED
    assert ctx.signals.failed == [(PNG_SHA, AssetErrorCodes.UPLOAD_FAILED)]
    assert ctx.manager._inflight is None


def test_a_finished_url_for_another_hash_is_refused(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.finish(asset.key, make_media(PNG_SHA, url=f"https://cdn.example/{GIF_SHA}"))
    assert asset.upload_state is AssetState.FAILED
    assert asset.remote_url == ""


def test_a_synchronous_failure_leaves_the_manager_consistent(tmp_path):
    ctx = make_manager(tmp_path)
    ctx.uploader.fail_synchronously = "Connect a Nostr signer first."
    asset = ctx.manager.adopt_bytes(PNG_BYTES)

    ctx.manager.request_upload(asset.sha256)
    assert asset.upload_state is AssetState.FAILED
    assert asset.failure_code == AssetErrorCodes.SIGNER_REJECTED
    assert ctx.manager._inflight is None
    assert list(ctx.manager._queue) == []
    assert ctx.manager._jobs == {}
    assert ctx.manager.resolve_bytes(asset.key) == PNG_BYTES


def test_missing_cached_bytes_fail_the_job_and_stop_the_queue(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES)
    ctx.store.evict(PNG_SHA)

    ctx.manager.request_upload(asset.sha256)
    assert ctx.uploader.calls == []
    assert asset.upload_state is AssetState.FAILED
    assert asset.failure_reason == "cached bytes missing"
    assert ctx.signals.failed == [(PNG_SHA, AssetErrorCodes.UPLOAD_FAILED)]
    assert ctx.manager._inflight is None


def test_flush_persists_the_index(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture")
    ctx.manager.flush()

    reloaded = AssetIndex(path=tmp_path / "media_assets.json").get(asset.sha256)
    assert reloaded is not None
    assert reloaded.alt == "a picture"
    assert reloaded.upload_state is AssetState.LOCAL


def test_state_survives_a_restart(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(PNG_BYTES, alt="a picture")
    ctx.manager.request_upload(asset.sha256)
    ctx.uploader.finish(asset.key, make_media(PNG_SHA))
    ctx.manager.flush()

    later = make_manager(tmp_path)
    restored = later.manager.get(PNG_SHA)
    assert restored.is_uploaded
    assert restored.remote_url == PNG_URL
    assert restored.servers == ["https://cdn.example"]
    assert later.manager.export_view(asset_key(PNG_SHA)).data == PNG_BYTES


def test_error_codes_are_plain_strings(tmp_path):
    # They cross signal boundaries and settle in the index, so they must
    # stay comparable to the copy in nostr/blossom/errors.py.
    assert AssetErrorCodes.SIGNER_REJECTED == "SIGNER_REJECTED"
    assert AssetErrorCodes.UPLOAD_FAILED == "UPLOAD_FAILED"


def test_gif_bytes_are_adoptable(tmp_path):
    ctx = make_manager(tmp_path)
    asset = ctx.manager.adopt_bytes(GIF_BYTES)
    assert asset.mime == "image/gif"
    assert asset.sha256 == sha_of(GIF_BYTES) == GIF_SHA
