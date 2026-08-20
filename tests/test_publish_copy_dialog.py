# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the last thing between a private picture and the public internet.

This dialog is the only place the user is asked, so every one of its
states is load-bearing:

  Cancelling before the run must leave nothing uploaded and nothing
  recorded, because that is what the button says.

  A run that stops half way must not be reported as a success. The
  copies that were made are named, since a public blob the user cannot
  see is one they cannot revoke, but the publish does not go ahead.

  A failure must name a reason, and retry must cost only the failures.

  Cancelling mid-run must not abandon a copy that is already uploading,
  because that is exactly how a blob ends up public and unlisted.

The maker underneath is the real one, so these run against the real state
machine rather than a mock of it, over a fake fetcher and uploader and a
real ledger in a tmp directory. No modal loop is ever entered: every
state is reached by calling the method that reaches it, the way
``tests/test_drafts_panel.py`` drives the panel's context menu. Nothing
touches a network, a server, a relay, a signer or a real home directory.
"""

from __future__ import annotations

import hashlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog

from nostr.media.filecrypto import encrypt_file
from nostr.media.media_visibility import MediaVisibility
from nostr.media.publish_copy import CopyResult, CopyStage, PublicCopyMaker, PublishSet
from nostr.media.scrub import scrub_for_publication
from nostr.media.visibility import PrivateBlob, PublicBlob, PublicLedger
from nostr.ui.publish_copy_dialog import (
    CANCELLED,
    EXPLAIN,
    FAILED,
    WORKING,
    PickResolution,
    PublishCopyDialog,
    _storage_line,
    resolution_from,
    resolve_pick,
)

from tests.media_fakes import PNG_BYTES, make_media, sha_of


KEY = "7e" * 32
SERVER = "https://one.example"
CDN = "https://cdn.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def sealed(data: bytes = PNG_BYTES) -> bytes:
    return encrypt_file(data, key_hex=KEY).envelope


CIPHER = sealed()
CIPHER_SHA = sha_of(CIPHER)
SCRUBBED_SHA = sha_of(scrub_for_publication(PNG_BYTES, "image/png").data)


def _second_picture() -> bytes:
    """A different picture, not the same one with bytes glued on.

    Padding a PNG produces a file with a new hash whose *pixels* are
    identical, and two originals that scrub to the same bytes are one
    public blob with one source. That is a real case the maker documents,
    and it is not the case these tests are about, so the second fixture
    is genuinely a different image.
    """
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(5, 4, QImage.Format_RGB32)
    image.fill(QColor("blue"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(buffer.data())


SECOND_PNG = _second_picture()
OTHER = encrypt_file(SECOND_PNG, key_hex=KEY).envelope
OTHER_SHA = sha_of(OTHER)
SECOND_SCRUBBED_SHA = sha_of(scrub_for_publication(SECOND_PNG, "image/png").data)
assert SECOND_SCRUBBED_SHA != SCRUBBED_SHA


def private(envelope: bytes = CIPHER, *, name="holiday.png", **kw) -> PrivateBlob:
    return PrivateBlob(
        sha256=sha_of(envelope),
        key_hex=KEY,
        servers=[SERVER],
        size=kw.pop("size", len(envelope)),
        mime=kw.pop("mime", "image/png"),
        name=name,
        **kw,
    )


class FakeFetcher:
    """Serves bytes per URL, or parks the call for the test to release."""

    def __init__(self, blobs=None, *, park=False) -> None:
        self.blobs = dict(blobs or {})
        self.park = park
        self.requests: list = []
        self.pending: list = []
        self.errors: dict = {}

    def fetch(self, url, *, on_success, on_failure) -> None:
        self.requests.append(url)
        if self.park:
            self.pending.append((url, on_success, on_failure))
            return
        if url in self.errors:
            on_failure(self.errors[url])
            return
        data = self.blobs.get(url)
        if data is None:
            on_failure("that server does not have it")
            return
        on_success(data)

    def release(self, index: int = 0) -> None:
        url, on_success, on_failure = self.pending.pop(index)
        data = self.blobs.get(url)
        if data is None:
            on_failure("that server does not have it")
        else:
            on_success(data)


class FakeUploader(QObject):
    """MediaStore's observed surface: two signals and one method."""

    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)

    def __init__(self, *, auto=True) -> None:
        super().__init__()
        self.calls: list = []
        self.auto = auto
        self.fail_with = None

    def upload_bytes(self, body, *, name, mime_type="application/octet-stream") -> None:
        self.calls.append(SimpleNamespace(name=name, body=bytes(body), mime=mime_type))
        if not self.auto:
            return
        if self.fail_with is not None:
            self.upload_failed.emit(name, self.fail_with)
            return
        self.finish()

    def finish(self) -> None:
        call = self.calls[-1]
        self.upload_finished.emit(
            call.name, make_media(sha_of(call.body), servers=(CDN,), mime=call.mime),
        )


