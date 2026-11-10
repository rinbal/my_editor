# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Media Library window: a familiar library, with privacy handled.

What must hold:

  The grid shows names (the one a file was added with, a private
  file's own, else the day it was added), never a hash, a type or a
  server. Public files, the normal case, carry no mark; a private one,
  the exception, carries a lock, and one nobody could check carries
  none. The window says once, at its foot, who can see its files. A
  public copy of a private original is not a second tile.

  An empty library, a loading one, a search with no match and a
  library that could not be listed each say one plain sentence, with
  the one action that helps.

  A file being added is a tile with its own progress, Cancel, and on
  failure the plain reason with Retry; a server that did not keep a
  copy is said once, when the file is added.

  Delete asks first, from the keys and the menus alike, and never says
  a file is gone while a server still has it.

  The picker inserts with Return, and refuses a file nobody could check,
  saying so under the grid with Try Again.

  Thumbnails are asked for as tiles come into view; a private picture
  is opened in memory and nothing decrypted reaches the blob cache.
"""

from __future__ import annotations

import os
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import alerts  # noqa: E402
import nostr.ui.media_library_dialog as dialog_module  # noqa: E402
from nostr.blossom.store import MediaFile, ServerListing  # noqa: E402
from nostr.blossom.upload_progress import UploadProgress  # noqa: E402
from nostr.media.filecrypto import encrypt_file  # noqa: E402
from nostr.media.media_visibility import MediaVisibility  # noqa: E402
from nostr.media.visibility import PrivateBlob, PublicBlob, PublicLedger  # noqa: E402
from nostr.ui.media_grid import FAILED, SENDING, TILE_ROLE, WAITING  # noqa: E402
from nostr.ui.media_library_dialog import MediaLibraryDialog, storage_text  # noqa: E402
from tests.media_fakes import PNG_BYTES, fake_alert, sha_of  # noqa: E402

KEY = "5c" * 32
SERVER = "https://one.example"
MEMBERS = "https://members.example"
GB = 1024 ** 3


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def close_windows():
    """Every window a test opens is closed after it, so no later test
    finds it still the active window."""
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MediaLibraryDialog) and widget.isVisible():
            widget.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeStore(QObject):
    """MediaStore's surface, recording what the window asks of it."""

    library_changed = Signal()
    fetch_started = Signal()
    fetch_finished = Signal()
    fetch_error = Signal(str)
    upload_started = Signal(str)
    upload_progress = Signal(str, int, int)
    upload_status = Signal(str, str)
    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)
    upload_cancelled = Signal(str)
    server_skipped = Signal(str, str, str)
    mirror_failed = Signal(str, str, str)
    listings_changed = Signal()
    file_deleted = Signal(str)
    delete_failed = Signal(str, str)
    submission_checked = Signal(str)

    def __init__(self, records=(), *, names=None, quota=None, used=0, listing_ok=True):
        super().__init__()
        self.files = {m.hash: m for m in records}
        self.names = dict(names or {})
        self.jobs = {}
        self.quota, self.used, self.listing_ok = quota, used, listing_ok
        self.calls = []
        self.refuse_on_add = ""

    def target_servers(self):
        return [SERVER, MEMBERS]

    def is_submission_file(self, sha):
        return False        # the real store's answer: test_submission_files_*

    def submission_state(self, sha):
        return False

    def submission_check_failed(self, sha):
        return False

    def check_submission_file(self, sha, *, again=False):
        self.calls.append(("check", sha))

    def server_listings(self):
        return {s: ServerListing(origin=s, ok=self.listing_ok,
                                 truncated=getattr(self, "truncated", False))
                for s in self.target_servers()}

    def quota_for(self, origin):
        return self.quota if origin == MEMBERS else None

    def bytes_on(self, origin):
        return self.used

    def name_of(self, sha):
        return self.names.get(sha, "")

    def file_list(self, *, filter_type="all", sort_by="newest"):
        files = list(self.files.values())
        if sort_by == "largest":
            return sorted(files, key=lambda m: -m.size)
        return sorted(files, key=lambda m: -m.uploaded_at_ms)

    def fetch(self, force=False):
        self.calls.append(("fetch", force))

    def upload_file(self, path):
        name = os.path.basename(path)
        self.calls.append(("upload_file", name))
        if self.refuse_on_add:
            self.upload_failed.emit(name, self.refuse_on_add)
        return name

    def add_bytes(self, body, *, label, mime_type):
        self.calls.append(("add_bytes", label, mime_type))
        return label

    def upload_job(self, name):
        return getattr(self, "jobs", {}).get(name)

    def added_uploads(self):
        return list(getattr(self, "added", []))

    def refusal_detail(self, name):
        return getattr(self, "refused", {}).get(name, "")

    def can_retry(self, name):
        return True

    def cancel_upload(self, name):
        self.calls.append(("cancel", name))
        return True

    def retry_upload(self, name):
        self.calls.append(("retry", name))

    def dismiss_upload(self, name):
        self.calls.append(("dismiss", name))

    def delete_file(self, sha):
        self.calls.append(("delete", sha))


class FakeDownload(QObject):
    progress = Signal(int, int)
    finished = Signal(str)

    def __init__(self, url, sha, target, size, transform, keep_existing):
        super().__init__()
        self.url, self.sha, self.target, self.size = url, sha, target, size
        self.transform, self.keep_existing = transform, keep_existing
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoader(QObject):
    ready = Signal(str, str, object)
    failed = Signal(str, str)
    url_ready = Signal(str, str, object)
    url_failed = Signal(str, str)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.loads = []
        self.downloads = []

    def download_to(self, url, sha, target, *, size, transform=None, keep_existing=False):
        job = FakeDownload(url, sha, target, size, transform, keep_existing)
        self.downloads.append(job)
        return job

    def cache_path(self, sha):
        return self.root / sha.lower()

    def load(self, sha, url):
        self.loads.append(sha)


class FakeLibrary(QObject):
    library_changed = Signal()
    status_changed = Signal(str)

    def __init__(self, blobs=(), *, vouches=True, loading=False):
        super().__init__()
        self.blobs = {b.sha256: b for b in blobs}
        self.vouches, self.loading = vouches, loading
        self.refreshed = 0

    @property
    def settled(self):
        return self.vouches and not self.loading

    def get(self, sha):
        return self.blobs.get(sha)

    def vouches_for(self, sha):
        return self.vouches

    def refresh(self):
        self.refreshed += 1


def media(n, *, mime="image/png", size=1000, day=0):
    sha = f"{n:064x}"
    return MediaFile(hash=sha, url=f"{SERVER}/{sha}.png", urls=[{"server": SERVER, "url": ""}],
                     mime_type=mime, size=size,
                     uploaded_at_ms=1_780_000_000_000 - day * 86_400_000)


def build(tmp_path, monkeypatch, store=None, *, pick=False, visibility=None, library=None,
          size=(860, 560)):
    cache = tmp_path / "cache"
    loaders = []

    def make_loader(parent=None):
        loader = FakeLoader(cache, parent=parent)
        loaders.append(loader)
        return loader

    monkeypatch.setattr(dialog_module, "ThumbnailLoader", make_loader)
    store = store if store is not None else FakeStore()
    dialog = MediaLibraryDialog(store=store, is_dark=False, pick_mode=pick,
                                visibility=visibility)
    if library is not None:
        dialog.bind_private_library(library)
    dialog.resize(*size)
    dialog.show()
    QCoreApplication.processEvents()
    return SimpleNamespace(dialog=dialog, store=store, loader=loaders[0], grid=dialog._grid,
                           model=dialog._model)


def tiles(world):
    return world.model.tiles


def names(world):
    return [tile.name for tile in tiles(world)]


def select(world, key):
    """That tile alone, as a click selects it. Not setCurrentIndex, which
    reads the keyboard's modifiers and so adds to the selection when a
    key event of an earlier test left Cmd down."""
    from PySide6.QtCore import QItemSelectionModel
    index = world.model.index(world.model.row_of(key))
    world.grid.selectionModel().setCurrentIndex(
        index, QItemSelectionModel.SelectionFlag.ClearAndSelect)


def press(widget, key, modifiers=Qt.KeyboardModifier.NoModifier):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def sealed(data=PNG_BYTES, key=KEY):
    return encrypt_file(data, key_hex=key).envelope


# --------------------------------------------------------------------------- #
# Names, and what a tile says
# --------------------------------------------------------------------------- #

def test_a_tile_is_named_by_the_name_it_was_added_with(tmp_path, monkeypatch):
    one, two = media(1, day=1), media(2, day=2)
    world = build(tmp_path, monkeypatch, FakeStore([one, two], names={one.hash: "Harbour.jpg"}))
    harbour, unnamed = tiles(world)
    assert harbour.name == "Harbour.jpg"
    assert harbour.detail == "May 27, 2026"
    # No name: the day it was added, and its size beneath.
    assert unnamed.name == "May 26, 2026"
    assert unnamed.detail == "1000 B"


def test_no_hash_type_or_server_is_on_a_tile(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]))
    tile = tiles(world)[0]
    for text in (tile.name, tile.detail, tile.tooltip(), tile.accessible_text()):
        assert tile.key not in text and "image/png" not in text and "one.example" not in text


