# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save As of a Media Library file, through the real loader and download,
against a small blob server on this computer.

What must hold, whatever goes wrong:

  Every save ends: it is saved, or the window says why not. A save that
  cannot begin (a folder that is gone or may not be written in) says so
  like any other, and the next save still runs.

  Nothing is left behind: the hidden part file a download is written to
  goes when the save ends, saved or not, even when the disk fills just
  as the last bytes are written.
"""

from __future__ import annotations

import errno
import hashlib
import http.server
import os
import sys
import threading
import time
import unicodedata
from types import SimpleNamespace

try:
    import resource
except ImportError:          # Windows
    resource = None

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import nostr.ui.media_library_dialog as dialog_module  # noqa: E402
from nostr.blossom.store import MediaFile  # noqa: E402
from nostr.media.filecrypto import encrypt_file  # noqa: E402
from nostr.media.media_visibility import MediaVisibility  # noqa: E402
from nostr.media.visibility import PrivateBlob, PublicLedger  # noqa: E402
from nostr.ui.media_library_dialog import MediaLibraryDialog  # noqa: E402
from nostr.ui import media_saver  # noqa: E402
from nostr.ui.media_saver import MediaSaver  # noqa: E402
from nostr.ui import thumbnail_loader  # noqa: E402
from nostr.ui.thumbnail_loader import ThumbnailLoader  # noqa: E402
from tests.test_media_library import FakeLibrary, FakeStore, select  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def close_windows():
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MediaLibraryDialog) and widget.isVisible():
            widget.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# --------------------------------------------------------------------------- #
# A blob server on this computer
# --------------------------------------------------------------------------- #

class _Blobs:
    files: dict = {}         # sha256 -> bytes
    delay = 0.0              # seconds after each 64 KiB
    asked: list = []         # (host, path) of each request


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        _Blobs.asked.append((self.headers.get("Host"), self.path))
        body = _Blobs.files.get(self.path.strip("/").split(".")[0])
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for start in range(0, len(body), 65536):
                self.wfile.write(body[start:start + 65536])
                self.wfile.flush()
                if _Blobs.delay:
                    time.sleep(_Blobs.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def quick_server():
    _Blobs.delay = 0.0
    yield
    _Blobs.delay = 0.0


def wait_for(condition, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return False


def blob(server, body: bytes, *, day: int = 0, mime: str = "video/mp4") -> MediaFile:
    sha = hashlib.sha256(body).hexdigest()
    _Blobs.files[sha] = body
    url = f"{server}/{sha}.mp4"
    return MediaFile(hash=sha, url=url, urls=[{"server": server, "url": url}],
                     mime_type=mime, size=len(body),
                     uploaded_at_ms=1_780_000_000_000 - day * 86_400_000)


def library(tmp_path, monkeypatch, files, *, names=None, saver=None, pick=False,
            visibility=None):
    """The library window, with the real loader and its cache in tmp_path."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(dialog_module, "ThumbnailLoader",
                        lambda parent=None: ThumbnailLoader(parent, cache_dir=cache))
    dialog = MediaLibraryDialog(store=FakeStore(files, names=names or {}), is_dark=False,
                                pick_mode=pick, saver=saver, visibility=visibility)
    dialog.resize(860, 560)
    dialog.show()
    QCoreApplication.processEvents()
    return SimpleNamespace(dialog=dialog, model=dialog._model, grid=dialog._grid)