def build(tmp_path, blobs, *, fetcher=None, uploader=None, ledger=None):
    """A dialog over the real maker. Never executed, only driven."""
    ledger = ledger or PublicLedger(path=tmp_path / "public.json")
    fetcher = fetcher or FakeFetcher({
        f"{SERVER}/{CIPHER_SHA}": CIPHER,
        f"{SERVER}/{OTHER_SHA}": OTHER,
    })
    uploader = uploader or FakeUploader()
    maker = PublicCopyMaker(
        ledger=ledger, fetcher=fetcher, uploader=uploader, clock=lambda: 1_700_000_000,
    )
    dialog = PublishCopyDialog(blobs=blobs, maker=maker, is_dark=True)
    return SimpleNamespace(
        dialog=dialog, maker=maker, ledger=ledger,
        fetcher=fetcher, uploader=uploader,
    )


def rows(dialog) -> list:
    return [dialog._list.item(i).text() for i in range(dialog._list.count())]


# --------------------------------------------------------------------- #
# What the user is told before anything moves                            #
# --------------------------------------------------------------------- #

def test_the_four_promises_are_all_made(tmp_path):
    world = build(tmp_path, [private()])
    text = world.dialog._promises.text()
    assert "public copy" in text and "anyone" in text.lower()
    assert "not changed" in text and "stays private" in text
    assert "Camera and location" in text and "removed" in text
    assert "revoke" in text


def test_the_promises_are_shown_before_the_choice_and_not_during(tmp_path):
    world = build(tmp_path, [private()], uploader=FakeUploader(auto=False))
    assert world.dialog.state == EXPLAIN
    assert world.dialog._promises.isVisibleTo(world.dialog)
    world.dialog._on_create_clicked()
    assert world.dialog.state == WORKING
    assert not world.dialog._promises.isVisibleTo(world.dialog)


def test_the_storage_cost_is_stated(tmp_path):
    world = build(tmp_path, [private(size=2 * 1024 * 1024)])
    line = world.dialog._storage.text()
    assert "2.0 MiB" in line
    # A copy sits beside the original rather than replacing it, which is
    # the part a quota makes expensive.
    assert "original" in line


def test_the_storage_line_adds_the_whole_set_up():
    total = _storage_line([private(size=1024), private(OTHER, size=3 * 1024)])
    assert "4.0 KiB" in total
    assert "copies" in total


def test_the_action_button_says_what_it_does(tmp_path):
    one = build(tmp_path, [private()])
    assert one.dialog._create_btn.text() == "Create public copy"
    many = build(tmp_path, [private(), private(OTHER)])
    assert many.dialog._create_btn.text() == "Create 2 public copies"


def test_each_file_is_listed_by_name_and_size(tmp_path):
    world = build(tmp_path, [private(name="beach.jpg", size=4096)])
    assert rows(world.dialog) == ["beach.jpg  ·  4.0 KiB  ·  Waiting"]


def test_a_file_with_no_name_is_listed_by_its_hash(tmp_path):
    world = build(tmp_path, [private(name="")])
    assert CIPHER_SHA[:8] in rows(world.dialog)[0]


def test_a_file_that_already_has_a_copy_is_not_offered_again(tmp_path):
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(
        sha256="c" * 64, url=f"{CDN}/{'c' * 64}", source_hash=CIPHER_SHA,
    ))
    world = build(tmp_path, [private(), private(OTHER)], ledger=ledger)
    assert [b.sha256 for b in world.dialog.pending] == [OTHER_SHA]


def test_the_same_picture_twice_is_one_copy(tmp_path):
    world = build(tmp_path, [private(), private()])
    assert len(world.dialog.pending) == 1


# --------------------------------------------------------------------- #
# Cancelling before the run means nothing happened                       #
# --------------------------------------------------------------------- #

def test_refusing_uploads_nothing_and_records_nothing(tmp_path):
    world = build(tmp_path, [private()])
    world.dialog.reject()
    assert world.uploader.calls == []
    assert world.fetcher.requests == []
    assert len(world.ledger) == 0
    assert world.dialog.publish_set is None
    assert world.dialog.result() == QDialog.Rejected


def test_a_refusal_reads_as_the_user_answering_not_as_a_fault():
    assert resolution_from(None) == PickResolution(cancelled=True)


# --------------------------------------------------------------------- #
# The run                                                                #
# --------------------------------------------------------------------- #

def test_a_finished_run_hands_back_the_public_copy(tmp_path):
    world = build(tmp_path, [private()])
    world.dialog._on_create_clicked()

    outcome = world.dialog.publish_set
    assert outcome is not None and outcome.ok
    assert len(outcome.blobs) == 1
    copy = outcome.blobs[0]
    # The address handed back is the copy's, never the private original's.
    assert copy.sha256 == SCRUBBED_SHA
    assert copy.sha256 != CIPHER_SHA
    assert copy.source_hash == CIPHER_SHA
    assert world.ledger.get(copy.sha256) is not None
    assert world.dialog.result() == QDialog.Accepted