def test_a_public_file_the_normal_case_carries_no_badge(tmp_path, monkeypatch):
    """Everything MyEditor uploads is public, so a mark on it would be on
    nearly every tile and say nothing. The exception is marked instead."""
    world = build(tmp_path, monkeypatch, FakeStore([media(1)], names={f"{1:064x}": "Garden.jpg"}))
    tile = tiles(world)[0]
    assert not tile.private
    assert tile.accessible_text() == "Garden.jpg, image"
    assert tile.tooltip() == "Garden.jpg"


def test_a_private_file_the_exception_carries_the_lock(tmp_path, monkeypatch):
    envelope = sealed()
    private = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}",
                        mime_type="application/octet-stream", size=10)
    blob = PrivateBlob(sha256=private.hash, key_hex=KEY, mime="image/png", name="Secret.png")
    visibility = MediaVisibility(library=FakeLibrary([blob]))
    world = build(tmp_path, monkeypatch, FakeStore([private]), visibility=visibility)
    tile = tiles(world)[0]
    assert (tile.name, tile.kind, tile.private) == ("Secret.png", "image", True)
    assert tile.accessible_text() == "Secret.png, image, private"
    assert tile.tooltip() == "Secret.png\nOnly you can see this"


def test_a_file_nobody_could_check_carries_no_badge(tmp_path, monkeypatch):
    unchecked = MediaVisibility(library=FakeLibrary(vouches=False))
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]), visibility=unchecked)
    assert not tiles(world)[0].private


def test_the_window_says_once_who_can_see_its_files(tmp_path, monkeypatch):
    """Truthful about the normal case without marking every tile: one
    quiet line at the foot, where Finder and Photos say what is true of
    the whole window, and in the empty state."""
    world = build(tmp_path, monkeypatch)
    line = "Anyone with the link can see the files you add here."
    assert world.dialog._status.text() == line
    assert world.dialog._empty_text.text() == (
        "Images, videos, audio and files you add appear here. Anyone with the link can see them.")
    world.dialog._say("“Garden.jpg” is no longer shared.")
    assert world.dialog._status.text() == "“Garden.jpg” is no longer shared."
    world.dialog._status_timer.timeout.emit()
    assert world.dialog._status.text() == line
    # A problem stays until something else is said.
    world.dialog._say("“Garden.jpg” couldn’t be saved. Try again.", error=True)
    assert not world.dialog._status_timer.isActive()


def test_a_public_copy_of_a_private_original_is_one_tile(tmp_path, monkeypatch):
    envelope = sealed()
    original = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10,
                         uploaded_at_ms=2)
    copy = media(7)
    blob = PrivateBlob(sha256=original.hash, key_hex=KEY, mime="image/png", name="Beach.png")
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(sha256=copy.hash, url=copy.url, source_hash=original.hash))
    visibility = MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)
    world = build(tmp_path, monkeypatch, FakeStore([original, copy]), visibility=visibility)
    assert names(world) == ["Beach.png"]
    assert not tiles(world)[0].private            # its copy is out there


# --------------------------------------------------------------------------- #
# Finding things
# --------------------------------------------------------------------------- #

def test_the_kind_pop_up_shows_images_second(tmp_path, monkeypatch):
    """The article cover's picker chooses Images by index (cover_field)."""
    store = FakeStore([media(1), media(2, mime="video/mp4")])
    world = build(tmp_path, monkeypatch, store)
    world.dialog._filter_combo.setCurrentIndex(1)
    assert [t.kind for t in tiles(world)] == ["image"]


def test_search_and_name_order(tmp_path, monkeypatch):
    files = [media(1, day=1), media(2, day=2), media(3, day=3)]
    store = FakeStore(files, names={files[0].hash: "Zebra.png", files[1].hash: "apple.png",
                                    files[2].hash: "Mango.png"})
    world = build(tmp_path, monkeypatch, store)
    world.dialog._sort_combo.setCurrentIndex(world.dialog._sort_combo.findData("name"))
    assert names(world) == ["apple.png", "Mango.png", "Zebra.png"]
    world.dialog._search.setText("an")
    assert names(world) == ["Mango.png"]
    world.dialog._search.setText("nothing like it")
    assert world.dialog._stack.currentWidget() is world.dialog._empty
    assert world.dialog._empty_text.text() == "No items match “nothing like it”."


def test_an_empty_library_says_one_sentence_and_offers_add(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch)
    assert world.dialog._empty_text.text() == (
        "Images, videos, audio and files you add appear here. Anyone with the link can see them.")
    assert world.dialog._empty_button.text() == "Add…"


def test_a_loading_library_says_so(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch)
    world.store.fetch_started.emit()
    assert world.dialog._empty_text.text() == "Loading…"
    assert world.dialog._empty_button.isHidden()


def test_a_library_that_could_not_be_listed_says_why_and_offers_try_again(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch)
    world.store.fetch_error.emit("NETWORK_UNAVAILABLE")
    assert world.dialog._empty_text.text() == (
        "Couldn’t reach your media servers: check your internet connection and try again.")
    assert world.dialog._empty_button.text() == "Try Again"
    world.dialog._empty_button.click()
    assert ("fetch", True) in world.store.calls

    world = build(tmp_path, monkeypatch)
    world.store.fetch_error.emit("SIGNER_REJECTED")
    assert "signer app" in world.dialog._empty_text.text()


def test_thumbnails_are_asked_for_as_tiles_come_into_view(tmp_path, monkeypatch):
    store = FakeStore([media(n) for n in range(200)])
    world = build(tmp_path, monkeypatch, store, size=(560, 420))
    world.dialog._load_visible()
    asked = len(world.loader.loads)
    assert 0 < asked < 60
    world.grid.verticalScrollBar().setValue(world.grid.verticalScrollBar().maximum())
    world.dialog._load_visible()
    assert f"{0:064x}" in world.loader.loads and len(world.loader.loads) > asked


# --------------------------------------------------------------------------- #
# Adding files
# --------------------------------------------------------------------------- #

def test_an_added_file_is_a_tile_with_its_own_progress(tmp_path, monkeypatch):
    path = tmp_path / "Beach day.png"
    path.write_bytes(PNG_BYTES)
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    tile = world.model.tile("Beach day.png")
    assert (tile.state, tile.name) == (WAITING, "Beach day.png")
    assert tile.meter is None and tile.detail == "Preparing…"     # nothing measured yet
    meter = UploadProgress()
    world.store.jobs["Beach day.png"] = types.SimpleNamespace(meter=meter)
    world.store.upload_started.emit("Beach day.png")
    meter.sending(420, 1000)
    world.store.upload_progress.emit("Beach day.png", 420, 1000)
    assert (tile.state, tile.meter) == (SENDING, meter)            # the upload's own indicator
    assert tile.detail.startswith("Uploading…")
    assert world.dialog._ticker.isActive()                         # it keeps moving
    assert tile.accessible_text() == "Beach day.png, image, uploading"

    landed = media(9)
    world.store.files[landed.hash] = landed
    world.store.upload_finished.emit("Beach day.png", landed)
    assert world.model.tile("Beach day.png") is None
    assert world.grid.currentIndex().data(TILE_ROLE).key == landed.hash


def test_a_failed_upload_says_why_on_the_tile_and_can_be_retried(tmp_path, monkeypatch):
    path = tmp_path / "Scan.png"
    path.write_bytes(PNG_BYTES)
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    reason = "Couldn’t reach your media server: check your internet connection and try again."
    world.store.upload_failed.emit("Scan.png", reason)
    tile = world.model.tile("Scan.png")
    assert (tile.state, tile.detail) == (FAILED, reason)
    assert world.dialog._status.text() == f"“Scan.png” wasn’t added. {reason}"
    world.grid.retry_requested.emit("Scan.png")
    assert ("retry", "Scan.png") in world.store.calls
    assert tile.state == WAITING


def test_the_servers_that_refused_a_file_are_the_line_s_tooltip_never_its_words(
        tmp_path, monkeypatch):
    # Contract 7 names them; lessons 36 keeps them behind a tooltip.
    path = tmp_path / "Scan.png"
    path.write_bytes(PNG_BYTES)
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    world.store.refused = {"Scan.png": "good.example: too large\nmirror.example: too large"}
    reason = "No media server took the file: add another one in Media Servers."
    world.store.upload_failed.emit("Scan.png", reason)
    assert world.dialog._status.text() == f"“Scan.png” wasn’t added. {reason}"
    assert world.dialog._status.toolTip() == "good.example: too large\nmirror.example: too large"
    world.dialog._say("Something else.")
    assert world.dialog._status.toolTip() == ""     # the next line takes it away


def test_a_refusal_before_the_upload_starts_reaches_the_tile(tmp_path, monkeypatch):
    path = tmp_path / "Huge.png"
    path.write_bytes(PNG_BYTES)
    store = FakeStore()
    store.refuse_on_add = "This file is larger than your media servers take (100.0 MB)."
    world = build(tmp_path, monkeypatch, store)
    world.dialog._add_paths([str(path)])
    tile = world.model.tile("Huge.png")
    assert (tile.state, tile.detail) == (FAILED, store.refuse_on_add)