def save_as(monkeypatch, world, media: MediaFile, target) -> None:
    monkeypatch.setattr(dialog_module.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    select(world, media.hash)
    world.dialog._save_as()


def saving(world) -> bool:
    return not world.dialog._save_bar.isHidden()


# --------------------------------------------------------------------------- #
# A save that cannot begin
# --------------------------------------------------------------------------- #

def test_a_download_that_cannot_begin_says_so_to_whoever_started_it(tmp_path, server):
    """The refusal comes after the caller has connected to the download,
    not before, when nobody hears it."""
    body = b"A" * 5000
    media = blob(server, body)
    loader = ThumbnailLoader(cache_dir=tmp_path / "cache")
    job = loader.download_to(media.url, media.hash, tmp_path / "gone" / "A.mp4",
                             size=len(body))
    done = []
    job.finished.connect(done.append)
    assert wait_for(lambda: done, 3)
    assert done == ["write"]


def test_cancel_ends_a_download_that_never_began(tmp_path, server):
    media = blob(server, b"C" * 100)
    loader = ThumbnailLoader(cache_dir=tmp_path / "cache")
    job = loader.download_to(media.url, media.hash, tmp_path / "gone" / "C.mp4", size=100)
    done = []
    job.finished.connect(done.append)
    job.cancel()
    wait_for(lambda: False, 0.2)
    assert done == ["cancelled"]


def test_save_as_into_a_folder_that_cannot_be_written_says_so_and_the_next_save_runs(
        tmp_path, monkeypatch, server):
    first, second = blob(server, b"A" * 5000), blob(server, b"B" * 5000, day=1)
    world = library(tmp_path, monkeypatch, [first, second])
    save_as(monkeypatch, world, first, tmp_path / "gone" / "A.mp4")
    assert wait_for(lambda: not saving(world), 3), "the save never ended"
    assert world.dialog._status.text() == "“A.mp4” couldn’t be saved there. Choose another folder."
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, second, out / "B.mp4")
    assert wait_for(lambda: (out / "B.mp4").exists(), 5)
    assert (out / "B.mp4").read_bytes() == b"B" * 5000


# --------------------------------------------------------------------------- #
# A disk that fills
# --------------------------------------------------------------------------- #

_REAL_FDOPEN = os.fdopen


class _FillingFile:
    """A part file on a disk that fills: the last bytes, written when the
    file is flushed and closed, don't fit."""

    def __init__(self, fd, mode):
        assert mode == "wb"
        self._file = _REAL_FDOPEN(fd, "wb")

    def write(self, data):
        return self._file.write(data)

    def fileno(self):
        return self._file.fileno()

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self):
        self._file.close()
        raise OSError(errno.ENOSPC, "No space left on device")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_a_disk_that_fills_at_the_end_ends_the_save_and_leaves_nothing(
        tmp_path, monkeypatch, server):
    big = blob(server, os.urandom(3 * 1024 * 1024))
    world = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"})
    monkeypatch.setattr(thumbnail_loader.os, "fdopen", _FillingFile)
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, big, out / "Big.mp4")
    assert wait_for(lambda: not saving(world), 10), "the save never ended"
    assert world.dialog._status.text() == (
        "“Big.mp4” couldn’t be saved: there isn’t enough space for it there. Free up some "
        "space or choose another folder.")
    assert sorted(p.name for p in out.iterdir()) == []


@pytest.mark.skipif(resource is None, reason="no file size limits here")
def test_a_file_size_limit_reached_at_the_end_ends_the_save_and_leaves_nothing(
        tmp_path, monkeypatch, server):
    """The real thing: the system refuses the last buffered bytes."""
    body = os.urandom(3 * 1024 * 1024)
    big = blob(server, body)
    world = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"})
    out = tmp_path / "out"
    out.mkdir()
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (len(body) - 1000, hard))
    try:
        save_as(monkeypatch, world, big, out / "Big.mp4")
        ended = wait_for(lambda: not saving(world), 10)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
    assert ended, "the save never ended"
    assert world.dialog._status.text() == (
        "“Big.mp4” couldn’t be saved there. Choose another folder.")
    assert sorted(p.name for p in out.iterdir()) == []


def test_a_file_too_big_to_hold_in_memory_ends_as_one_that_couldnt_be_written(
        tmp_path, monkeypatch, server):
    """Saved over a file that is written in place (a hard link, a folder
    that takes no new file), a download is held in memory: one too big
    for it says it couldn't be saved, as any file that couldn't be
    written, and no error escapes."""
    media = blob(server, b"a long film" * 100)
    world = library(tmp_path, monkeypatch, [media], names={media.hash: "Film.mp4"})
    escaped = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, *rest: escaped.append(kind))

    def no_memory(part, target):
        raise MemoryError

    monkeypatch.setattr(thumbnail_loader, "place_document", no_memory)
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, media, out / "Film.mp4")
    assert wait_for(lambda: not saving(world), 10), "the save never ended"
    assert world.dialog._status.text() == (
        "“Film.mp4” couldn’t be saved there. Choose another folder.")
    assert escaped == []
    assert sorted(p.name for p in out.iterdir()) == []