def test_the_bytes_uploaded_are_the_scrubbed_ones(tmp_path):
    world = build(tmp_path, [private()])
    world.dialog._on_create_clicked()
    sent = world.uploader.calls[0].body
    assert sent != PNG_BYTES or sent == scrub_for_publication(PNG_BYTES, "").data
    assert hashlib.sha256(sent).hexdigest() == SCRUBBED_SHA


def test_each_stage_reaches_the_row_it_belongs_to(tmp_path):
    world = build(tmp_path, [private()], uploader=FakeUploader(auto=False))
    world.dialog._on_create_clicked()
    # Parked at the upload, so the row shows the stage it is actually at.
    assert "Uploading the copy" in rows(world.dialog)[0]
    world.uploader.finish()
    assert "Public copy created" in rows(world.dialog)[0]


def test_the_scrub_stage_names_what_it_removes(tmp_path):
    seen = []
    world = build(tmp_path, [private()], uploader=FakeUploader(auto=False))
    world.maker.copy_progress.connect(lambda _s, stage: seen.append(stage))
    world.dialog._on_create_clicked()
    assert CopyStage.PREPARING in seen
    world.dialog._on_copy_progress(CIPHER_SHA, CopyStage.PREPARING)
    assert "Removing camera and location data" in rows(world.dialog)[0]


def test_progress_is_determinate_and_counts_settled_copies(tmp_path):
    world = build(tmp_path, [private(), private(OTHER)],
                  uploader=FakeUploader(auto=False))
    world.dialog._on_create_clicked()
    assert world.dialog._progress.maximum() == 2
    assert world.dialog._progress.isVisibleTo(world.dialog)
    world.uploader.finish()
    assert world.dialog._progress.value() == 1
    assert "1 of 2" in world.dialog._status.text()


# --------------------------------------------------------------------- #
# A partial set is never a success                                       #
# --------------------------------------------------------------------- #

def test_a_failure_stops_the_run_and_names_a_reason(tmp_path):
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER})
    world = build(tmp_path, [private(), private(OTHER)], fetcher=fetcher)
    world.dialog._on_create_clicked()

    assert world.dialog.state == FAILED
    assert world.dialog.publish_set.blobs is None
    assert "Nothing was published" in world.dialog._status.text()
    assert "could not be downloaded" in world.dialog._status.text()
    # Still open: reporting a partial set as done is the failure mode.
    assert world.dialog.result() != QDialog.Accepted


def test_the_copies_made_before_a_failure_are_named_not_hidden(tmp_path):
    # They are in the ledger and on a server. A user who is not told
    # they exist cannot revoke them.
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER})
    world = build(tmp_path, [private(), private(OTHER)], fetcher=fetcher)
    world.dialog._on_create_clicked()

    assert len(world.dialog.publish_set.minted) == 1
    status = world.dialog._status.text()
    assert "1 copy made before this" in status
    assert "revoked" in status


def test_a_failed_run_offers_a_retry_and_not_a_second_create(tmp_path):
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER})
    world = build(tmp_path, [private(), private(OTHER)], fetcher=fetcher)
    world.dialog._on_create_clicked()
    assert world.dialog._retry_btn.isVisibleTo(world.dialog)
    assert not world.dialog._create_btn.isVisibleTo(world.dialog)


def test_retrying_costs_only_the_file_that_failed(tmp_path):
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER})
    world = build(tmp_path, [private(), private(OTHER)], fetcher=fetcher)
    world.dialog._on_create_clicked()
    assert len(world.uploader.calls) == 1

    # The server comes back, and the copy that already succeeded is
    # reused from the ledger rather than fetched or uploaded again.
    fetcher.blobs[f"{SERVER}/{OTHER_SHA}"] = OTHER
    world.dialog._on_retry_clicked()

    assert world.dialog.publish_set.ok
    assert len(world.uploader.calls) == 2
    assert fetcher.requests.count(f"{SERVER}/{CIPHER_SHA}") == 1


def test_retry_is_not_offered_before_anything_has_failed(tmp_path):
    world = build(tmp_path, [private()])
    assert not world.dialog._retry_btn.isVisibleTo(world.dialog)
    # And it does nothing if reached another way.
    world.dialog._on_retry_clicked()
    assert world.uploader.calls == []


def test_a_failed_set_never_reports_a_public_address(tmp_path):
    fetcher = FakeFetcher({})
    world = build(tmp_path, [private()], fetcher=fetcher)
    world.dialog._on_create_clicked()
    resolution = resolution_from(world.dialog.publish_set)
    assert not resolution.ok
    assert resolution.url == ""
    assert "could not be downloaded" in resolution.reason


# --------------------------------------------------------------------- #
# Cancelling during the run                                              #
# --------------------------------------------------------------------- #