def test_cancel_and_remove(tmp_path, monkeypatch):
    path = tmp_path / "Clip.mp4"
    path.write_bytes(b"x")
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    world.grid.cancel_requested.emit("Clip.mp4")
    assert ("cancel", "Clip.mp4") in world.store.calls
    world.store.upload_cancelled.emit("Clip.mp4")
    assert world.model.tile("Clip.mp4") is None


def test_a_file_that_is_no_media_is_not_added_and_it_says_so(tmp_path, monkeypatch):
    path = tmp_path / "program.exe"
    path.write_text("hello", encoding="utf-8")
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    assert world.store.calls == [("fetch", False)]
    assert world.dialog._status.text() == (
        "“program.exe” wasn’t added. Only images, videos, audio and documents can be added: "
        "choose one of those.")


def test_files_that_are_no_media_are_said_once_never_counted(tmp_path, monkeypatch):
    # Lessons 36: what did not happen, never how many; which is the tooltip.
    paths = []
    for name in ("program.exe", "setup.dmg", "notes.xyz"):
        (tmp_path / name).write_text("hello", encoding="utf-8")
        paths.append(str(tmp_path / name))
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths(paths)
    assert world.dialog._status.text() == (
        "Some files weren’t added. Only images, videos, audio and documents can be added: "
        "choose one of those.")
    assert world.dialog._status.toolTip() == "program.exe\nsetup.dmg\nnotes.xyz"


@pytest.mark.parametrize("missed", ["mirror_failed", "server_skipped"])
def test_a_server_that_did_not_keep_a_copy_says_nothing_as_the_file_landed(
        tmp_path, monkeypatch, missed):
    # Lessons 36: the file is on the others, so the upload worked.
    path = tmp_path / "Garden.png"
    path.write_bytes(PNG_BYTES)
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    getattr(world.store, missed).emit("Garden.png", "members.example", "NETWORK_UNAVAILABLE")
    world.store.upload_finished.emit("Garden.png", media(3))
    assert world.dialog._status.text() == "Anyone with the link can see the files you add here."
    assert world.dialog._status.toolTip() == ""


# --------------------------------------------------------------------------- #
# Delete, copy, save
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("how", ["key", "command_backspace", "menu"])
def test_delete_asks_first_however_it_is_asked_for(tmp_path, monkeypatch, how):
    one = media(1)
    world = build(tmp_path, monkeypatch, FakeStore([one], names={one.hash: "Garden.jpg"}))
    alert, shown = fake_alert(click="Cancel")
    monkeypatch.setattr(alerts, "Alert", alert)
    select(world, one.hash)
    if how == "key":
        press(world.grid, Qt.Key.Key_Delete)
    elif how == "command_backspace":
        press(world.grid, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
    else:
        world.dialog._delete()
    assert shown[0].title == "Delete “Garden.jpg”?"
    assert shown[0].message == (
        "Anything you published with it will no longer show it. You can’t undo this action.")
    assert ("delete", one.hash) not in world.store.calls

    alert, shown = fake_alert(click="Delete")
    monkeypatch.setattr(alerts, "Alert", alert)
    world.dialog._delete()
    assert ("delete", one.hash) in world.store.calls


def test_a_file_a_server_still_has_is_never_called_gone(tmp_path, monkeypatch):
    one = media(1)
    world = build(tmp_path, monkeypatch, FakeStore([one], names={one.hash: "Garden.jpg"}))
    world.store.delete_failed.emit(one.hash, "NETWORK_UNAVAILABLE")
    assert world.dialog._status.text() == (
        "“Garden.jpg” wasn’t deleted. Couldn’t reach your media server: check your "
        "internet connection and try again.")


def test_copy_link_copies_every_selected_address(tmp_path, monkeypatch):
    one, two = media(1, day=1), media(2, day=2)
    world = build(tmp_path, monkeypatch, FakeStore([one, two]))
    world.grid.selectAll()
    press(world.grid, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == f"{one.url}\n{two.url}"


def _save_video(tmp_path, monkeypatch, *, servers=(SERVER,)):
    video = media(4, mime="video/mp4", size=60 * 1024 * 1024)
    video.urls = [{"server": s, "url": f"{s}/{video.hash}.mp4"} for s in servers]
    video.url = video.urls[0]["url"]
    world = build(tmp_path, monkeypatch, FakeStore([video], names={video.hash: "Talk.mp4"}))
    target = tmp_path / "out" / "Talk.mp4"
    target.parent.mkdir()
    monkeypatch.setattr(dialog_module.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    select(world, video.hash)
    world.dialog._save_as()
    return world, video, target


def test_save_as_downloads_a_large_video_with_progress_and_cancel(tmp_path, monkeypatch):
    """Not through the thumbnail cache, whose cap is far below a video's
    size: streamed to the file, bounded by the file's own size, with a
    determinate bar and Cancel at the foot of the window."""
    world, video, target = _save_video(tmp_path, monkeypatch)
    [job] = world.loader.downloads
    assert (job.sha, str(job.target), job.size) == (video.hash, str(target), video.size)
    assert world.loader.loads == []
    job.progress.emit(30 * 1024 * 1024, video.size)
    assert not world.dialog._save_bar.isHidden() and world.dialog._save_bar.value() == 50
    assert world.dialog._status.text() == "Saving “Talk.mp4”…"
    world.dialog._save_stop.click()
    assert job.cancelled
    job.finished.emit("cancelled")
    assert world.dialog._save_bar.isHidden() and world.dialog._save_stop.isHidden()
    assert world.dialog._status.text() == "Anyone with the link can see the files you add here."


def test_save_as_tries_the_next_server_and_then_gives_the_real_reason(tmp_path, monkeypatch):
    world, video, _target = _save_video(tmp_path, monkeypatch, servers=(SERVER, MEMBERS))
    world.loader.downloads[0].finished.emit("missing")
    assert world.loader.downloads[1].url.startswith(MEMBERS)
    world.loader.downloads[1].finished.emit("too_large")
    assert world.dialog._status.text() == (
        "“Talk.mp4” wasn’t saved, as a media server sent more than the file: try again later.")


@pytest.mark.parametrize("why, said", [
    ("network", "“Talk.mp4” couldn’t be downloaded: check your internet connection and try again."),
    ("missing", "“Talk.mp4” isn’t on your media servers anymore, so it couldn’t be saved."),
    ("server", "“Talk.mp4” couldn’t be downloaded, as your media server didn’t send it: try "
               "again later."),
    ("wrong_file", "“Talk.mp4” wasn’t saved, as a media server returned the wrong file: try "
                   "again later."),
    ("write", "“Talk.mp4” couldn’t be saved there: choose another folder."),
])
def test_save_as_failures_say_their_own_reason(tmp_path, monkeypatch, why, said):
    world, _video, _target = _save_video(tmp_path, monkeypatch)
    world.loader.downloads[0].finished.emit(why)
    assert world.dialog._status.text() == said


def test_saving_a_private_file_saves_the_file_not_its_envelope(tmp_path, monkeypatch):
    envelope = sealed(b"the real picture")
    private = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10)
    blob = PrivateBlob(sha256=private.hash, key_hex=KEY, mime="image/png", name="Secret.png")
    world = build(tmp_path, monkeypatch, FakeStore([private]),
                  visibility=MediaVisibility(library=FakeLibrary([blob])))
    world.loader.cache_path(private.hash).write_bytes(envelope)    # cached: no download
    target = tmp_path / "Secret.png"
    monkeypatch.setattr(dialog_module.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    select(world, private.hash)
    world.dialog._save_as()
    assert target.read_bytes() == b"the real picture"


# --------------------------------------------------------------------------- #
# Picking
# --------------------------------------------------------------------------- #

def test_return_inserts_the_chosen_file_with_its_alt_text(tmp_path, monkeypatch):
    one = media(1)
    world = build(tmp_path, monkeypatch, FakeStore([one]), pick=True)
    picked = []
    world.dialog.file_picked.connect(lambda m, alt: picked.append((m.hash, alt)))
    select(world, one.hash)
    world.dialog._alt_edit.setText("A garden")
    press(world.grid, Qt.Key.Key_Return)
    assert picked == [(one.hash, "A garden")]


def test_a_pick_has_the_type_its_tile_shows(tmp_path, monkeypatch):
    """A server that calls a video a byte stream does not make it one: the
    file is handed on as what its name says it is."""
    clip = media(1, mime="application/octet-stream")
    world = build(tmp_path, monkeypatch, FakeStore([clip], names={clip.hash: "Talk.mp4"}),
                  pick=True)
    picked = []
    world.dialog.file_picked.connect(lambda m, alt: picked.append(m.mime_type))
    select(world, clip.hash)
    assert not world.dialog._alt_edit.isEnabled()      # alt text describes pictures
    press(world.grid, Qt.Key.Key_Return)
    assert picked == ["video/mp4"]


def test_the_picker_offers_every_kind_of_file(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]), pick=True)
    assert world.dialog.windowTitle() == "Insert from Media Library"
    assert world.dialog._filter_combo.currentData() == "all"


def test_browsing_insert_in_document_puts_the_file_into_the_document(tmp_path, monkeypatch):
    one = media(1)
    world = build(tmp_path, monkeypatch, FakeStore([one]))
    inserted, open_document = [], [True]
    world.dialog.set_inserter(lambda m, alt: inserted.append(m.hash), lambda: open_document[0])
    select(world, one.hash)
    menu = QMenu()
    world.dialog._add_file_commands(menu, world.grid.selected_tiles())
    [insert] = [action for action in menu.actions() if action.text() == "Insert in Document"]
    assert insert.isEnabled()
    insert.trigger()
    assert inserted == [one.hash] and world.dialog.isVisible()      # the library stays open
    open_document[0] = False
    menu = QMenu()
    world.dialog._add_file_commands(menu, world.grid.selected_tiles())
    assert not [a for a in menu.actions() if a.text() == "Insert in Document"][0].isEnabled()


def test_a_file_nobody_could_check_is_not_inserted_and_it_says_why(tmp_path, monkeypatch):
    one = media(1)
    library = FakeLibrary(vouches=False)
    world = build(tmp_path, monkeypatch, FakeStore([one]), pick=True,
                  visibility=MediaVisibility(library=library), library=library)
    picked = []
    world.dialog.file_picked.connect(lambda *a: picked.append(a))
    select(world, one.hash)
    assert not world.dialog._insert_button.isEnabled()
    assert not world.dialog._pick_notice.isHidden()
    assert world.dialog._pick_notice_text.text() == (
        "This image can’t be used until your signer app lets MyEditor check whether "
        "it is private.")
    press(world.grid, Qt.Key.Key_Return)
    assert picked == []
    world.dialog._pick_notice_button.click()
    assert library.refreshed == 1


def test_while_the_check_runs_the_picker_says_one_moment(tmp_path, monkeypatch):
    one = media(1)
    library = FakeLibrary(vouches=False, loading=True)
    world = build(tmp_path, monkeypatch, FakeStore([one]), pick=True,
                  visibility=MediaVisibility(library=library), library=library)
    select(world, one.hash)
    assert world.dialog._pick_notice_text.text() == (
        "MyEditor is still checking whether this image is private. One moment.")
    assert world.dialog._pick_notice_button.isHidden()


def test_the_library_itself_says_nothing_about_privacy(tmp_path, monkeypatch):
    library = FakeLibrary(vouches=False)
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]),
                  visibility=MediaVisibility(library=library), library=library)
    select(world, f"{1:064x}")
    assert world.dialog._pick_notice.isHidden()