def test_a_file_saved_from_this_computer_onto_a_full_disk_says_so(tmp_path, monkeypatch, server):
    body = b"cached" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media], names={media.hash: "Clip.mp4"})
    world.dialog._loader.cache_path(media.hash).write_bytes(body)

    def full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(media_saver, "save_document", full)
    save_as(monkeypatch, world, media, tmp_path / "Clip.mp4")
    assert world.dialog._status.text() == (
        "“Clip.mp4” couldn’t be saved: there isn’t enough space for it there. Free up some "
        "space or choose another folder.")


# --------------------------------------------------------------------------- #
# Closing the window while it saves
# --------------------------------------------------------------------------- #

def _keeper(tmp_path):
    """What outlives the library windows (the main window), with the saver
    it gives them."""
    keeper = QObject()
    loader = ThumbnailLoader(keeper, cache_dir=tmp_path / "cache")
    return keeper, MediaSaver(loader, parent=keeper)


def folder(path) -> list:
    return sorted(p.name for p in path.iterdir())


@pytest.mark.parametrize("pick", [False, True])
def test_a_save_goes_on_after_the_window_that_asked_for_it_closes(
        tmp_path, monkeypatch, server, pick):
    """As in the Finder: closing the window doesn't stop the save."""
    _Blobs.delay = 0.02
    body = os.urandom(1024 * 1024)
    big = blob(server, body)
    keeper, saver = _keeper(tmp_path)
    done = []
    saver.finished.connect(lambda path, why: done.append((os.path.basename(path), why)))
    world = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"}, saver=saver,
                    pick=pick)
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, big, out / "Big.mp4")
    assert wait_for(lambda: world.dialog._save_bar.value() > 0, 10)
    world.dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert wait_for(lambda: done, 15)
    assert done == [("Big.mp4", "")]
    assert folder(out) == ["Big.mp4"] and (out / "Big.mp4").read_bytes() == body


def test_a_window_opened_while_a_save_runs_shows_it(tmp_path, monkeypatch, server):
    _Blobs.delay = 0.02
    big = blob(server, os.urandom(1024 * 1024))
    keeper, saver = _keeper(tmp_path)
    first = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"}, saver=saver)
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, first, big, out / "Big.mp4")
    assert wait_for(lambda: first.dialog._save_bar.value() > 0, 10)
    first.dialog.close()
    second = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"}, saver=saver)
    assert saving(second) and second.dialog._status.text() == "Saving “Big.mp4”…"
    assert wait_for(lambda: not saving(second), 15)
    assert folder(out) == ["Big.mp4"]


def test_a_window_saving_on_its_own_stops_the_save_when_it_closes_and_leaves_nothing(
        tmp_path, monkeypatch, server):
    """A window with nothing to outlive it (no main window) can't hand the
    save on: closing it stops the save, and its part file goes."""
    _Blobs.delay = 0.02
    big = blob(server, os.urandom(1024 * 1024))
    world = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"})
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, big, out / "Big.mp4")
    assert wait_for(lambda: world.dialog._save_bar.value() > 0, 10)
    [part] = folder(out)
    assert part.startswith(".") and part.endswith(".part")
    world.dialog.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    wait_for(lambda: False, 0.3)
    assert folder(out) == []


# --------------------------------------------------------------------------- #
# Files of the same name
# --------------------------------------------------------------------------- #

def save_to_folder(monkeypatch, world, path) -> None:
    monkeypatch.setattr(dialog_module.QFileDialog, "getExistingDirectory",
                        lambda *a, **k: str(path))
    world.grid.selectAll()
    world.dialog._save_as()


def test_save_to_folder_keeps_every_file_of_the_same_name(tmp_path, monkeypatch, server):
    """Files with no remembered name, added the same day, share the day as
    their name: each is kept, numbered as the Finder numbers a copy."""
    first, second = blob(server, b"first" * 100), blob(server, b"second" * 100)
    world = library(tmp_path, monkeypatch, [first, second])
    assert [tile.name for tile in world.model.tiles] == ["May 28, 2026", "May 28, 2026"]
    out = tmp_path / "out"
    out.mkdir()
    (out / "May 28, 2026.mp4").write_bytes(b"already here")
    save_to_folder(monkeypatch, world, out)
    assert wait_for(lambda: len(folder(out)) == 3 and not saving(world), 5)
    assert folder(out) == ["May 28, 2026 2.mp4", "May 28, 2026 3.mp4", "May 28, 2026.mp4"]
    assert (out / "May 28, 2026.mp4").read_bytes() == b"already here"
    assert {(out / name).read_bytes() for name in ("May 28, 2026 2.mp4", "May 28, 2026 3.mp4")} \
        == {b"first" * 100, b"second" * 100}