def test_cancelling_before_an_upload_leaves_nothing_behind(tmp_path):
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER}, park=True)
    world = build(tmp_path, [private()], fetcher=fetcher)
    world.dialog._on_create_clicked()
    assert world.dialog.state == WORKING

    world.dialog.reject()

    assert world.dialog.state == CANCELLED
    assert world.uploader.calls == []
    assert len(world.ledger) == 0
    assert world.dialog.publish_set.cancelled
    assert "Nothing was uploaded" in world.dialog._status.text()


def test_a_copy_that_is_already_uploading_is_carried_through(tmp_path):
    # Dropping it is exactly how a blob ends up on a server with nothing
    # in the ledger pointing at it, and a blob the user cannot see is one
    # they cannot revoke. So the cancel waits, and the window stays.
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER})
    world = build(tmp_path, [private()], fetcher=fetcher,
                  uploader=FakeUploader(auto=False))
    world.dialog._on_create_clicked()

    world.dialog.reject()

    assert world.dialog.state == WORKING
    assert "Stopping" in world.dialog._status.text()
    assert world.dialog.publish_set is None

    world.uploader.finish()
    assert len(world.ledger) == 1
    assert world.dialog.state == CANCELLED


def test_a_copy_already_minted_is_named_when_the_rest_is_cancelled(tmp_path):
    fetcher = FakeFetcher({
        f"{SERVER}/{CIPHER_SHA}": CIPHER, f"{SERVER}/{OTHER_SHA}": OTHER,
    })
    world = build(tmp_path, [private(), private(OTHER)], fetcher=fetcher,
                  uploader=FakeUploader(auto=False))
    world.dialog._on_create_clicked()
    # The second copy parks at its fetch, so it has not reached the
    # uploader and cancelling can drop it where it stands.
    fetcher.park = True
    world.uploader.finish()          # the first copy lands in the ledger

    world.dialog.reject()

    status = world.dialog._status.text()
    assert "Cancelled" in status
    assert "1 public copy had already been created" in status
    assert "revoked" in status
    assert len(world.ledger) == 1
    assert len(world.uploader.calls) == 1


def test_closing_after_the_run_settles_actually_closes(tmp_path):
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER}, park=True)
    world = build(tmp_path, [private()], fetcher=fetcher)
    world.dialog._on_create_clicked()
    world.dialog.reject()
    assert world.dialog.state == CANCELLED
    world.dialog.reject()
    assert world.dialog.result() == QDialog.Rejected


def test_the_close_button_renames_itself_once_there_is_nothing_to_stop(tmp_path):
    fetcher = FakeFetcher({f"{SERVER}/{CIPHER_SHA}": CIPHER}, park=True)
    world = build(tmp_path, [private()], fetcher=fetcher)
    assert world.dialog._cancel_btn.text() == "Cancel"
    world.dialog._on_create_clicked()
    assert world.dialog._cancel_btn.text() == "Cancel"
    world.dialog.reject()
    assert world.dialog._cancel_btn.text() == "Close"


# --------------------------------------------------------------------- #
# Consent covers exactly what was shown                                  #
# --------------------------------------------------------------------- #

def test_consent_does_not_stretch_to_a_file_that_was_never_shown(tmp_path):
    world = build(tmp_path, [private()])
    assert world.dialog._confirm_scope([private()])
    assert not world.dialog._confirm_scope([private(OTHER)])


def test_a_widened_set_publishes_nothing(tmp_path):
    # A copy revoked between the question and the answer would make the
    # maker want a file the user was never asked about.
    world = build(tmp_path, [private()])
    world.dialog._agreed = set()
    world.dialog._on_create_clicked()
    assert world.uploader.calls == []
    assert world.dialog.publish_set.blobs is None
    assert world.dialog.publish_set.cancelled


# --------------------------------------------------------------------- #
# The picker's answer                                                    #
# --------------------------------------------------------------------- #

def test_a_public_pick_passes_straight_through(tmp_path):
    media = make_media("d" * 64, servers=(CDN,), mime="image/png", size=17)
    resolution = resolve_pick(media, visibility=MediaVisibility(), maker=None)
    assert resolution.ok
    assert resolution.url == media.url
    assert resolution.sha256 == "d" * 64
    assert resolution.size == 17


def test_a_private_pick_with_no_maker_refuses_rather_than_leaking(tmp_path):
    # The fallback would be to embed the private file's own address,
    # which points at ciphertext no reader can open.
    blob = private()

    class Library:
        def get(self, sha):
            return blob if sha == blob.sha256 else None

        def vouches_for(self, sha):
            return True

    media = make_media(CIPHER_SHA, servers=(SERVER,))
    resolution = resolve_pick(
        media, visibility=MediaVisibility(library=Library()), maker=None,
    )
    assert not resolution.ok
    assert resolution.url == ""
    assert "private" in resolution.reason


def test_a_finished_set_resolves_to_the_copy(tmp_path):
    copy = PublicBlob(
        sha256=SCRUBBED_SHA, url=f"{CDN}/{SCRUBBED_SHA}", size=99, mime="image/png",
        source_hash=CIPHER_SHA,
    )
    resolution = resolution_from(PublishSet(blobs=[copy]))
    assert resolution.ok
    assert resolution.sha256 == SCRUBBED_SHA
    assert resolution.url == f"{CDN}/{SCRUBBED_SHA}"
    assert resolution.mime == "image/png"