# --------------------------------------------------------------------------- #
# Private pictures
# --------------------------------------------------------------------------- #

def test_a_private_picture_is_fetched_despite_its_type_and_shown(tmp_path, monkeypatch):
    envelope = sealed()
    private = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}",
                        mime_type="application/octet-stream", size=10)
    blob = PrivateBlob(sha256=private.hash, key_hex=KEY, mime="image/png", name="Secret.png")
    world = build(tmp_path, monkeypatch, FakeStore([private]),
                  visibility=MediaVisibility(library=FakeLibrary([blob])))
    world.dialog._load_visible()
    assert world.loader.loads == [private.hash]
    world.loader.cache_path(private.hash).write_bytes(envelope)
    before = sorted(p.name for p in world.loader.root.iterdir())
    world.loader.failed.emit(private.hash, "not an image")
    assert tiles(world)[0].pixmap is not None
    # Nothing decrypted is written where an export would find it.
    assert sorted(p.name for p in world.loader.root.iterdir()) == before
    assert world.loader.cache_path(private.hash).read_bytes() == envelope


def test_a_private_video_is_not_downloaded_to_be_refused(tmp_path, monkeypatch):
    envelope = sealed()
    private = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10)
    blob = PrivateBlob(sha256=private.hash, key_hex=KEY, mime="video/mp4", name="Clip.mp4")
    world = build(tmp_path, monkeypatch, FakeStore([private]),
                  visibility=MediaVisibility(library=FakeLibrary([blob])))
    world.dialog._load_visible()
    assert world.loader.loads == []


def test_an_opaque_public_file_is_not_downloaded_on_a_hunch(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch, FakeStore([media(1, mime="application/pdf")]))
    world.dialog._load_visible()
    assert world.loader.loads == []


# --------------------------------------------------------------------------- #
# Storage, servers, and the window itself
# --------------------------------------------------------------------------- #

def test_storage_is_one_quiet_line_when_it_is_known(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch, FakeStore(quota=5 * GB, used=int(1.2 * GB)))
    assert world.dialog._storage.text() == "1.2 GB of 5 GB used"
    world = build(tmp_path, monkeypatch, FakeStore(quota=5 * GB, used=GB, listing_ok=False))
    assert world.dialog._storage.isHidden()
    world = build(tmp_path, monkeypatch, FakeStore())
    assert world.dialog._storage.isHidden()


def test_storage_text_says_nothing_it_does_not_know():
    assert storage_text(GB, 5 * GB, None) == ""
    assert storage_text(GB, 5 * GB, ServerListing(origin=SERVER, ok=False)) == ""
    almost = storage_text(int(4.6 * GB), 5 * GB, ServerListing(origin=SERVER))
    assert almost == "Almost full: 4.6 GB of 5 GB used"


def test_media_servers_is_in_the_action_menu(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch)
    asked = []
    world.dialog.servers_requested.connect(lambda: asked.append(True))
    world.dialog._fill_actions_menu()
    action = [a for a in world.dialog._actions_menu.actions() if a.text() == "Media Servers…"][0]
    action.trigger()
    assert asked == [True]


def test_space_opens_quick_look_and_space_closes_it(tmp_path, monkeypatch):
    one = media(1)
    world = build(tmp_path, monkeypatch, FakeStore([one], names={one.hash: "Garden.jpg"}))
    select(world, one.hash)
    press(world.grid, Qt.Key.Key_Space)
    look = world.dialog._quick_look
    assert look.isVisible() and look.name.text() == "Garden.jpg"
    assert look.detail.text() == "Image  ·  1000 B  ·  May 28, 2026"
    press(look, Qt.Key.Key_Space)
    assert not look.isVisible()


def test_every_control_has_a_name(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch, pick=True)
    d = world.dialog
    for widget in (d._filter_combo, d._sort_combo, d._search, d._actions_button, d._grid,
                   d._alt_edit):
        assert widget.accessibleName(), widget


def test_a_closed_library_leaves_nothing_connected_to_the_store(tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc[1]))
    world = build(tmp_path, monkeypatch)
    world.dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(world.dialog)
    for signal in (world.store.fetch_started, world.store.fetch_finished,
                   world.store.listings_changed, world.store.library_changed):
        signal.emit()
    world.store.fetch_error.emit("NETWORK_UNAVAILABLE")
    world.store.upload_failed.emit("x", "y")
    assert errors == []


@pytest.mark.parametrize("module", ["media_library_dialog", "media_grid", "media_preview",
                                    "media_servers_dialog"])
def test_no_colour_is_written_into_the_media_windows(module):
    """Every colour comes from the palette, theme.py or constants.py."""
    source = (Path(__file__).parent.parent / "nostr" / "ui" / f"{module}.py").read_text(encoding="utf-8")
    assert not re.findall(r"#[0-9A-Fa-f]{6}\b", source)


# --------------------------------------------------------------------------- #
# Stop Sharing
# --------------------------------------------------------------------------- #

def _shared_original(tmp_path, monkeypatch):
    envelope = sealed()
    original = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10)
    copy_sha = "c" * 64
    blob = PrivateBlob(sha256=original.hash, key_hex=KEY, mime="image/png", name="Beach.png")
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(sha256=copy_sha, url=f"{MEMBERS}/{copy_sha}.png",
                             servers=[MEMBERS], source_hash=original.hash))
    # The copy's record names MEMBERS only; SERVER is configured.
    visibility = MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)
    world = build(tmp_path, monkeypatch, FakeStore([original]), visibility=visibility)
    world.store.delete_file = lambda sha, servers=(), best_effort=(): world.store.calls.append(
        ("delete", sha, tuple(servers), tuple(best_effort)))
    return world, original, copy_sha, ledger


def _menu_texts(world):
    world.dialog._fill_actions_menu()
    return [a.text() for a in world.dialog._actions_menu.actions() if a.text()]


def test_stop_sharing_is_offered_only_for_a_shared_copy(tmp_path, monkeypatch):
    world, original, _copy, _ledger = _shared_original(tmp_path, monkeypatch)
    select(world, original.hash)
    assert "Stop Sharing" in _menu_texts(world)
    plain = build(tmp_path, monkeypatch, FakeStore([media(1)]))
    select(plain, f"{1:064x}")
    assert "Stop Sharing" not in _menu_texts(plain)


def test_stop_sharing_asks_then_deletes_the_copy_where_it_is(tmp_path, monkeypatch):
    world, original, copy_sha, ledger = _shared_original(tmp_path, monkeypatch)
    select(world, original.hash)
    alert, shown = fake_alert(click="Stop Sharing")
    monkeypatch.setattr(alerts, "Alert", alert)
    world.dialog._stop_sharing()
    assert shown[0].title == "Stop sharing “Beach.png”?"
    assert shown[0].message == (
        "Anyone with the link will no longer see it, and anything you published with it "
        "will no longer show it.")
    assert world.store.calls[-1][:2] == ("delete", copy_sha)
    # Every server the copy was put on (its record and its address): a
    # copy is not "no longer shared" while one of them has it. And every
    # server in use now, in case it is there unrecorded, which only
    # counts where it is listed.
    _delete, _sha, servers, best_effort = world.store.calls[-1]
    assert MEMBERS in servers and SERVER not in servers
    assert SERVER in best_effort

    world.store.file_deleted.emit(copy_sha)
    world.store.library_changed.emit()
    assert ledger.get(copy_sha) is None
    assert tiles(world)[0].private                # only the person again
    assert world.dialog._status.text() == "“Beach.png” is no longer shared."