def test_save_to_folder_keeps_a_file_that_took_the_name_meanwhile(tmp_path, monkeypatch, server):
    _Blobs.delay = 0.02
    body = os.urandom(1024 * 1024)
    big = blob(server, body)
    other = blob(server, b"other", day=1)
    world = library(tmp_path, monkeypatch, [big, other], names={big.hash: "Big.mp4"})
    out = tmp_path / "out"
    out.mkdir()
    save_to_folder(monkeypatch, world, out)
    assert wait_for(lambda: world.dialog._save_bar.value() > 0, 10)
    (out / "Big.mp4").write_bytes(b"made meanwhile")
    assert wait_for(lambda: not saving(world) and len(folder(out)) == 3, 15)
    assert (out / "Big.mp4").read_bytes() == b"made meanwhile"
    assert (out / "Big 2.mp4").read_bytes() == body


def test_two_save_as_to_one_name_keep_both(tmp_path, monkeypatch, server):
    """The second Save As was not asked to replace the first: that file was
    not there yet."""
    _Blobs.delay = 0.02
    one, two = blob(server, os.urandom(512 * 1024)), blob(server, os.urandom(512 * 1024), day=1)
    world = library(tmp_path, monkeypatch, [one, two])
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, one, out / "Talk.mp4")
    save_as(monkeypatch, world, two, out / "Talk.mp4")
    assert wait_for(lambda: not saving(world) and len(folder(out)) == 2, 15)
    assert (out / "Talk.mp4").read_bytes() == _Blobs.files[one.hash]
    assert (out / "Talk 2.mp4").read_bytes() == _Blobs.files[two.hash]


# --------------------------------------------------------------------------- #
# The name a file is saved under
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, saved", [
    ("Report: Q3?.pdf", "Report- Q3-.pdf"),
    ('a<b>"c|d*e.png', "a-b--c-d-e.png"),
    ("back\\slash/and.png", "back-slash-and.png"),
    ("tab\there\x7f.png", "tab-here-.png"),
    (".hidden.png", "hidden.png"),
    ("trailing. .png", "trailing.png"),
    ("CON.png", "CON-.png"),
    ("lpt1", "lpt1-.mp4"),
    ("CON.tar.gz", "CON-.tar.gz"),
    ("Nul.backup.png", "Nul-.backup.png"),
    ("COM¹.png", "COM¹-.png"),
    ("lpt³.png", "lpt³-.png"),
    ("CONIN$.png", "CONIN$-.png"),
    ("conout$", "conout$-.mp4"),
    ("AUX .png", "AUX-.png"),
    ("CONSOLE.png", "CONSOLE.png"),
    ("com0.png", "com0-.png"),
    ("LPT0.txt.png", "LPT0-.txt.png"),
    ("...", "Video.mp4"),
    ("invoice\u202egnp.exe", "invoicegnp.exe"),
    ("a\u202ab\u202cc\u2066d\u2069e\u200ef\u200fg\u061ch\u200bi\ufeff.png", "abcdefghi.png"),
    ("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.png",) * 2,
    ("\U0001f469\u200d\U0001f4bb.png",) * 2,
])
def test_a_name_is_one_every_system_can_save(tmp_path, monkeypatch, server, name, saved):
    """As a system save dialog would have it: no character Windows refuses,
    nothing hidden, no device name, nothing a folder can't hold."""
    media = blob(server, name.encode())
    world = library(tmp_path, monkeypatch, [media], names={media.hash: name})
    assert dialog_module._save_name(world.model.tiles[0]) == saved


def test_a_long_name_is_cut_to_what_a_folder_holds_keeping_its_extension(
        tmp_path, monkeypatch, server):
    media = blob(server, b"long")
    world = library(tmp_path, monkeypatch, [media], names={media.hash: "ü" * 300 + ".mp4"})
    saved = dialog_module._save_name(world.model.tiles[0])
    assert saved.endswith("ü.mp4")
    # With room for the hidden part file it is downloaded to, and a number.
    assert len(f".{saved} 99.part".encode()) <= 255


# --------------------------------------------------------------------------- #
# A private file
# --------------------------------------------------------------------------- #

KEY = "5c" * 32


