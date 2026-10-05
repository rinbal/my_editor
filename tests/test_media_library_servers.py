# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the Media Library's per-server parts: notes, meters, names, deletes.

What must hold:

  A server that could not be listed is named, and the library says it
  shows that server's files from last time only when it has some.

  The storage meter is built once for the servers that have an
  allowance and then only updated; a row that goes is hidden first.

  Store messages name a server the way the library does (the members'
  server is "EINUNDZWANZIG", not its host).

  The delete warning asks the state to act on, not the state drawn: a
  file nobody has checked yet still counts as possibly private while the
  private library loads. It is worded for one file or several, and
  assumes no particular other app.

  A closed library leaves nothing connected to the store: the store's
  next fetch runs no code against the deleted dialog.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import nostr.ui.media_library_dialog as dialog_module  # noqa: E402
from nostr.blossom.store import MediaFile, ServerListing  # noqa: E402
from nostr.media.media_visibility import MediaVisibility  # noqa: E402
from nostr.ui.media_library_dialog import (  # noqa: E402
    MediaLibraryDialog, delete_warning, unreachable_text,
)
from tests.test_media_library_visibility import FakeLibrary, FakeLoader, FakeStore  # noqa: E402

E21 = "https://blossom.einundzwanzig.space"
OWN = "https://own.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def flush_deletes():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def blob(sha, *servers, size=10):
    urls = [{"server": s, "url": f"{s}/{sha}"} for s in servers]
    return MediaFile(hash=sha, url=urls[0]["url"], urls=urls, mime_type="image/png",
                     size=size, uploaded_at_ms=1)


class ServerStore(FakeStore):
    """A store with servers, listings and an allowance on one of them."""

    def __init__(self, records=(), *, servers=(OWN, E21), listings=None, quota=None):
        super().__init__(records)
        self.servers = list(servers)
        self.listings = dict(listings or {})
        self.quota = dict(quota or {})

    def target_servers(self):
        return list(self.servers)

    def server_listings(self):
        return dict(self.listings)

    def quota_for(self, origin):
        return self.quota.get(origin)

    def bytes_on(self, origin):
        return sum(m.size for m in self.files.values()
                   if any(u["server"] == origin for u in m.urls))


def build(tmp_path, monkeypatch, store, *, visibility=None):
    monkeypatch.setattr(
        dialog_module, "ThumbnailLoader",
        lambda parent=None: FakeLoader(tmp_path / "cache", parent=parent))
    label = (lambda origin: "EINUNDZWANZIG" if origin == E21 else None)
    return MediaLibraryDialog(store=store, is_dark=True, visibility=visibility,
                              server_label=label)


# -- servers that could not be listed ------------------------------------------------

def test_an_unreachable_server_with_nothing_shown_is_only_named():
    assert unreachable_text(["own.example"], []) == "Couldn’t reach own.example."


def test_an_unreachable_server_with_files_shown_says_so():
    assert unreachable_text(["own.example"], ["own.example"]) == (
        "Couldn’t reach own.example. Showing the files it had last time.")
    assert unreachable_text(["a", "b"], ["a", "b"]).endswith("they had last time.")
    assert unreachable_text(["a", "b"], ["b"]).endswith("Showing the files b had last time.")
    assert unreachable_text([], []) == ""


def test_the_library_note_names_the_server_and_claims_only_what_it_shows(
        tmp_path, monkeypatch):
    down = {E21: ServerListing(origin=E21, ok=False), OWN: ServerListing(origin=OWN)}
    store = ServerStore([blob("a" * 64, OWN)], listings=down)
    dialog = build(tmp_path, monkeypatch, store)
    assert dialog._servers_label.text() == "Couldn’t reach EINUNDZWANZIG."
    store.files["b" * 64] = blob("b" * 64, E21)
    store.listings_changed.emit()
    assert dialog._servers_label.text().endswith("Showing the files it had last time.")


# -- the storage meter -----------------------------------------------------------------