def test_a_copy_a_server_still_has_is_still_shared_and_it_says_so(tmp_path, monkeypatch):
    world, original, copy_sha, ledger = _shared_original(tmp_path, monkeypatch)
    select(world, original.hash)
    monkeypatch.setattr(alerts, "Alert", fake_alert(click="Stop Sharing")[0])
    world.dialog._stop_sharing()
    world.store.delete_failed.emit(copy_sha, "NETWORK_UNAVAILABLE")
    assert ledger.get(copy_sha) is not None
    assert not tiles(world)[0].private
    assert world.dialog._status.text() == (
        "“Beach.png” is still shared. Couldn’t reach your media server: check your "
        "internet connection and try again.")


@pytest.mark.parametrize("unsharing", [True, False])
def test_a_server_that_refuses_the_account_is_not_a_reason_to_choose_another(
        tmp_path, monkeypatch, unsharing):
    """Choosing another server can't delete a file from the one it is on."""
    world, original, copy_sha, _ledger = _shared_original(tmp_path, monkeypatch)
    select(world, original.hash)
    if unsharing:
        monkeypatch.setattr(alerts, "Alert", fake_alert(click="Stop Sharing")[0])
        world.dialog._stop_sharing()
    world.store.delete_failed.emit(copy_sha if unsharing else original.hash, "AUTH_REJECTED")
    assert world.dialog._status.text() == (
        "“Beach.png” is still shared. A media server it’s on didn’t let your account "
        "delete it: try again with the account that added it." if unsharing else
        "“Beach.png” wasn’t deleted. A media server it’s on didn’t let your account "
        "delete it: try again with the account that added it.")


def test_deleting_a_shared_original_deletes_its_copy_too(tmp_path, monkeypatch):
    """Otherwise the copy stays public with no tile left to reach it."""
    world, original, copy_sha, _ledger = _shared_original(tmp_path, monkeypatch)
    select(world, original.hash)
    monkeypatch.setattr(alerts, "Alert", fake_alert(click="Delete")[0])
    world.dialog._delete()
    deleted = [call[1] for call in world.store.calls if call[0] == "delete"]
    assert deleted == [copy_sha, original.hash]


def test_a_start_or_cancel_before_the_tile_exists_is_kept(tmp_path, monkeypatch):
    """A small file can start (and be cancelled) inside upload_file,
    before the window has made its tile."""
    path = tmp_path / "Quick.png"
    path.write_bytes(PNG_BYTES)
    store = FakeStore()
    world = build(tmp_path, monkeypatch, store)
    store.upload_file = lambda p: (store.upload_started.emit("Quick.png"), "Quick.png")[1]
    world.dialog._add_paths([str(path)])
    assert world.model.tile("Quick.png").state == SENDING

    other = tmp_path / "Gone.png"
    other.write_bytes(PNG_BYTES)
    store.upload_file = lambda p: (store.upload_started.emit("Gone.png"),
                                   store.upload_cancelled.emit("Gone.png"), "Gone.png")[2]
    world.dialog._add_paths([str(other)])
    assert world.model.tile("Gone.png") is None


def test_the_smallest_window_cuts_no_toolbar_control(tmp_path, monkeypatch):
    """A longer translation widens the smallest window instead of
    cutting a button's or a pop-up's words."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(dialog_module, "ThumbnailLoader",
                        lambda parent=None: FakeLoader(cache, parent=parent))
    dialog = MediaLibraryDialog(store=FakeStore(), is_dark=False)
    dialog._add_button.setText("Hinzufügen zur Mediathek …")
    dialog._sort_combo.setItemText(0, "Neueste Objekte zuerst anzeigen")
    dialog.show()
    dialog.resize(100, 100)
    QCoreApplication.processEvents()
    for widget in (dialog._add_button, dialog._filter_combo, dialog._sort_combo,
                   dialog._actions_button):
        assert widget.width() >= widget.sizeHint().width(), widget


def test_a_file_the_editor_uploads_appears_in_an_open_library_at_once(tmp_path, monkeypatch):
    """A picture pasted into a document goes through the same store, so
    it is in an open library as soon as a server has it, with no
    listing in between. (Its progress shows in the document, not here.)"""
    from nostr.blossom.client import BlossomClient
    from nostr.blossom.settings import BlossomSettings
    from nostr.blossom.store import MediaStore
    from tests.blossom_fakes import (SERVER as BLOSSOM, FakeBlossomServer, FakeNam,
                                     FakeProfile, FakeSessionPool)
    settings = BlossomSettings(tmp_path / "blossom_servers.json")
    settings.set_custom_servers([BLOSSOM])
    nam = FakeNam(None, responder=FakeBlossomServer(BLOSSOM, require_auth=False))
    store = MediaStore(session_pool=FakeSessionPool(), profile_provider=FakeProfile,
                       settings=settings, client=BlossomClient(nam=nam))
    world = build(tmp_path, monkeypatch, store)
    nam.settle()                                   # the window's own listing: empty
    assert tiles(world) == []

    sha = sha_of(PNG_BYTES)
    store.upload_bytes(PNG_BYTES, name=f"asset:{sha}", mime_type="image/png")
    assert tiles(world) == []                      # its progress is the document's
    nam.settle()
    assert [tile.key for tile in tiles(world)] == [sha]
    assert len([c for c in nam.calls if c[0] == "get"]) == 1      # no second listing


def test_a_library_opened_later_shows_the_files_still_being_added(tmp_path, monkeypatch):
    """The store owns the uploads, not the window that started them: one
    that failed after its window closed is shown with its reason and
    Retry when the library opens again."""
    from nostr.blossom.store import AddedUpload
    path = tmp_path / "Holiday.png"
    path.write_bytes(PNG_BYTES)
    store = FakeStore()
    reason = "Couldn’t reach your media server: check your internet connection and try again."
    store.added = [AddedUpload(name="Holiday.png", mime="image/png", state="failed",
                               error=reason, path=str(path)),
                   AddedUpload(name="Clip.mp4", mime="video/mp4", state="sending")]
    meter = UploadProgress()
    meter.sending(400, 1000)
    store.jobs["Clip.mp4"] = types.SimpleNamespace(meter=meter)
    world = build(tmp_path, monkeypatch, store)
    failed, sending = world.model.tile("Holiday.png"), world.model.tile("Clip.mp4")
    assert (failed.state, failed.detail) == (FAILED, reason)
    assert failed.pixmap is not None
    assert (sending.state, sending.meter) == (SENDING, meter)      # its live indicator
    world.grid.retry_requested.emit("Holiday.png")
    assert ("retry", "Holiday.png") in store.calls


def test_a_picker_does_not_offer_media_servers(tmp_path, monkeypatch):
    """Choosing an image is not where servers are managed, and the
    article cover's picker has nobody to open the sheet: the item would
    be dead there."""
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]), pick=True)
    world.dialog._fill_actions_menu()
    texts = [a.text() for a in world.dialog._actions_menu.actions() if a.text()]
    assert "Media Servers…" not in texts and "Refresh" in texts


def _menu_action(world, text):
    world.dialog._fill_actions_menu()
    return next(a for a in world.dialog._actions_menu.actions() if a.text() == text)


def test_a_shared_originals_link_is_its_public_copys(tmp_path, monkeypatch):
    """The original's own address serves sealed bytes: a link to it shows
    nobody the picture. The public copy's does."""
    world, original, copy_sha, _ledger = _shared_original(tmp_path, monkeypatch)
    QApplication.clipboard().setText("")
    select(world, original.hash)
    _menu_action(world, "Copy Link").trigger()
    assert QApplication.clipboard().text() == f"{MEMBERS}/{copy_sha}.png"


def test_a_private_file_has_no_link_to_hand_out(tmp_path, monkeypatch):
    envelope = sealed()
    private = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10)
    blob = PrivateBlob(sha256=private.hash, key_hex=KEY, mime="image/png", name="Secret.png")
    world = build(tmp_path, monkeypatch, FakeStore([private]),
                  visibility=MediaVisibility(library=FakeLibrary([blob])))
    QApplication.clipboard().setText("before")
    select(world, private.hash)
    assert not _menu_action(world, "Copy Link").isEnabled()
    assert not _menu_action(world, "Open in Browser").isEnabled()
    press(world.grid, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "before"


def test_a_listing_that_may_be_incomplete_is_said_quietly(tmp_path, monkeypatch):
    """Not an error and not the connection: some files may be missing
    from the list, and Refresh is the way to look again."""
    store = FakeStore([media(1)])
    world = build(tmp_path, monkeypatch, store)
    store.truncated = True
    store.fetch_finished.emit()
    assert world.dialog._status.text() == (
        "Your media servers may not have listed every file: choose Refresh to look again.")
    assert world.dialog._status.property("tone") == ""


@pytest.mark.parametrize("search", ["", "nothing like it"])
def test_files_dropped_where_the_grid_would_be_are_added(tmp_path, monkeypatch, search):
    """On an empty library (the first file is the one most likely dragged
    in) and on "No items match" alike."""
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    path = tmp_path / "Garden.png"
    path.write_bytes(PNG_BYTES)
    world = build(tmp_path, monkeypatch, FakeStore([media(1)] if search else []))
    world.dialog._search.setText(search)
    assert world.dialog._stack.currentWidget() is world.dialog._empty
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(path))])
    target = world.dialog._empty
    enter = QDragEnterEvent(target.rect().center(), Qt.DropAction.CopyAction, data,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(target.rect().center()), Qt.DropAction.CopyAction, data,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, drop)
    assert ("upload_file", "Garden.png") in world.store.calls