@pytest.mark.parametrize("key", ["6d" * 32, "zz" * 32, "abc"])
def test_a_private_file_its_key_does_not_open_says_so_and_asks_no_other_server(
        tmp_path, monkeypatch, server, key):
    """The file is the right one (its hash matched): the key is wrong, and
    no other server has a better one."""
    envelope = encrypt_file(b"the real picture" * 100, key_hex=KEY).envelope
    sha = hashlib.sha256(envelope).hexdigest()
    _Blobs.files[sha] = envelope
    other = server.replace("127.0.0.1", "localhost")
    private = MediaFile(hash=sha, url=f"{server}/{sha}",
                        urls=[{"server": server, "url": f"{server}/{sha}"},
                              {"server": other, "url": f"{other}/{sha}"}],
                        mime_type="application/octet-stream", size=len(envelope))
    blob_record = PrivateBlob(sha256=sha, key_hex=key, mime="image/png", name="Beach.png")
    visibility = MediaVisibility(library=FakeLibrary([blob_record]),
                                 ledger=PublicLedger(path=tmp_path / "public.json"))
    world = library(tmp_path, monkeypatch, [private], visibility=visibility)
    out = tmp_path / "out"
    out.mkdir()
    _Blobs.asked = []
    save_as(monkeypatch, world, private, out / "Beach.png")
    assert wait_for(lambda: not saving(world), 5)
    assert world.dialog._status.text() == (
        "“Beach.png” wasn’t saved: it’s kept privately, and its key doesn’t open it.")
    # Both addresses reach this one test server; the Host tells them apart.
    assert {host for host, _path in _Blobs.asked} == {server.split("//", 1)[1]}
    assert {path for _host, path in _Blobs.asked} == {f"/{sha}"}
    assert folder(out) == []


def test_a_private_file_on_this_computer_its_key_does_not_open_says_so(
        tmp_path, monkeypatch, server):
    envelope = encrypt_file(b"the real picture", key_hex=KEY).envelope
    sha = hashlib.sha256(envelope).hexdigest()
    private = MediaFile(hash=sha, url=f"{server}/{sha}", size=len(envelope))
    blob_record = PrivateBlob(sha256=sha, key_hex="6d" * 32, mime="image/png", name="Beach.png")
    visibility = MediaVisibility(library=FakeLibrary([blob_record]),
                                 ledger=PublicLedger(path=tmp_path / "public.json"))
    world = library(tmp_path, monkeypatch, [private], visibility=visibility)
    world.dialog._loader.cache_path(sha).write_bytes(envelope)
    save_as(monkeypatch, world, private, tmp_path / "Beach.png")
    assert world.dialog._status.text() == (
        "“Beach.png” wasn’t saved: it’s kept privately, and its key doesn’t open it.")


def test_a_picker_saving_shows_one_cancel_and_a_stop_for_the_save(tmp_path, monkeypatch, server):
    """The picker's Cancel closes it; the save's own button stops the save,
    and says so to VoiceOver."""
    from PySide6.QtWidgets import QPushButton
    _Blobs.delay = 0.02
    big = blob(server, os.urandom(1024 * 1024))
    world = library(tmp_path, monkeypatch, [big], names={big.hash: "Big.mp4"}, pick=True)
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, big, out / "Big.mp4")
    assert wait_for(lambda: world.dialog._save_bar.value() > 0, 10)
    shown = [b.text() for b in world.dialog.findChildren(QPushButton) if b.isVisible()]
    assert shown.count("Cancel") == 1 and "Stop" in shown
    assert world.dialog._save_stop.accessibleName() == "Stop Saving"
    world.dialog._save_stop.click()
    assert wait_for(lambda: not saving(world), 5)
    assert folder(out) == []


def test_the_saver_names_every_save_quitting_would_stop(tmp_path, monkeypatch, server):
    _Blobs.delay = 0.02
    one, two = blob(server, os.urandom(512 * 1024)), blob(server, os.urandom(512 * 1024), day=1)
    keeper, saver = _keeper(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    assert saver.pending() == []
    saver.save(one, str(out / "One.mp4"))
    saver.save(two, str(out / "Two.mp4"))
    assert saver.pending() == [str(out / "One.mp4"), str(out / "Two.mp4")]
    saver.cancel()
    assert saver.pending() == []


# --------------------------------------------------------------------------- #
# The part file
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                    reason="links need privileges on Windows")
def test_the_part_file_never_follows_a_link_already_there(tmp_path, monkeypatch, server):
    """It is a new file of its own: a link someone left in a shared folder
    under the name it once had leads nowhere."""
    body = b"downloaded" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media], names={media.hash: "Pic.mp4"})
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"mine")
    os.symlink(victim, out / ".Pic.mp4.part")
    save_as(monkeypatch, world, media, out / "Pic.mp4")
    assert wait_for(lambda: not saving(world), 5)
    assert victim.read_bytes() == b"mine"
    assert os.path.islink(out / ".Pic.mp4.part")
    assert (out / "Pic.mp4").read_bytes() == body