def test_the_meter_is_built_once_and_then_only_updated(tmp_path, monkeypatch):
    ok = {E21: ServerListing(origin=E21, ok=True)}
    store = ServerStore([blob("a" * 64, E21, size=1024 ** 3)], listings=ok,
                        quota={E21: 5 * 1024 ** 3})
    dialog = build(tmp_path, monkeypatch, store)
    row = dialog._storage_widgets[E21]
    assert row.value.text() == "1 GB of 5 GB used"
    # One fetch sends both of these; neither rebuilds the row.
    store.files["b" * 64] = blob("b" * 64, E21, size=1024 ** 3)
    store.library_changed.emit()
    store.listings_changed.emit()
    assert dialog._storage_widgets[E21] is row
    assert row.value.text() == "2 GB of 5 GB used"
    assert row.bar.accessibleDescription() == "2 GB of 5 GB used"


def test_a_meter_that_goes_is_hidden_before_it_is_deleted(tmp_path, monkeypatch):
    store = ServerStore(listings={E21: ServerListing(origin=E21)},
                        quota={E21: 5 * 1024 ** 3})
    dialog = build(tmp_path, monkeypatch, store)
    dialog.show()
    old = dialog._storage_widgets[E21].widget
    store.quota = {}                               # the membership lapsed
    store.listings_changed.emit()
    assert old.isHidden()
    assert not dialog._storage_box.isVisibleTo(dialog)
    flush_deletes()
    assert not shiboken6.isValid(old)
    dialog.close()


# -- names in store messages -----------------------------------------------------------

def test_a_skipped_server_is_named_the_way_the_library_names_it(tmp_path, monkeypatch):
    store = ServerStore()
    dialog = build(tmp_path, monkeypatch, store)
    store.server_skipped.emit("photo.png", "blossom.einundzwanzig.space", "full")
    assert "not stored on EINUNDZWANZIG" in dialog._status_label.text()
    store.mirror_failed.emit("photo.png", "own.example", "server")
    assert "own.example didn’t take a copy" in dialog._status_label.text()


# -- deleting -------------------------------------------------------------------------

def test_delete_warning_is_worded_for_one_file_or_several():
    assert delete_warning(3, 0) == "This can't be undone."
    one = delete_warning(1, 1)
    assert one.startswith("This may be a private file from another app.")
    assert "removes it there too" in one
    assert delete_warning(2, 2).startswith("These may be private files")
    assert delete_warning(3, 1).startswith("Some of these may be private files")
    for text in (one, delete_warning(2, 2), delete_warning(3, 1)):
        assert "Lotus" not in text


def test_a_file_not_yet_checked_is_warned_about_while_the_library_loads(
        tmp_path, monkeypatch):
    # Drawn as "checking" while the private library loads, but acted on
    # as what it is: not known to be public.
    sha = "c" * 64
    store = ServerStore([blob(sha, OWN)])
    visibility = MediaVisibility(library=FakeLibrary(vouches=False))
    dialog = build(tmp_path, monkeypatch, store, visibility=visibility)
    dialog._private_library = SimpleNamespace(loading=True, failures=[], status="")
    assert dialog._shown_state(sha) != "unknown"   # softened on screen
    asked = []
    monkeypatch.setattr(dialog_module, "confirm_destructive",
                        lambda parent, **kw: asked.append(kw) or False)
    dialog._grid.selectAll()
    dialog._on_delete_clicked()
    assert asked and asked[0]["message"].startswith("This may be a private file")
    assert asked[0]["title"] == "Delete 1 file from your Blossom servers?"


def test_a_checked_public_file_gets_the_plain_warning(tmp_path, monkeypatch):
    sha = "d" * 64
    store = ServerStore([blob(sha, OWN)])
    visibility = MediaVisibility(library=FakeLibrary(vouches=True))
    dialog = build(tmp_path, monkeypatch, store, visibility=visibility)
    asked = []
    monkeypatch.setattr(dialog_module, "confirm_destructive",
                        lambda parent, **kw: asked.append(kw) or False)
    dialog._grid.selectAll()
    dialog._on_delete_clicked()
    assert asked[0]["message"] == "This can't be undone."


# -- a closed library -------------------------------------------------------------------

def test_a_closed_library_leaves_nothing_connected_to_the_store(tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc[1]))
    store = ServerStore()
    dialog = build(tmp_path, monkeypatch, store)
    dialog.show()
    dialog.close()                                 # deletes itself
    flush_deletes()
    assert not shiboken6.isValid(dialog)
    store.fetch_started.emit()
    store.fetch_finished.emit()
    store.fetch_error.emit("Could not reach any Blossom server.")
    store.listings_changed.emit()
    store.library_changed.emit()
    assert errors == []