def test_an_iphone_photo_is_an_image_the_library_takes(tmp_path, monkeypatch):
    """HEIC is what an iPhone takes photos in; AVIF and HEIF are images
    too. They upload like any image (a preview only where the system
    can draw them, and the kind's own mark where it cannot)."""
    import media_files
    assert media_files.type_of("IMG_0001.HEIC") == "image/heic"
    assert media_files.type_of("a.heif") == "image/heif"
    assert media_files.type_of("a.avif") == "image/avif"
    path = tmp_path / "IMG_0001.HEIC"
    path.write_bytes(b"heic bytes")
    world = build(tmp_path, monkeypatch)
    world.dialog._add_paths([str(path)])
    assert ("upload_file", "IMG_0001.HEIC") in world.store.calls


def test_the_add_button_names_every_kind_the_library_takes(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch)
    assert world.dialog._add_button.toolTip() == (
        "Add images, videos, audio or files from your computer")


def test_the_add_dialog_names_every_kind_the_library_takes(tmp_path, monkeypatch):
    seen = {}

    def choose(parent, title, folder, filters):
        seen["filters"] = filters
        return [], ""

    monkeypatch.setattr(dialog_module.QFileDialog, "getOpenFileNames", choose)
    world = build(tmp_path, monkeypatch)
    world.dialog._choose_files()
    assert seen["filters"].startswith("Images, Videos, Audio and Files (")
    assert "*.heic" in seen["filters"] and "*.pdf" in seen["filters"]


def test_a_picture_the_system_cannot_draw_shows_its_kind(tmp_path, monkeypatch):
    heic = media(5, mime="image/heic")
    world = build(tmp_path, monkeypatch, FakeStore([heic]))
    world.loader.failed.emit(heic.hash, "not an image")
    assert world.model.tile(heic.hash).no_picture


def test_a_delete_no_server_took_says_it_was_not_deleted(tmp_path, monkeypatch):
    one = media(1)
    one.urls = [{"server": SERVER, "url": one.url}, {"server": MEMBERS, "url": one.url}]
    world = build(tmp_path, monkeypatch, FakeStore([one], names={one.hash: "Garden.jpg"}))
    monkeypatch.setattr(alerts, "Alert", fake_alert(click="Delete")[0])
    select(world, one.hash)
    world.dialog._delete()
    world.store.delete_failed.emit(one.hash, "NETWORK_UNAVAILABLE")    # both servers kept it
    assert world.dialog._status.text().startswith("“Garden.jpg” wasn’t deleted.")

    world.dialog._delete()
    one.urls = one.urls[1:]                                             # one server took it
    world.store.delete_failed.emit(one.hash, "NETWORK_UNAVAILABLE")
    assert world.dialog._status.text().startswith(
        "“Garden.jpg” is still on one of your media servers.")


def test_escape_clears_the_search_as_in_the_finder(tmp_path, monkeypatch):
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]))
    world.dialog._search.setFocus()
    world.dialog._search.setText("garden")
    press(world.dialog._search, Qt.Key.Key_Escape)
    assert world.dialog._search.text() == "" and world.dialog.isVisible()


def test_escape_does_not_close_the_browsing_library(tmp_path, monkeypatch):
    """A Finder or Photos window does not close on Escape; a picker
    (a question being answered) does, as Cancel."""
    world = build(tmp_path, monkeypatch, FakeStore([media(1)]))
    press(world.grid, Qt.Key.Key_Escape)
    press(world.dialog._search, Qt.Key.Key_Escape)
    assert world.dialog.isVisible()
    picker = build(tmp_path, monkeypatch, FakeStore([media(1)]), pick=True)
    press(picker.grid, Qt.Key.Key_Escape)
    assert not picker.dialog.isVisible()


def test_return_in_the_search_field_goes_to_the_results_not_insert(tmp_path, monkeypatch):
    one = media(1)
    world = build(tmp_path, monkeypatch, FakeStore([one]), pick=True)
    picked = []
    world.dialog.file_picked.connect(lambda *a: picked.append(a))
    select(world, one.hash)
    world.dialog._search.setFocus()
    press(world.dialog._search, Qt.Key.Key_Return)
    assert picked == [] and world.dialog.isVisible()
    assert world.grid.hasFocus() or QApplication.focusWidget() is world.grid


def test_deleting_a_private_file_says_the_app_that_keeps_it_loses_it(tmp_path, monkeypatch):
    """A private file belongs to another app's private library (Lotus,
    say): deleting it here takes it from there too."""
    envelope = sealed()
    private = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10)
    blob = PrivateBlob(sha256=private.hash, key_hex=KEY, mime="image/png", name="Secret.png")
    world = build(tmp_path, monkeypatch, FakeStore([private]),
                  visibility=MediaVisibility(library=FakeLibrary([blob])))
    alert, shown = fake_alert(click="Cancel")
    monkeypatch.setattr(alerts, "Alert", alert)
    select(world, private.hash)
    world.dialog._delete()
    assert shown[0].message == (
        "It’s also gone from the other app that keeps it privately. You can’t undo "
        "this action.")


def test_in_german_the_save_progress_is_named_as_a_progress(tmp_path, monkeypatch):
    """VoiceOver reads the bar's name: a process, not the Save command."""
    import i18n
    i18n.install("de")
    try:
        world = build(tmp_path, monkeypatch)
        assert world.dialog._save_bar.accessibleName() == "Speichervorgang"
        assert world.dialog._save_stop.accessibleName() == "Speichern stoppen"
    finally:
        i18n.install("en")