def test_the_part_file_leaves_a_file_of_its_old_name_alone(tmp_path, monkeypatch, server):
    body = b"downloaded" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media], names={media.hash: "Pic.mp4"})
    out = tmp_path / "out"
    out.mkdir()
    (out / ".Pic.mp4.part").write_bytes(b"someone else's")
    save_as(monkeypatch, world, media, out / "Pic.mp4")
    assert wait_for(lambda: not saving(world), 5)
    assert (out / ".Pic.mp4.part").read_bytes() == b"someone else's"
    assert folder(out) == [".Pic.mp4.part", "Pic.mp4"]


# --------------------------------------------------------------------------- #
# A long name typed in Save As
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cached", [False, True])
def test_the_longest_name_a_folder_holds_is_saved(tmp_path, monkeypatch, server, cached):
    """The system's save dialog takes a name of up to 255 bytes; nothing
    the save adds to it for its own temporary file may push it over."""
    body = b"long" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media])
    if cached:
        world.dialog._loader.cache_path(media.hash).write_bytes(body)
    out = tmp_path / "out"
    out.mkdir()
    name = "n" * 251 + ".mp4"
    save_as(monkeypatch, world, media, out / name)
    assert wait_for(lambda: (out / name).exists() and not saving(world), 5)
    assert folder(out) == [name]


@pytest.mark.parametrize("cached", [False, True])
def test_a_name_too_long_for_the_folder_says_so(tmp_path, monkeypatch, server, cached):
    body = b"long" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media])
    if cached:
        world.dialog._loader.cache_path(media.hash).write_bytes(body)
    out = tmp_path / "out"
    out.mkdir()
    name = "n" * 300 + ".mp4"
    save_as(monkeypatch, world, media, out / name)
    assert wait_for(lambda: not saving(world), 5)
    assert world.dialog._status.text() == (
        f"“{name}” couldn’t be saved: its name is too long. Choose a shorter one.")
    assert folder(out) == []


def test_a_name_too_long_is_told_whatever_error_the_system_gives():
    """Windows has no ENAMETOOLONG for a name past what a folder holds: it
    says the path wasn't found or the name is invalid. The name says why."""
    part = os.path.join("out", ".nnnn.part")
    target = os.path.join("out", "n" * 300 + ".mp4")
    for code in (errno.ENOENT, errno.EINVAL):
        assert thumbnail_loader.write_problem(
            OSError(code, "error", part, None, target)) == "name"
    assert thumbnail_loader.write_problem(
        OSError(errno.EACCES, "denied", os.path.join("out", "a.mp4"))) == "write"


# --------------------------------------------------------------------------- #
# What the saved file is
# --------------------------------------------------------------------------- #

def _new_file_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                    reason="links need privileges on Windows")
@pytest.mark.parametrize("cached", [False, True])
def test_save_as_onto_a_link_replaces_the_file_it_points_to(tmp_path, monkeypatch, server,
                                                             cached):
    """The system's save dialog asked to replace the file the link names:
    that file gets the new content and keeps its permissions, and the link
    stays, from a download or from this computer's copy alike."""
    body = b"new content" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media])
    if cached:
        world.dialog._loader.cache_path(media.hash).write_bytes(body)
    real = tmp_path / "elsewhere" / "Clip.mp4"
    real.parent.mkdir()
    real.write_bytes(b"old")
    os.chmod(real, 0o640)
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(real, out / "Clip.mp4")
    save_as(monkeypatch, world, media, out / "Clip.mp4")
    assert wait_for(lambda: real.read_bytes() == body and not saving(world), 5)
    assert os.path.islink(out / "Clip.mp4")
    assert oct(os.stat(real).st_mode & 0o777) == oct(0o640)
    assert folder(out) == ["Clip.mp4"] and folder(real.parent) == ["Clip.mp4"]