def test_a_set_with_no_blobs_is_never_ok():
    outcome = PublishSet(blobs=None, failures=[CopyResult(
        source_hash=CIPHER_SHA, reason="the server said no",
    )])
    resolution = resolution_from(outcome)
    assert not resolution.ok
    assert resolution.reason == "the server said no"


def test_a_cancelled_set_carries_no_complaint():
    resolution = resolution_from(PublishSet(blobs=None, cancelled=True))
    assert not resolution.ok
    assert resolution.cancelled
    assert resolution.reason == ""


# --------------------------------------------------------------------- #
# The paths that embed media in published content                        #
# --------------------------------------------------------------------- #

class _Window:
    """MainWindow's insert path over a stub, the way test_media_document does.

    ``MainWindow`` cannot be constructed here: its ``__init__`` reads the
    real settings file and builds a relay pool. The method under test is
    lifted off the class instead, which is narrow but does catch the one
    failure that matters, an insert that skips the gate.
    """

    import main_window as _module

    _insert_media_at_cursor = _module.MainWindow._insert_media_at_cursor

    def __init__(self, resolution) -> None:
        self.is_dark_theme = True
        self._media_visibility = MediaVisibility()
        self._copy_maker = object()
        self._resolution = resolution
        self.asked: list = []
        self.adopted: list = []
        self.urls: list = []
        self.assets: list = []
        self.messages: list = []
        self.status = SimpleNamespace(
            showMessage=lambda text, msecs=0: self.messages.append(text)
        )
        self._asset_manager = SimpleNamespace(
            adopt_library_file=self._adopt,
        )

    def _adopt(self, **kw):
        self.adopted.append(kw)
        return SimpleNamespace(**kw)

    def _insert_url_as_text(self, editor, url) -> None:
        self.urls.append(url)

    def _insert_asset(self, editor, asset, *, alt) -> None:
        self.assets.append((asset, alt))


@pytest.fixture
def gated(monkeypatch):
    """Patch the gate at the name main_window imported it under."""
    import main_window as module

    def install(resolution):
        window = _Window(resolution)
        monkeypatch.setattr(
            module, "resolve_pick",
            lambda media, **kw: (window.asked.append((media, kw)) or resolution),
        )
        return window

    return install


def test_an_insert_goes_through_the_gate(gated):
    window = gated(PickResolution(
        sha256=SCRUBBED_SHA, url=f"{CDN}/{SCRUBBED_SHA}", mime="image/png",
        size=42, ok=True,
    ))
    media = make_media(CIPHER_SHA, servers=(SERVER,))

    window._insert_media_at_cursor(media, object(), "a beach")

    assert len(window.asked) == 1
    asked_media, kwargs = window.asked[0]
    assert asked_media is media
    assert kwargs["visibility"] is window._media_visibility
    assert kwargs["maker"] is window._copy_maker


def test_the_address_inserted_is_the_copy_and_never_the_original(gated):
    window = gated(PickResolution(
        sha256=SCRUBBED_SHA, url=f"{CDN}/{SCRUBBED_SHA}", mime="image/png",
        size=42, ok=True,
    ))
    media = make_media(CIPHER_SHA, servers=(SERVER,))

    window._insert_media_at_cursor(media, object(), "")

    assert window.adopted[0]["sha256"] == SCRUBBED_SHA
    assert window.adopted[0]["remote_url"] == f"{CDN}/{SCRUBBED_SHA}"
    assert CIPHER_SHA not in str(window.adopted)


def test_a_refused_pick_inserts_nothing_at_all(gated):
    window = gated(PickResolution(reason="This picture is private."))
    window._insert_media_at_cursor(make_media(CIPHER_SHA), object(), "")

    assert window.adopted == []
    assert window.assets == []
    assert window.urls == []
    assert window.messages == ["This picture is private."]


def test_a_cancelled_pick_says_nothing_and_inserts_nothing(gated):
    # The user already knows what they did; a message would be a scold.
    window = gated(PickResolution(cancelled=True))
    window._insert_media_at_cursor(make_media(CIPHER_SHA), object(), "")
    assert window.adopted == [] and window.urls == []
    assert window.messages == []


def test_a_public_pick_still_inserts_the_way_it_always_did(gated):
    media = make_media("d" * 64, servers=(CDN,), mime="image/png", size=9)
    window = gated(PickResolution(
        sha256=media.hash, url=media.url, mime="image/png", size=9, ok=True,
    ))
    window._insert_media_at_cursor(media, object(), "alt words")
    assert window.adopted[0]["sha256"] == media.hash
    assert window.assets[0][1] == "alt words"