@pytest.mark.parametrize("tiles", [2, 5, 8])
def test_the_grid_settles_whatever_its_width(tiles):
    """A row that filled the grid to its last pixel wrapped, the extra row
    brought the scroll bar, the bar made the row fit again and went: the
    whole library flickered, without end, after the one upload that made
    the count land on such a width."""
    from PySide6.QtCore import QEventLoop, QTimer
    from nostr.ui.media_grid import MediaGrid, MediaModel, Tile

    def spin(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    resized = []
    grid = MediaGrid(is_dark=False)
    model = MediaModel()
    grid.setModel(model)
    grid.show()
    grid.viewport().installEventFilter(watcher := _ResizeWatcher(resized))
    model.set_tiles([Tile(key=str(i), name=f"{i}.png", kind="image") for i in range(tiles)])
    cell = grid.delegate.cell_size()
    try:
        for columns in range(1, tiles + 1):
            # Each column count's widths, where the row fills exactly.
            for width in (columns * cell.width(), columns * cell.width() + 18):
                grid.resize(width, int(cell.height() * 1.95))
                spin(10)
                resized.clear()
                spin(40)
                assert len(resized) <= 1, (tiles, width)
    finally:
        grid.viewport().removeEventFilter(watcher)
        grid.close()
        grid.deleteLater()


class _ResizeWatcher(QObject):
    def __init__(self, seen: list) -> None:
        super().__init__()
        self._seen = seen

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self._seen.append(event.size().width())
        return False


# --------------------------------------------------------------------------- #
# A submission's encrypted files, with the real store (RG-M2b 6)
# --------------------------------------------------------------------------- #

def _real_store(tmp_path, cache_dir):
    from nostr.blossom.client import BlossomClient
    from nostr.blossom.settings import BlossomSettings
    from nostr.blossom.store import MediaStore
    from nostr.ui.thumbnail_loader import ThumbnailLoader
    from tests.blossom_fakes import (SERVER as BLOSSOM, FakeBlossomServer, FakeNam,
                                     FakeProfile, FakeSessionPool, FakeSigner)
    settings = BlossomSettings(tmp_path / "servers.json")
    settings.set_custom_servers([BLOSSOM])
    server = FakeBlossomServer(BLOSSOM)
    nam = FakeNam(None, responder=server)
    store = MediaStore(session_pool=FakeSessionPool(FakeSigner()), profile_provider=FakeProfile,
                       settings=settings, client=BlossomClient(nam=nam),
                       blob_cache=ThumbnailLoader(cache_dir=cache_dir, nam=FakeNam()))
    return store, server, nam


def _sealed_file():
    from nostr.standup.drafting import LocalPicture, encrypt_pictures
    picture, = encrypt_pictures([LocalPicture(key="k", name="cafe.jpg", mime="image/jpeg",
                                              data=PNG_BYTES)])
    return picture.cipher


def test_submission_files_are_apart_never_offered_and_deleting_one_warns(tmp_path,
                                                                          monkeypatch):
    store, _server, nam = _real_store(tmp_path, tmp_path / "blobs")
    sealed = _sealed_file()
    store.upload_bytes(PNG_BYTES, name="garden.png", mime_type="image/png")
    store.upload_bytes(sealed, name="a.smf1", force=True, submission=True)
    nam.settle()
    sealed_sha, picture_sha = sha_of(sealed), sha_of(PNG_BYTES)
    assert set(store.files) == {sealed_sha, picture_sha}
    world = build(tmp_path, monkeypatch, store)
    assert [tile.key for tile in tiles(world)] == [picture_sha]        # not among All Items
    combo = world.dialog._filter_combo
    combo.setCurrentIndex(combo.findData("submission"))
    assert [(tile.key, tile.name) for tile in tiles(world)] == [
        (sealed_sha, "Encrypted file of a submission")]
    select(world, sealed_sha)
    assert not world.dialog._can_insert() and world.dialog._link_of(tiles(world)[0]) == ""
    alert, shown = fake_alert(click="Cancel")
    monkeypatch.setattr(alerts, "Alert", alert)
    world.dialog._delete()
    assert shown[0].message == ("The editorial team can no longer open this file of your "
                                "submission. You can’t undo this action.")
    picker = build(tmp_path, monkeypatch, store, pick=True)
    assert picker.dialog._filter_combo.findData("submission") == -1
    assert [tile.key for tile in tiles(picker)] == [picture_sha]      # never insertable


def test_after_a_restart_a_submission_file_is_known_by_its_first_bytes(tmp_path):
    first, _server, nam = _real_store(tmp_path, tmp_path / "blobs")
    sealed = _sealed_file()
    first.upload_bytes(sealed, name="a.smf1", force=True, submission=True)
    nam.settle()
    again, _server, _nam = _real_store(tmp_path, tmp_path / "blobs")   # a new session
    assert again.is_submission_file(sha_of(sealed))
    assert not again.is_submission_file(sha_of(PNG_BYTES))
    first._blob_cache = None                         # no cache: only what it sent itself
    assert first.is_submission_file(sha_of(sealed))
    first.clear()                                    # another account: nothing it sent
    assert not first.is_submission_file(sha_of(sealed))


# --------------------------------------------------------------------------- #
# On another computer: told by its first bytes, read from its server (RG-M2r 5)
# --------------------------------------------------------------------------- #

def _listed_from_elsewhere(tmp_path, body, *, mime="application/octet-stream", **server):
    """A file another computer uploaded: on the server, listed here, its
    bytes never cached here."""
    from tests.blossom_fakes import SERVER as BLOSSOM, descriptor
    store, fake, nam = _real_store(tmp_path, tmp_path / "blobs")
    for name, value in server.items():
        setattr(fake, name, value)
    sha = sha_of(body)
    fake.contents[sha] = body
    fake.blobs[sha] = descriptor(sha, server=BLOSSOM, size=len(body), mime=mime,
                                 url=f"{BLOSSOM}/{sha}")
    store.fetch(force=True)
    nam.settle()
    assert sha in store.files
    return store, fake, nam, sha


def test_on_another_computer_a_submission_file_is_told_before_it_is_offered(tmp_path,
                                                                            monkeypatch):
    store, server, nam, sha = _listed_from_elsewhere(tmp_path, _sealed_file())
    assert store.submission_state(sha) is None
    world = build(tmp_path, monkeypatch, store)
    [tile] = tiles(world)
    # Until its first bytes are read: neither inserted nor deleted plainly.
    assert tile.unsure and not tile.submission
    select(world, sha)
    assert not world.dialog._can_insert() and world.dialog._link_of(tile) == ""
    alert, shown = fake_alert(click="Cancel")
    monkeypatch.setattr(alerts, "Alert", alert)
    world.dialog._delete()
    assert shown[-1].message == ("If this is a file of one of your submissions, the editorial "
                                 "team can no longer open it. You can’t undo this action.")
    nam.settle()
    QApplication.processEvents()                     # laid out again on the next turn
    # Eight bytes asked for, by range, once: then it is in its own group.
    assert server.reads == [(sha, "bytes=0-7")]
    assert store.is_submission_file(sha) and tiles(world) == []
    combo = world.dialog._filter_combo
    combo.setCurrentIndex(combo.findData("submission"))
    assert [(t.key, t.name) for t in tiles(world)] == [(sha, "Encrypted file of a submission")]


def test_on_another_computer_a_plain_byte_stream_is_offered_once_told(tmp_path, monkeypatch):
    store, server, nam, sha = _listed_from_elsewhere(tmp_path, b"PK\x03\x04 a zip of notes")
    world = build(tmp_path, monkeypatch, store)
    nam.settle()
    [tile] = tiles(world)
    assert store.submission_state(sha) is False and not tile.unsure
    select(world, sha)
    assert world.dialog._can_insert() and world.dialog._link_of(tile) == tile.media.url
    alert, shown = fake_alert(click="Cancel")
    monkeypatch.setattr(alerts, "Alert", alert)
    world.dialog._delete()
    assert shown[-1].message.startswith("Anything you published with it")


def test_a_file_of_a_kind_is_never_read_to_be_told(tmp_path, monkeypatch):
    store, server, nam, sha = _listed_from_elsewhere(tmp_path, PNG_BYTES, mime="image/png")
    world = build(tmp_path, monkeypatch, store)
    nam.settle()
    assert store.submission_state(sha) is False and server.reads == []
    assert not tiles(world)[0].unsure


def test_a_server_that_sends_the_whole_file_is_stopped_after_the_first_bytes(tmp_path):
    from nostr.standup import crypto as scrypto
    sealed = _sealed_file() + bytes(4096)
    store, server, nam, sha = _listed_from_elsewhere(tmp_path, sealed, ignore_range=True)
    store.check_submission_file(sha)
    nam.settle()
    [reply] = [r for r in nam.issued if r.request().url().path() == f"/{sha}"]
    assert reply.aborted and reply._read == scrypto.MEDIA_HEADER_BYTES
    assert store.is_submission_file(sha)


def test_when_no_server_gives_the_first_bytes_the_picker_says_so_and_asks_again(tmp_path,
                                                                              monkeypatch):
    store, server, nam, sha = _listed_from_elsewhere(tmp_path, _sealed_file())
    body = server.contents.pop(sha)                  # the server answers 404
    picker = build(tmp_path, monkeypatch, store, pick=True)
    nam.settle()
    assert store.submission_check_failed(sha) and store.submission_state(sha) is None
    assert len(server.reads) == 1                    # not asked again by itself
    select(picker, sha)
    picker.dialog._refresh_pick_notice()
    assert picker.dialog._pick_notice_text.text() == (
        "This file can’t be used until its server answers.")
    assert not picker.dialog._pick_notice_button.isHidden()
    assert not picker.dialog._insert_button.isEnabled()
    server.contents[sha] = body
    picker.dialog._pick_notice_button.click()
    nam.settle()
    QApplication.processEvents()                     # laid out again on the next turn
    assert store.is_submission_file(sha)
    assert [t.key for t in tiles(picker)] == []      # never offered to insert


def test_an_answer_for_the_account_left_tells_nothing(tmp_path):
    store, server, nam, sha = _listed_from_elsewhere(tmp_path, _sealed_file())
    store.check_submission_file(sha)
    store.clear()
    nam.settle()
    assert store.submission_state(sha) is False      # not listed for this account
    assert sha not in store._sealed_known


# -- many files listed from another computer (RG-M2r2a 4) ----------------------------

def _many_from_elsewhere(tmp_path, count):
    """``count`` plain byte streams another computer uploaded: each must
    be read before it is offered."""
    from tests.blossom_fakes import SERVER as BLOSSOM, descriptor
    store, server, nam = _real_store(tmp_path, tmp_path / "blobs")
    shas = []
    for n in range(count):
        body = b"PK\x03\x04 notes " + bytes([n])
        sha = sha_of(body)
        server.contents[sha] = body
        server.blobs[sha] = descriptor(sha, server=BLOSSOM, size=len(body),
                                       mime="application/octet-stream", url=f"{BLOSSOM}/{sha}")
        shas.append(sha)
    store.fetch(force=True)
    nam.settle()
    return store, server, nam, shas


def _heads_issued(nam, shas):
    return [r for r in nam.issued if r.request().url().path().lstrip("/") in shas]


def test_only_a_few_files_are_read_at_once(tmp_path):
    from nostr.blossom.store import MAX_HEADS_AT_ONCE
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 10)
    for sha in shas:
        store.check_submission_file(sha)
    assert len(_heads_issued(nam, shas)) == MAX_HEADS_AT_ONCE
    nam.settle()
    assert len(_heads_issued(nam, shas)) == 10
    assert all(store.submission_state(sha) is False for sha in shas)


def test_a_new_listing_reads_again_what_was_on_its_way(tmp_path):
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 6)
    for sha in shas:
        store.check_submission_file(sha)
    store.fetch(force=True)                         # the answers are for the old listing
    nam.settle()
    assert all(store.submission_state(sha) is None for sha in shas)
    read = len(_heads_issued(nam, shas))
    for sha in shas:
        store.check_submission_file(sha)            # never stuck as being asked
    nam.settle()
    assert all(store.submission_state(sha) is False for sha in shas)
    # Each read once for the new listing: what waited for the old is gone.
    assert len(_heads_issued(nam, shas)) - read == len(shas)