@pytest.mark.parametrize("cached", [False, True])
def test_a_saved_private_file_has_a_new_files_permissions(tmp_path, monkeypatch, server,
                                                         cached):
    """As any file saved here: readable as the system's settings make a
    new file, whether downloaded or from this computer's copy."""
    envelope = encrypt_file(b"the real picture" * 100, key_hex=KEY).envelope
    sha = hashlib.sha256(envelope).hexdigest()
    _Blobs.files[sha] = envelope
    private = MediaFile(hash=sha, url=f"{server}/{sha}",
                        urls=[{"server": server, "url": f"{server}/{sha}"}],
                        mime_type="application/octet-stream", size=len(envelope))
    blob_record = PrivateBlob(sha256=sha, key_hex=KEY, mime="image/png", name="Beach.png")
    visibility = MediaVisibility(library=FakeLibrary([blob_record]),
                                 ledger=PublicLedger(path=tmp_path / "public.json"))
    world = library(tmp_path, monkeypatch, [private], visibility=visibility)
    if cached:
        world.dialog._loader.cache_path(sha).write_bytes(envelope)
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, private, out / "Beach.png")
    assert wait_for(lambda: (out / "Beach.png").exists() and not saving(world), 5)
    assert (out / "Beach.png").read_bytes() == b"the real picture" * 100
    if sys.platform != "win32":
        assert oct(os.stat(out / "Beach.png").st_mode & 0o777) == oct(_new_file_mode())


# --------------------------------------------------------------------------- #
# One file under two spellings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("first_name, second_name", [
    ("A.mp4", "a.mp4"),                                       # one name where case is ignored
    (unicodedata.normalize("NFC", "Café.mp4"), unicodedata.normalize("NFD", "Café.mp4")),
])
def test_two_save_as_to_one_file_by_two_spellings_keep_both(tmp_path, monkeypatch, server,
                                                             first_name, second_name):
    """Neither file was there when it was asked for, so the person was
    asked to replace nothing: where the folder takes the two spellings
    for one name, the second is numbered, not put over the first."""
    _Blobs.delay = 0.02
    one, two = blob(server, os.urandom(512 * 1024)), blob(server, os.urandom(512 * 1024), day=1)
    world = library(tmp_path, monkeypatch, [one, two])
    out = tmp_path / "out"
    out.mkdir()
    save_as(monkeypatch, world, one, out / first_name)
    save_as(monkeypatch, world, two, out / second_name)
    assert wait_for(lambda: not saving(world) and len(folder(out)) == 2, 15)
    kept = {(out / name).read_bytes() for name in folder(out)}
    assert kept == {_Blobs.files[one.hash], _Blobs.files[two.hash]}


def test_save_as_onto_a_file_that_was_there_replaces_it(tmp_path, monkeypatch, server):
    """The system's save dialog asked about that one: it is replaced."""
    body = b"new" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media])
    out = tmp_path / "out"
    out.mkdir()
    (out / "Clip.mp4").write_bytes(b"old")
    save_as(monkeypatch, world, media, out / "Clip.mp4")
    assert wait_for(lambda: (out / "Clip.mp4").read_bytes() == body and not saving(world), 5)
    assert folder(out) == ["Clip.mp4"]


# --------------------------------------------------------------------------- #
# Save to Folder: what wasn't saved, at the end
# --------------------------------------------------------------------------- #

def _three_with_one_gone(server):
    one, three = blob(server, b"one" * 100), blob(server, b"three" * 100, day=2)
    gone = blob(server, b"two" * 100, day=1)
    del _Blobs.files[gone.hash]                        # no longer on any server
    names = {one.hash: "One.mp4", gone.hash: "Two.mp4", three.hash: "Three.mp4"}
    return [one, gone, three], names


SUMMARY = ("Saved 2 of 3 files. “Two.mp4” isn’t on your media servers anymore, so it "
           "couldn’t be saved.")


def test_save_to_folder_says_at_the_end_what_wasnt_saved(tmp_path, monkeypatch, server):
    files, names = _three_with_one_gone(server)
    world = library(tmp_path, monkeypatch, files, names=names)
    out = tmp_path / "out"
    out.mkdir()
    save_to_folder(monkeypatch, world, out)
    assert wait_for(lambda: len(folder(out)) == 2 and not saving(world), 5)
    assert world.dialog._status.text() == SUMMARY
    assert world.dialog._status.toolTip() == (
        "“Two.mp4” isn’t on your media servers anymore, so it couldn’t be saved.")