def test_a_non_image_copy_falls_back_to_a_link(gated):
    window = gated(PickResolution(
        sha256="e" * 64, url=f"{CDN}/{'e' * 64}", mime="video/mp4", ok=True,
    ))
    window._insert_media_at_cursor(make_media("e" * 64), object(), "")
    assert window.urls == [f"{CDN}/{'e' * 64}"]
    assert window.adopted == []


class _CoverDialog:
    """PublishArticleDialog's cover-pick path over a stub."""

    from nostr.ui.publish_article_dialog import PublishArticleDialog as _Dialog

    _on_cover_image_picked = _Dialog._on_cover_image_picked

    def __init__(self) -> None:
        self._is_dark = True
        self._media_visibility = MediaVisibility()
        self._copy_maker = object()
        self._cover_thumb_hash = ""
        self._cover_loader = None
        self.text = ""
        self.errors: list = []
        self._image_edit = SimpleNamespace(setText=self._set_text)

    def _set_text(self, value) -> None:
        self.text = value

    def _set_status(self, text, *, error=False) -> None:
        self.errors.append((text, error))


def test_the_cover_field_takes_the_copy_not_the_private_address(monkeypatch):
    import nostr.ui.publish_article_dialog as module

    monkeypatch.setattr(module, "resolve_pick", lambda media, **kw: PickResolution(
        sha256=SCRUBBED_SHA, url=f"{CDN}/{SCRUBBED_SHA}", mime="image/png", ok=True,
    ))
    dialog = _CoverDialog()
    dialog._on_cover_image_picked(make_media(CIPHER_SHA, servers=(SERVER,)), "")

    assert dialog.text == f"{CDN}/{SCRUBBED_SHA}"
    assert dialog._cover_thumb_hash == SCRUBBED_SHA


def test_a_refused_cover_leaves_the_field_alone(monkeypatch):
    import nostr.ui.publish_article_dialog as module

    monkeypatch.setattr(module, "resolve_pick", lambda media, **kw: PickResolution(
        reason="No public copy was created.",
    ))
    dialog = _CoverDialog()
    dialog._on_cover_image_picked(make_media(CIPHER_SHA, servers=(SERVER,)), "")

    assert dialog.text == ""
    assert dialog._cover_thumb_hash == ""
    assert dialog.errors == [("No public copy was created.", True)]


def test_a_pick_that_is_already_public_asks_nothing(tmp_path):
    """No modal for a copy that already exists.

    Nothing new becomes public, so there is nothing to consent to, and a
    dialog that asks about nothing is the kind users learn to click
    through without reading.
    """
    blob = private()
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(
        sha256=SCRUBBED_SHA, url=f"{CDN}/{SCRUBBED_SHA}", mime="image/png",
        size=64, source_hash=CIPHER_SHA,
    ))
    fetcher = FakeFetcher({})
    uploader = FakeUploader()
    maker = PublicCopyMaker(ledger=ledger, fetcher=fetcher, uploader=uploader)

    class Library:
        def get(self, sha):
            return blob if sha == blob.sha256 else None

        def vouches_for(self, sha):
            return True

    resolution = resolve_pick(
        make_media(CIPHER_SHA, servers=(SERVER,)),
        visibility=MediaVisibility(library=Library(), ledger=ledger),
        maker=maker,
    )

    assert resolution.ok
    assert resolution.url == f"{CDN}/{SCRUBBED_SHA}"
    # Nothing was fetched and nothing was uploaded to answer it.
    assert fetcher.requests == []
    assert uploader.calls == []


def test_a_dialog_with_nothing_pending_still_settles(tmp_path):
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(
        sha256=SCRUBBED_SHA, url=f"{CDN}/{SCRUBBED_SHA}", source_hash=CIPHER_SHA,
    ))
    world = build(tmp_path, [private()], ledger=ledger)
    assert world.dialog.pending == []
    assert world.dialog._create_btn.text() == "Use the existing copies"

    world.dialog._on_create_clicked()

    assert world.dialog.publish_set.ok
    assert world.uploader.calls == []


# --------------------------------------------------------------------- #
# A pick nobody has been able to check                                   #
# --------------------------------------------------------------------- #
#
# The gate used to have two answers, private and public, and gave the
# second one to everything it had not read yet. So a pick made while the
# library was still opening, or after a signer failed to answer at all,
# passed straight through with the private blob's own ciphertext address
# and its content hash, unwarned and unasked. These pin the third answer.

class _UncheckedLibrary:
    """A private library that has not been able to account for anything."""

    def __init__(self, blobs=()) -> None:
        self.blobs = {b.sha256: b for b in blobs}

    def get(self, sha):
        return self.blobs.get(sha)

    def vouches_for(self, sha):
        return False


def test_an_unchecked_pick_is_refused_rather_than_passed_through():
    media = make_media(CIPHER_SHA, servers=(SERVER,), mime="application/octet-stream")

    resolution = resolve_pick(
        media, visibility=MediaVisibility(library=_UncheckedLibrary()), maker=None,
    )

    assert not resolution.ok
    assert not resolution.cancelled
    # Nothing about the original comes back, so a caller that ignores
    # ``ok`` still has nothing to embed.
    assert resolution.url == ""
    assert resolution.sha256 == ""
    assert "not been checked" in resolution.reason