def test_a_new_listing_stops_the_reads_on_their_way(tmp_path):
    # RG-M2r3 10: the reads of the old listing ran on, unaborted, for up
    # to 45 s, so each Refresh opened MAX_HEADS_AT_ONCE more sockets.
    from nostr.blossom.store import MAX_HEADS_AT_ONCE
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 10)
    for sha in shas:
        store.check_submission_file(sha)
    first = _heads_issued(nam, shas)
    assert len(first) == MAX_HEADS_AT_ONCE and not any(reply.aborted for reply in first)
    for _refresh in range(3):
        store.fetch(force=True)
        assert all(reply.aborted for reply in _heads_issued(nam, shas))
        for _turn in range(20):                      # the new listing comes
            listing = [r for r in nam.issued if not r.settled
                       and r.request().url().path().startswith("/list")]
            if not listing:
                break
            listing[0].finish()
        for sha in shas:
            store.check_submission_file(sha)
        running = [r for r in _heads_issued(nam, shas) if not r.aborted]
        assert len(running) == MAX_HEADS_AT_ONCE        # never more open at once
    nam.settle()
    assert all(store.submission_state(sha) is False for sha in shas)
    assert store._heads_reading == {}               # nothing kept for a read that ended


def test_another_account_stops_the_reads_on_their_way(tmp_path):
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 6)
    for sha in shas:
        store.check_submission_file(sha)
    store.clear()                                   # sign-out, another account
    assert all(reply.aborted for reply in _heads_issued(nam, shas))


def test_a_tile_made_again_in_place_is_drawn_again(tmp_path, monkeypatch):
    from dataclasses import replace
    world = build(tmp_path, monkeypatch, FakeStore([media(1), media(2)]))
    drawn = []
    world.model.dataChanged.connect(lambda top, _bottom, _roles=(): drawn.append(top.row()))
    second = tiles(world)[1]
    assert world.model.replace(replace(second, detail="told")) is True
    assert drawn == [1] and tiles(world)[1].detail == "told"
    assert world.model.replace(replace(second, key="ff" * 32)) is False and drawn == [1]


def test_the_grid_is_not_laid_out_again_for_each_file_told(tmp_path, monkeypatch):
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 12)
    world = build(tmp_path, monkeypatch, store)
    resets = []
    world.model.modelReset.connect(lambda: resets.append(True))
    nam.settle()
    QApplication.processEvents()
    assert resets == []                             # each tile made again in place
    assert len(tiles(world)) == 12 and all(not tile.unsure for tile in tiles(world))


def test_a_file_told_that_leaves_the_filter_is_taken_out_where_it_stands(tmp_path,
                                                                        monkeypatch):
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 3)
    picker = build(tmp_path, monkeypatch, store, pick=True)
    assert len(tiles(picker)) == 3
    calls, resets, removed = [], [], []
    original = picker.dialog._rebuild
    monkeypatch.setattr(picker.dialog, "_rebuild", lambda *a: (calls.append(1), original())[1])
    picker.model.modelReset.connect(lambda: resets.append(True))
    picker.model.rowsRemoved.connect(lambda _parent, first, _last: removed.append(first))
    # Three told to be submissions' files: never offered to insert, so
    # each leaves the grid at once, the others staying where they are.
    order = [t.key for t in tiles(picker)]
    store._sealed_known.update({sha: True for sha in shas})
    picker.dialog._on_submission_checked(shas[1])
    assert [t.key for t in tiles(picker)] == [key for key in order if key != shas[1]]
    for sha in (shas[0], shas[2]):
        picker.dialog._on_submission_checked(sha)
    QApplication.processEvents()
    assert calls == [] and resets == [] and len(removed) == 3 and tiles(picker) == []


def _many_sealed_and_plain(tmp_path, count):
    """``count`` files another computer uploaded, every other one a
    submission's: each read before it is offered."""
    from tests.blossom_fakes import SERVER as BLOSSOM, descriptor
    store, server, nam = _real_store(tmp_path, tmp_path / "blobs")
    shas = []
    for n in range(count):
        body = (_sealed_file() + bytes([n]) if n % 2 else b"PK\x03\x04 notes " + bytes([n]))
        sha = sha_of(body)
        server.contents[sha] = body
        server.blobs[sha] = descriptor(sha, server=BLOSSOM, size=len(body), uploaded=1000 + n,
                                       mime="application/octet-stream", url=f"{BLOSSOM}/{sha}")
        shas.append(sha)
    store.fetch(force=True)
    nam.settle()
    return store, server, nam, shas


@pytest.mark.parametrize("kind", ["all", "file", "submission"])
@pytest.mark.parametrize("sort", ["newest", "name"])
def test_files_told_one_per_turn_each_move_one_tile_and_end_as_a_layout_would(
        tmp_path, monkeypatch, kind, sort):
    # RG-M2r3 2: real replies come one per turn of the event loop. Each
    # answer makes its one tile again; the grid is never laid out again,
    # and in the end it is what a layout of it gives.
    from nostr.ui.media_library_dialog import MediaLibraryDialog
    store, server, nam, shas = _many_sealed_and_plain(tmp_path, 12)
    built = []
    real_tile = MediaLibraryDialog._file_tile
    monkeypatch.setattr(MediaLibraryDialog, "_file_tile",
                        lambda self, media: (built.append(media.hash), real_tile(self, media))[1])
    world = build(tmp_path, monkeypatch, store)
    for combo, value in ((world.dialog._filter_combo, kind), (world.dialog._sort_combo, sort)):
        combo.setCurrentIndex(combo.findData(value))
    QApplication.processEvents()
    resets = []
    world.model.modelReset.connect(lambda: resets.append(True))
    before = len(built)
    turns = 0
    while nam.settle_one(missing_ok=True):          # one reply per turn, as Qt has them
        QApplication.processEvents()
        turns += 1
        assert turns < 10 * len(shas)
    QApplication.processEvents()
    assert all(store.submission_state(sha) is not None for sha in shas)
    assert resets == [] and len(built) - before == len(shas)    # one tile per answer
    shown = [(t.key, t.name, t.submission, t.unsure) for t in tiles(world)]
    world.dialog._rebuild()
    assert shown == [(t.key, t.name, t.submission, t.unsure) for t in tiles(world)]
    assert len(shown) == {"all": 6, "file": 6, "submission": 6}[kind]


def _told(world, sha, monkeypatch, **changes):
    """``sha`` answered, its tile made again with ``changes``."""
    from dataclasses import replace
    real = MediaLibraryDialog._file_tile
    monkeypatch.setattr(MediaLibraryDialog, "_file_tile", lambda self, m: replace(
        real(self, m), **changes) if m.hash == sha else real(self, m))
    world.dialog._on_submission_checked(sha)


def _laid_out(world):
    shown = [(t.key, t.name) for t in tiles(world)]
    world.dialog._rebuild()
    return shown == [(t.key, t.name) for t in tiles(world)]


def test_a_file_told_whose_name_sorts_it_elsewhere_moves_after_the_uploads(tmp_path,
                                                                          monkeypatch):
    # RG-M2r3 2: by name, a tile named again goes where a layout puts it,
    # after the uploads on their way, whatever their names.
    from nostr.ui.media_grid import Tile
    world = build(tmp_path, monkeypatch, FakeStore([media(1), media(2), media(3)]))
    combo = world.dialog._sort_combo
    combo.setCurrentIndex(combo.findData("name"))
    world.dialog._jobs["zz.png"] = Tile(key="zz.png", name="zz.png", detail="", kind="image",
                                        state=SENDING)
    world.dialog._rebuild()
    resets = []
    world.model.modelReset.connect(lambda: resets.append(True))
    last = tiles(world)[-1]
    _told(world, last.key, monkeypatch, name="AAA first")
    assert resets == [] and [t.name for t in tiles(world)][:2] == ["zz.png", "AAA first"]
    assert _laid_out(world)


def test_the_last_file_told_away_leaves_the_plain_sentence(tmp_path, monkeypatch):
    store, server, nam, shas = _many_from_elsewhere(tmp_path, 2)
    picker = build(tmp_path, monkeypatch, store, pick=True)
    store._sealed_known.update({sha: True for sha in shas})
    for sha in shas:
        picker.dialog._on_submission_checked(sha)
    assert tiles(picker) == [] and picker.dialog._stack.currentWidget() is picker.dialog._empty


def test_a_hidden_copy_told_stays_out_of_the_grid(tmp_path, monkeypatch):
    envelope = sealed()
    original = MediaFile(hash=sha_of(envelope), url=f"{SERVER}/{sha_of(envelope)}", size=10,
                         uploaded_at_ms=2)
    copy = media(7)
    blob = PrivateBlob(sha256=original.hash, key_hex=KEY, mime="image/png", name="Beach.png")
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(sha256=copy.hash, url=copy.url, source_hash=original.hash))
    visibility = MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)
    world = build(tmp_path, monkeypatch, FakeStore([original, copy]), visibility=visibility)
    world.dialog._on_submission_checked(copy.hash)
    assert names(world) == ["Beach.png"]