def tooltip_at(widget, x) -> str:
    """The tooltip the pointer at ``x`` in ``widget`` shows, "" for none."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip
    QToolTip.hideText()
    wait_for(lambda: not QToolTip.isVisible(), 2)      # it fades out
    point = QPoint(x, widget.height() // 2)
    QApplication.sendEvent(widget, QHelpEvent(QEvent.Type.ToolTip, point,
                                              widget.mapToGlobal(point)))
    return QToolTip.text() if QToolTip.isVisible() else ""


def test_save_to_folder_says_what_wasnt_saved_after_its_window_closed(
        tmp_path, monkeypatch, server):
    """In the status bar, with each reason in the message's tooltip: over
    the message alone, not the word count beside it, and gone with it."""
    from PySide6.QtWidgets import QLabel
    from nostr.ui.context_widgets import ContextStatusBar
    import main_window
    files, names = _three_with_one_gone(server)
    keeper, saver = _keeper(tmp_path)
    bar = ContextStatusBar()
    words = QLabel("1,234 words")
    bar.addPermanentWidget(words)
    bar.resize(700, 24)
    bar.show()

    class Host:                                    # the main window's part of it
        status, _media_save_said = bar, ""
        _message_tip = main_window._MessageTip(bar)

        @staticmethod
        def _media_window_open():
            return False

    host = Host()
    for name in ("_on_media_save_started", "_on_media_save_finished", "_on_media_saves_finished",
                 "_media_save_announced"):
        setattr(host, name, getattr(main_window.MainWindow, name).__get__(host))
    saver.started.connect(host._on_media_save_started)
    saver.finished.connect(host._on_media_save_finished)
    saver.batch_finished.connect(host._on_media_saves_finished)
    out = tmp_path / "out"
    out.mkdir()
    saver.save_all([(media, str(out / names[media.hash]), None) for media in files])
    assert wait_for(lambda: len(folder(out)) == 2 and bar.currentMessage() == SUMMARY, 5)
    reason = "“Two.mp4” isn’t on your media servers anymore, so it couldn’t be saved."
    assert tooltip_at(bar, 10) == reason
    assert tooltip_at(words, 5) == ""
    bar.showMessage("Saved: /Users/me/Essay.md")
    assert tooltip_at(bar, 10) == ""


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                    reason="links need privileges on Windows")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root may write into any folder")
@pytest.mark.parametrize("cached", [False, True])
def test_save_as_onto_a_link_into_a_folder_that_takes_no_new_file(tmp_path, monkeypatch, server,
                                                                  cached):
    """The linked file can be written, its folder can't take a new one:
    it is written in place, from a download as from this computer's copy."""
    body = b"new content" * 100
    media = blob(server, body)
    world = library(tmp_path, monkeypatch, [media])
    if cached:
        world.dialog._loader.cache_path(media.hash).write_bytes(body)
    real = tmp_path / "elsewhere" / "Clip.mp4"
    real.parent.mkdir()
    real.write_bytes(b"old")
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(real, out / "Clip.mp4")
    os.chmod(real.parent, 0o555)
    try:
        save_as(monkeypatch, world, media, out / "Clip.mp4")
        assert wait_for(lambda: not saving(world), 5)
        assert real.read_bytes() == body
        assert os.path.islink(out / "Clip.mp4") and folder(out) == ["Clip.mp4"]
    finally:
        os.chmod(real.parent, 0o755)


def test_a_save_to_folder_that_saved_one_file_says_so_in_the_singular(tmp_path, monkeypatch,
                                                                      server):
    one = blob(server, b"one" * 100)
    gone = [blob(server, f"gone {n}".encode() * 100, day=n) for n in (1, 2)]
    for media in gone:
        del _Blobs.files[media.hash]
    names = {one.hash: "One.mp4", gone[0].hash: "Two.mp4", gone[1].hash: "Three.mp4"}
    world = library(tmp_path, monkeypatch, [one, *gone], names=names)
    out = tmp_path / "out"
    out.mkdir()
    save_to_folder(monkeypatch, world, out)
    assert wait_for(lambda: len(folder(out)) == 1 and not saving(world), 5)
    assert world.dialog._status.text().startswith("Saved 1 of 3 files. ")