def test_an_unchecked_pick_is_refused_even_with_a_maker_available(tmp_path):
    # A maker cannot help: making a public copy needs the key, and the
    # reason this pick is unchecked is that no record for it was read.
    ledger = PublicLedger(path=tmp_path / "public.json")
    fetcher = FakeFetcher({})
    uploader = FakeUploader()
    maker = PublicCopyMaker(ledger=ledger, fetcher=fetcher, uploader=uploader)

    resolution = resolve_pick(
        make_media(CIPHER_SHA, servers=(SERVER,)),
        visibility=MediaVisibility(library=_UncheckedLibrary(), ledger=ledger),
        maker=maker,
    )

    assert not resolution.ok
    assert fetcher.requests == []
    assert uploader.calls == []
    assert len(ledger) == 0


def test_a_file_already_opened_is_still_gated_while_the_rest_load(tmp_path):
    # Coverage is per file. One that has been read is private and goes
    # to the consent gate as usual, not to the unchecked refusal.
    blob = private()
    resolution = resolve_pick(
        make_media(CIPHER_SHA, servers=(SERVER,)),
        visibility=MediaVisibility(library=_UncheckedLibrary([blob])),
        maker=None,
    )

    assert not resolution.ok
    assert "private" in resolution.reason


# --------------------------------------------------------------------- #
# The same thing end to end, over the real library                       #
# --------------------------------------------------------------------- #

PRIVATE_URL = f"{SERVER}/{CIPHER_SHA}"


def _private_event():
    """One kind-34578 record for the encrypted picture above."""
    import json

    payload = {
        "name": "holiday.png",
        "hash": CIPHER_SHA,
        "size": len(CIPHER),
        "type": "image/png",
        "server": SERVER,
        "servers": [SERVER],
        "encryptionKey": KEY,
        "uploadedAt": 1_699_000_000,
    }
    return {
        "id": "e" * 64,
        "kind": 34578,
        "pubkey": "ab" * 32,
        "created_at": 1000,
        "tags": [["d", CIPHER_SHA], ["client", "lotus"], ["encrypted", "nip44"]],
        "content": "ENC[" + json.dumps(payload) + "]",
    }


def _real_library(*, signer):
    """The real PrivateLibrary, wired the way ``MainWindow`` wires it."""
    from nostr.media.private_library import PrivateLibrary
    from tests.imports_fakes import FakeRelayListCache

    class Query:
        def addressable(self, relays, filters, on_done):
            on_done([_private_event()])

    return PrivateLibrary(
        session_pool=signer,
        relay_list_cache=FakeRelayListCache(),
        query=Query(),
        clock=lambda: 1_700_000_000,
    )


class _ParkedSigner:
    """A signer showing a prompt that has not been answered yet."""

    def __init__(self) -> None:
        self.pending: list = []

    def get(self, profile, on_ready=None, on_error=None):
        on_ready(self)

    def nip44_decrypt_self(self, ciphertext, on_success, on_failure, **_kw):
        self.pending.append((ciphertext, on_success))

    def settle_all(self) -> None:
        while self.pending:
            ciphertext, on_success = self.pending.pop(0)
            on_success(ciphertext[4:-1])


class _AbsentSigner:
    def __init__(self, reason="bunker relay refused the connection") -> None:
        self.reason = reason

    def get(self, profile, on_ready=None, on_error=None):
        on_error(self.reason)


def _insert_window(library, ledger):
    """``MainWindow``'s insert path over the real gate and real library."""
    window = _Window(None)
    window._media_visibility = MediaVisibility(library=library, ledger=ledger)
    window._copy_maker = None
    return window


def test_an_insert_while_the_signer_prompt_is_open_embeds_nothing(tmp_path):
    signer = _ParkedSigner()
    library = _real_library(signer=signer)
    library.bind_profile(SimpleNamespace(
        user_pubkey="ab" * 32, bunker_relays=["wss://bunker.example"]))
    assert library.loading is True

    ledger = PublicLedger(path=tmp_path / "public.json")
    window = _insert_window(library, ledger)
    window._insert_media_at_cursor(
        make_media(CIPHER_SHA, servers=(SERVER,)), object(), "a photo")

    # The private blob's own address and content hash reach nothing.
    assert window.adopted == []
    assert window.assets == []
    assert window.urls == []
    assert CIPHER_SHA not in str(window.adopted + window.assets + window.urls)
    assert window.messages and "not been checked" in window.messages[0]


def test_the_same_pick_is_gated_normally_once_the_signer_answers(tmp_path):
    signer = _ParkedSigner()
    library = _real_library(signer=signer)
    library.bind_profile(SimpleNamespace(
        user_pubkey="ab" * 32, bunker_relays=["wss://bunker.example"]))
    signer.settle_all()

    ledger = PublicLedger(path=tmp_path / "public.json")
    window = _insert_window(library, ledger)
    window._insert_media_at_cursor(
        make_media(CIPHER_SHA, servers=(SERVER,)), object(), "a photo")

    # Still nothing embedded, but now for the reason the user can act on.
    assert window.urls == [] and window.adopted == []
    assert "private" in window.messages[0]


def test_a_signer_that_never_answers_does_not_make_private_files_public(tmp_path):
    library = _real_library(signer=_AbsentSigner())
    seen: list = []
    library.status_changed.connect(seen.append)
    library.bind_profile(SimpleNamespace(
        user_pubkey="ab" * 32, bunker_relays=["wss://bunker.example"]))

    # The library is done trying, for the whole session.
    assert library.loading is False
    assert len(library) == 0
    assert any("stayed closed" in line for line in seen)

    ledger = PublicLedger(path=tmp_path / "public.json")
    window = _insert_window(library, ledger)
    window._insert_media_at_cursor(
        make_media(CIPHER_SHA, servers=(SERVER,)), object(), "a photo")

    assert window.adopted == [] and window.urls == [] and window.assets == []
    assert PRIVATE_URL not in str(window.messages)


def test_a_public_file_still_inserts_while_the_library_is_unreadable(tmp_path):
    # Failing closed is per file, not per library. An ordinary blob the
    # relays never listed as private is still public, and refusing it
    # would break the picker every time a signer is unplugged.
    library = _real_library(signer=_AbsentSigner())
    library.bind_profile(SimpleNamespace(
        user_pubkey="ab" * 32, bunker_relays=["wss://bunker.example"]))

    ledger = PublicLedger(path=tmp_path / "public.json")
    window = _insert_window(library, ledger)
    ordinary = make_media("d" * 64, servers=(CDN,), mime="image/png", size=17)
    window._insert_media_at_cursor(ordinary, object(), "alt words")

    assert window.adopted[0]["sha256"] == "d" * 64
    assert window.messages == []


# --------------------------------------------------------------------- #
# The article's cover picker is the second way into the same choice      #
# --------------------------------------------------------------------- #

class _Chooser:
    """PublishArticleDialog's cover-picker path over a stub.

    This picker's library is bound nowhere else in the app, so before
    this it was never read at all and every private file in it looked
    like an ordinary public blob for the life of the dialog.
    """

    from nostr.ui.publish_article_dialog import PublishArticleDialog as _Dialog

    _on_choose_cover_image = _Dialog._on_choose_cover_image

    def __init__(self, library) -> None:
        self._is_dark = True
        self._media_store = object()
        self._media_visibility = MediaVisibility()
        self._private_library = library
        self._current_profile = SimpleNamespace(
            user_pubkey="ab" * 32, bunker_relays=["wss://bunker.example"])

    def _on_cover_image_picked(self, media, alt) -> None:
        pass


class _StubPicker:
    def __init__(self, **kw) -> None:
        self.kwargs = kw
        self.bound: list = []
        self.executed = 0
        self._filter_combo = SimpleNamespace(setCurrentIndex=lambda _i: None)
        self.file_picked = SimpleNamespace(connect=lambda _slot: None)

    def bind_private_library(self, library) -> None:
        self.bound.append(library)

    def setWindowTitle(self, title) -> None:
        pass

    def exec(self) -> int:
        self.executed += 1
        return 0


def test_the_cover_picker_opens_the_private_library_and_follows_it(monkeypatch):
    import nostr.ui.publish_article_dialog as module

    class Library:
        def __init__(self) -> None:
            self.bound: list = []

        def bind_profile(self, profile) -> None:
            self.bound.append(profile)

    library = Library()
    made: list = []
    monkeypatch.setattr(
        module, "MediaLibraryDialog",
        lambda **kw: (made.append(_StubPicker(**kw)) or made[-1]),
    )

    chooser = _Chooser(library)
    chooser._on_choose_cover_image()

    # The read is started here rather than on the way into the dialog,
    # so an article published without a cover costs no signer prompts.
    assert library.bound == [chooser._current_profile]
    assert made[0].bound == [library]
    assert made[0].executed == 1


def test_a_cover_picker_with_no_library_still_opens(monkeypatch):
    import nostr.ui.publish_article_dialog as module

    made: list = []
    monkeypatch.setattr(
        module, "MediaLibraryDialog",
        lambda **kw: (made.append(_StubPicker(**kw)) or made[-1]),
    )
    chooser = _Chooser(None)
    chooser._on_choose_cover_image()

    assert made[0].executed == 1
    assert made[0].bound == []


def test_an_unchecked_cover_pick_leaves_the_field_alone():
    dialog = _CoverDialog()
    dialog._media_visibility = MediaVisibility(library=_UncheckedLibrary())

    dialog._on_cover_image_picked(make_media(CIPHER_SHA, servers=(SERVER,)), "")

    assert dialog.text == ""
    assert dialog._cover_thumb_hash == ""
    assert "not been checked" in dialog.errors[0][0]
