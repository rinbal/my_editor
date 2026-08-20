# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins that the library says which files are private, and shows them.

A blob stored encrypted and a blob anyone can open are the same thing
from the server's side: bytes at a hash. If the grid does not say which
is which, the first time a user learns they picked a private picture is
the confirmation at the end of a publish, which is far too late to be a
choice they made.

Two failure modes are guarded here. The first is silence: no word on the
tile, no sentence in the tooltip, no mark on the thumbnail. The second
is the wrong words: an encrypted file whose preview is refused is not
"downloaded, but the bytes are not a readable image", because the
picture is fine and merely sealed, and saying otherwise sends the user
looking for a corruption that is not there.

Then the thing that must never happen: a decrypted thumbnail reaching
the shared blob cache, which is the directory an export walks.

The store and the thumbnail loader are fakes. Nothing here touches a
network, a signer, a relay or a real home directory, and no dialog is
executed: every state is reached by calling the method that reaches it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import nostr.ui.media_library_dialog as dialog_module
from nostr.blossom.store import MediaFile
from nostr.media.filecrypto import encrypt_file
from nostr.media.media_visibility import (
    PRIVATE,
    PUBLIC,
    PUBLISHED_COPY,
    UNKNOWN,
    MediaVisibility,
)
from nostr.media.visibility import PrivateBlob, PublicBlob, PublicLedger
from nostr.ui.media_library_dialog import (
    _LIBRARY_UNREAD,
    _PICK_PRIVATE_NOTICE,
    _PICK_PUBLISHED_NOTICE,
    _PICK_UNKNOWN_NOTICE,
    MediaLibraryDialog,
    _chipped_icon,
    _no_preview_reason,
    _placeholder_icon,
    _short_label,
    _tooltip_for,
)

from tests.media_fakes import PNG_BYTES, TEXT_BYTES, sha_of


KEY = "5c" * 32
OTHER_KEY = "6d" * 32
COPY_SHA = "b" * 64
COPY_URL = "https://cdn.example/" + "b" * 64


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def sealed(data: bytes = PNG_BYTES, key: str = KEY) -> bytes:
    return encrypt_file(data, key_hex=key).envelope


CIPHER = sealed()
CIPHER_SHA = sha_of(CIPHER)


def media(sha=CIPHER_SHA, *, mime="application/octet-stream", size=None):
    """A library record as the server describes an encrypted blob.

    The mime is the envelope's, not the picture's, which is exactly why
    the grid cannot decide anything from it.
    """
    return MediaFile(
        hash=sha,
        url=f"https://one.example/{sha}",
        mime_type=mime,
        size=len(CIPHER) if size is None else size,
    )


def private(sha=CIPHER_SHA, *, key=KEY, **kw) -> PrivateBlob:
    return PrivateBlob(
        sha256=sha,
        key_hex=key,
        servers=["https://one.example"],
        size=len(CIPHER),
        mime=kw.pop("mime", "image/png"),
        name=kw.pop("name", "holiday.png"),
        **kw,
    )


class FakeLibrary:
    """A library that has finished reading, unless a test says otherwise."""

    def __init__(self, blobs=(), *, vouches=True) -> None:
        self.blobs = {b.sha256: b for b in blobs}
        self.vouches = vouches

    def get(self, sha256):
        return self.blobs.get((sha256 or "").lower())

    def vouches_for(self, sha256):
        return self.vouches


class FakeStore(QObject):
    """MediaStore's observed surface, with nothing behind it."""

    library_changed = Signal()
    fetch_started = Signal()
    fetch_finished = Signal()
    fetch_error = Signal(str)
    upload_started = Signal(str)
    upload_progress = Signal(str, int, int)
    upload_status = Signal(str, str)
    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)
    upload_rerouted = Signal(str, str, str)
    file_deleted = Signal(str)
    delete_failed = Signal(str, str)

    def __init__(self, records=()) -> None:
        super().__init__()
        self.files = {m.hash: m for m in records}
        self.fetched = 0

    def file_list(self, *, filter_type="all", sort_by="newest"):
        return list(self.files.values())

    def fetch(self, force: bool = False) -> None:
        self.fetched += 1

    def upload_bytes(self, body, *, name, mime_type) -> None:
        pass

    def upload_file(self, path) -> None:
        pass

    def delete_file(self, sha) -> None:
        pass


class FakeLoader(QObject):
    """ThumbnailLoader's surface over a directory the test owns."""

    ready = Signal(str, str, object)
    failed = Signal(str, str)
    url_ready = Signal(str, str, object)
    url_failed = Signal(str, str)

    def __init__(self, root, parent=None) -> None:
        super().__init__(parent)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.loads: list = []

    def cache_path(self, sha256: str) -> Path:
        return self.root / sha256.lower()

    def load(self, sha256: str, url: str) -> None:
        self.loads.append((sha256, url))


def build_dialog(tmp_path, monkeypatch, *, records=(), visibility=None,
                 pick_mode=False, cache=None):
    """A real dialog over fake seams, with no real cache directory.

    The loader is swapped at the module name the dialog constructs it
    from, which is the only way in: it builds its own, and the real one
    writes into ~/.config.
    """
    cache_root = cache if cache is not None else tmp_path / "cache"
    monkeypatch.setattr(
        dialog_module, "ThumbnailLoader",
        lambda parent=None: FakeLoader(cache_root, parent=parent),
    )
    store = FakeStore(records)
    return MediaLibraryDialog(
        store=store, is_dark=True, pick_mode=pick_mode, visibility=visibility,
    )


def visibility_with_copy(tmp_path, blob):
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(
        sha256=COPY_SHA, url=COPY_URL, source_hash=blob.sha256, size=900,
    ))
    return MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)


# --------------------------------------------------------------------- #
# The word on the tile                                                   #
# --------------------------------------------------------------------- #

def test_a_public_file_claims_nothing():
    # Everything in an ordinary library is public. Badging all of it
    # would be noise, and noise is what teaches people to stop reading.
    label = _short_label(media(), PUBLIC)
    assert len(label.splitlines()) == 2
    assert "Private" not in label


def test_a_private_file_says_private_on_the_tile():
    assert "Private" in _short_label(media(), PRIVATE)


def test_a_copied_file_says_both_facts():
    label = _short_label(media(), PUBLISHED_COPY)
    assert "Private" in label
    assert "copy published" in label


def test_the_cell_is_tall_enough_for_the_state_line(tmp_path, monkeypatch):
    # The grid sizes every cell alike, so the extra line a private file
    # carries has to have been paid for up front. Without this the word
    # is clipped away on exactly the tiles that need it.
    dialog = build_dialog(tmp_path, monkeypatch, records=[media()])
    longest = max(
        len(_short_label(media(), s).splitlines())
        for s in (PUBLIC, PRIVATE, PUBLISHED_COPY)
    )
    assert longest == dialog_module._LABEL_LINES
    height = dialog._grid.gridSize().height()
    assert height >= dialog_module._THUMB_SIZE + longest * 16


def test_the_tile_still_carries_size_and_hash():
    label = _short_label(media(), PRIVATE)
    assert CIPHER_SHA[:8] in label
    assert "B" in label


# --------------------------------------------------------------------- #
# The sentence in the tooltip                                            #
# --------------------------------------------------------------------- #

def test_the_tooltip_explains_what_private_means():
    tip = _tooltip_for(media(), state=PRIVATE)
    assert "encrypted" in tip
    assert "only by you" in tip


def test_the_tooltip_names_the_public_copy_address():
    # The address is the thing a user needs in order to revoke it, so it
    # is worth the line.
    copy = PublicBlob(sha256=COPY_SHA, url=COPY_URL, source_hash=CIPHER_SHA)
    tip = _tooltip_for(media(), state=PUBLISHED_COPY, public_copy=copy)
    assert COPY_URL in tip
    assert "a public copy of it exists" in tip


def test_a_public_file_gets_no_access_line():
    assert "access:" not in _tooltip_for(media(mime="image/png"))


# --------------------------------------------------------------------- #
# The vocabulary of a missing preview                                    #
# --------------------------------------------------------------------- #

def test_a_sealed_blob_is_not_called_unreadable():
    # The decoder refusing an envelope is the expected step, not a
    # fault, and "the bytes are not a readable image" would describe the
    # ciphertext while sounding like a claim about the picture.
    assert _no_preview_reason(media(), "not an image", PRIVATE) == ""


def test_a_key_failure_reaches_the_tooltip():
    reason = _no_preview_reason(
        media(), "the key in your library does not open this file", PRIVATE,
    )
    assert "key" in reason
    tip = _tooltip_for(media(), preview_error=reason, state=PRIVATE)
    assert "preview:" in tip


def test_a_public_blob_keeps_the_old_vocabulary():
    # The behaviour this dialog already had must not shift underneath.
    assert _no_preview_reason(media(mime="image/png"), "not an image") == (
        "downloaded, but the bytes are not a readable image"
    )
    assert "not an image" in _no_preview_reason(media(mime="video/mp4"))


# --------------------------------------------------------------------- #
# The mark on the thumbnail                                              #
# --------------------------------------------------------------------- #

def test_a_private_thumbnail_is_marked():
    base = _placeholder_icon("image/png", PUBLIC, True)
    marked = _placeholder_icon("image/png", PRIVATE, True)
    plain = base.pixmap(128, 128).toImage()
    chipped = marked.pixmap(128, 128).toImage()
    assert plain != chipped


def test_the_two_private_states_are_told_apart_by_more_than_words():
    private_icon = _placeholder_icon("image/png", PRIVATE, True).pixmap(128, 128)
    copied_icon = _placeholder_icon("image/png", PUBLISHED_COPY, True).pixmap(128, 128)
    assert private_icon.toImage() != copied_icon.toImage()


def test_a_public_thumbnail_is_left_alone():
    from PySide6.QtGui import QPixmap

    pix = QPixmap(64, 64)
    pix.fill()
    assert _chipped_icon(pix, PUBLIC, True).pixmap(64, 64).toImage() == pix.toImage()


def test_the_mark_survives_a_theme():
    dark = _placeholder_icon("image/png", PRIVATE, True).pixmap(128, 128).toImage()
    light = _placeholder_icon("image/png", PRIVATE, False).pixmap(128, 128).toImage()
    # Different ink and fill per theme, so the two must not be identical
    # and neither may be the unmarked square.
    plain = _placeholder_icon("image/png", PUBLIC, True).pixmap(128, 128).toImage()
    assert dark != light
    assert dark != plain and light != plain


def test_a_private_file_gets_a_neutral_placeholder_not_a_type_colour():
    # The server's type for an envelope is not the picture's type, so
    # colouring the square by it would state something false.
    sealed_square = _placeholder_icon("application/octet-stream", PRIVATE, True)
    image_square = _placeholder_icon("image/png", PRIVATE, True)
    assert sealed_square.pixmap(128, 128).toImage() == image_square.pixmap(128, 128).toImage()


# --------------------------------------------------------------------- #
# The grid asks for the bytes, decrypts them, and writes nothing         #
# --------------------------------------------------------------------- #

def test_an_encrypted_blob_is_fetched_despite_its_mime(tmp_path, monkeypatch):
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    # An octet-stream would never be requested by the old rule, so a
    # private file would have shown a grey square forever.
    assert dialog._loader.loads == [(CIPHER_SHA, f"https://one.example/{CIPHER_SHA}")]


def test_the_decoder_refusing_an_envelope_produces_a_real_thumbnail(
    tmp_path, monkeypatch,
):
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    dialog._loader.cache_path(CIPHER_SHA).write_bytes(CIPHER)

    dialog._loader.failed.emit(CIPHER_SHA, "not an image")

    item = dialog._items_by_hash[CIPHER_SHA]
    assert CIPHER_SHA not in dialog._preview_errors
    assert not item.icon().isNull()
    # The decrypted picture is 3x2, the placeholder is a filled square,
    # so a real decode is the only way these differ.
    assert item.icon().pixmap(128, 128).toImage() != (
        _placeholder_icon("application/octet-stream", PRIVATE, True)
        .pixmap(128, 128).toImage()
    )


def test_no_plaintext_is_written_to_the_blob_cache(tmp_path, monkeypatch):
    # The whole point. The cache is content-addressed and shared with
    # the document asset layer, and an export walks it.
    cache = tmp_path / "cache"
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
        cache=cache,
    )
    dialog._loader.cache_path(CIPHER_SHA).write_bytes(CIPHER)

    dialog._loader.failed.emit(CIPHER_SHA, "not an image")

    written = sorted(p.name for p in cache.iterdir())
    assert written == [CIPHER_SHA]
    assert cache.joinpath(CIPHER_SHA).read_bytes() == CIPHER
    # The plaintext's own hash must not appear as a cache entry either.
    assert not cache.joinpath(sha_of(PNG_BYTES)).exists()


def test_a_key_that_does_not_open_the_file_is_explained(tmp_path, monkeypatch):
    blob = private(key=OTHER_KEY)
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    dialog._loader.cache_path(CIPHER_SHA).write_bytes(CIPHER)

    dialog._loader.failed.emit(CIPHER_SHA, "not an image")

    reason = dialog._preview_errors[CIPHER_SHA]
    assert "key" in reason
    assert OTHER_KEY not in reason
    assert "preview:" in dialog._items_by_hash[CIPHER_SHA].toolTip()


def test_a_sealed_file_that_is_not_a_picture_is_explained(tmp_path, monkeypatch):
    envelope = sealed(TEXT_BYTES)
    sha = sha_of(envelope)
    blob = private(sha)
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media(sha)],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    dialog._loader.cache_path(sha).write_bytes(envelope)

    dialog._loader.failed.emit(sha, "not an image")

    assert "not an image" in dialog._preview_errors[sha]


def test_bytes_that_never_arrived_are_reported_as_themselves(tmp_path, monkeypatch):
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    # No cache file: the download failed, which is a different problem
    # from a file that will not open.
    dialog._loader.failed.emit(CIPHER_SHA, "network error")
    assert dialog._preview_errors[CIPHER_SHA] == "network error"


def test_a_public_blob_is_not_run_through_the_decrypt(tmp_path, monkeypatch):
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media(mime="image/png")],
        visibility=MediaVisibility(),
    )
    dialog._loader.failed.emit(CIPHER_SHA, "not an image")
    assert dialog._preview_errors[CIPHER_SHA] == "not an image"


# --------------------------------------------------------------------- #
# The picker warns before the end                                        #
# --------------------------------------------------------------------- #

def test_the_picker_warns_when_the_selection_is_private(tmp_path, monkeypatch):
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
        pick_mode=True,
    )
    dialog._grid.item(0).setSelected(True)
    assert dialog._notice_label.isVisibleTo(dialog)
    assert dialog._notice_label.text() == _PICK_PRIVATE_NOTICE
    assert "public copy" in dialog._notice_label.text()


def test_the_warning_says_the_original_stays_private(tmp_path, monkeypatch):
    assert "original stays private" in _PICK_PRIVATE_NOTICE
    assert "cancel" in _PICK_PRIVATE_NOTICE


def test_a_file_that_already_has_a_copy_is_told_it_costs_nothing(
    tmp_path, monkeypatch,
):
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=visibility_with_copy(tmp_path, blob),
        pick_mode=True,
    )
    dialog._grid.item(0).setSelected(True)
    assert dialog._notice_label.text() == _PICK_PUBLISHED_NOTICE
    assert "nothing new is uploaded" in dialog._notice_label.text()


def test_a_public_selection_says_nothing(tmp_path, monkeypatch):
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media(mime="image/png")],
        visibility=MediaVisibility(),
        pick_mode=True,
    )
    dialog._grid.item(0).setSelected(True)
    assert not dialog._notice_label.isVisibleTo(dialog)
    assert dialog._notice_label.text() == ""


def test_deselecting_takes_the_warning_with_it(tmp_path, monkeypatch):
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
        pick_mode=True,
    )
    dialog._grid.item(0).setSelected(True)
    dialog._grid.clearSelection()
    assert not dialog._notice_label.isVisibleTo(dialog)


def test_the_library_view_does_not_warn(tmp_path, monkeypatch):
    # Nothing is about to be published from here, so the same sentence
    # would be a statement with no decision attached to it.
    blob = private()
    dialog = build_dialog(
        tmp_path, monkeypatch,
        records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
        pick_mode=False,
    )
    dialog._grid.item(0).setSelected(True)
    assert not dialog._notice_label.isVisibleTo(dialog)


def test_the_grid_still_works_with_no_visibility_wired(tmp_path, monkeypatch):
    # An account with no signer has no private library, and the dialog
    # must behave exactly as it did before any of this existed.
    dialog = build_dialog(tmp_path, monkeypatch, records=[media(mime="image/png")])
    item = dialog._items_by_hash[CIPHER_SHA]
    assert len(item.text().splitlines()) == 2
    assert "access:" not in item.toolTip()


# --------------------------------------------------------------------- #
# Deleting a public copy is revoking it                                  #
# --------------------------------------------------------------------- #

def test_deleting_the_public_copy_makes_the_original_private_again(
    tmp_path, monkeypatch,
):
    # Without this the ledger goes on claiming the copy exists, so the
    # badge lies and the next publish reuses an address that is gone.
    blob = private()
    view = visibility_with_copy(tmp_path, blob)
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=view,
    )
    assert view.state_of(CIPHER_SHA) == PUBLISHED_COPY

    dialog._store.file_deleted.emit(COPY_SHA)

    assert view.state_of(CIPHER_SHA) == PRIVATE
    assert view.public_copy_of(CIPHER_SHA) is None
    assert "private again" in dialog._status_label.text()


def test_deleting_an_ordinary_file_says_nothing_about_copies(
    tmp_path, monkeypatch,
):
    blob = private()
    view = visibility_with_copy(tmp_path, blob)
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=view,
    )
    dialog._store.file_deleted.emit("f" * 64)
    assert view.state_of(CIPHER_SHA) == PUBLISHED_COPY
    assert dialog._status_label.text() == ""


def test_a_library_with_no_ledger_survives_a_delete(tmp_path, monkeypatch):
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=MediaVisibility(),
    )
    dialog._store.file_deleted.emit(CIPHER_SHA)
    assert dialog._status_label.text() == ""


# --------------------------------------------------------------------- #
# A file nobody has been able to check                                   #
# --------------------------------------------------------------------- #
#
# The grid used to draw a file the private library had not read yet as an
# ordinary public blob: no word, no chip, no notice. So the state it was
# in was invisible at exactly the moment a user was choosing a picture.

def _unchecked(blobs=()):
    return MediaVisibility(library=FakeLibrary(blobs, vouches=False))


def test_an_unchecked_file_says_so_on_the_tile():
    label = _short_label(media(), UNKNOWN)
    assert "Not checked" in label
    # It does not claim to be private. That is a different statement and
    # this one is the absence of any.
    assert "Private" not in label


def test_an_unchecked_file_explains_itself_in_the_tooltip():
    tip = _tooltip_for(media(), state=UNKNOWN)
    assert "access:" in tip
    assert "not checked" in tip
    assert "cannot say whether this file is private" in tip


def test_an_unchecked_thumbnail_carries_its_own_mark():
    # Not the private chip: borrowing it would state something the app
    # does not know. Not nothing either, which is what it used to be.
    from PySide6.QtGui import QPixmap

    pix = QPixmap(128, 128)
    pix.fill()
    plain = _chipped_icon(QPixmap(pix), PUBLIC, True).pixmap(128, 128).toImage()
    unchecked = _chipped_icon(QPixmap(pix), UNKNOWN, True).pixmap(128, 128).toImage()
    marked = _chipped_icon(QPixmap(pix), PRIVATE, True).pixmap(128, 128).toImage()

    assert unchecked != plain
    assert unchecked != marked


def test_the_grid_marks_an_unchecked_file(tmp_path, monkeypatch):
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    item = dialog._items_by_hash[CIPHER_SHA]
    assert "Not checked" in item.text()
    assert "not checked" in item.toolTip()


def test_the_picker_warns_when_the_selection_is_unchecked(tmp_path, monkeypatch):
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
        pick_mode=True,
    )
    dialog._grid.item(0).setSelected(True)

    assert dialog._notice_label.isVisibleTo(dialog)
    assert dialog._notice_label.text() == _PICK_UNKNOWN_NOTICE
    assert "cannot be used yet" in dialog._notice_label.text()


def test_an_unchecked_blob_is_not_downloaded_on_a_hunch(tmp_path, monkeypatch):
    # An encrypted blob is fetched despite its mime because the record
    # says it is a picture. With no record there is no such reason, and
    # fetching every opaque blob in the library to look for one would
    # cost the user the library's worth of downloads.
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    assert dialog._loader.loads == []


def test_a_private_video_is_not_downloaded_to_be_refused(tmp_path, monkeypatch):
    # The record says what it is, so the image decoder does not have to
    # be shown the whole file to say no.
    blob = private(mime="video/mp4")
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    assert dialog._loader.loads == []


def test_a_private_record_with_no_type_is_still_tried(tmp_path, monkeypatch):
    blob = private(mime="")
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([blob])),
    )
    assert dialog._loader.loads == [(CIPHER_SHA, f"https://one.example/{CIPHER_SHA}")]


# --------------------------------------------------------------------- #
# The library's own trouble reaches the person choosing a picture        #
# --------------------------------------------------------------------- #

class FakePrivateLibrary(QObject):
    """PrivateLibrary's observed surface: two signals and two reads."""

    library_changed = Signal()
    status_changed = Signal(str)

    def __init__(self, *, settled=False, failures=(), loading=False, status="") -> None:
        super().__init__()
        self.settled = settled
        self.failures = list(failures)
        self.blobs: dict = {}
        # ``loading`` separates work in progress from a finding, and
        # ``status`` is readable rather than only emitted, so a dialog
        # built after the load can ask what it missed.
        self.loading = loading
        self.status = status

    def get(self, sha256):
        return self.blobs.get((sha256 or "").lower())

    def vouches_for(self, sha256):
        return self.settled


def test_a_library_that_stayed_closed_is_said_out_loud(tmp_path, monkeypatch):
    # This sentence was emitted into nothing before: main_window never
    # connected status_changed, so the one place the app learns the
    # library did not open never reached a widget.
    library = FakePrivateLibrary()
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
        pick_mode=True,
    )
    dialog.bind_private_library(library)

    library.status_changed.emit(
        "Couldn't reach your signer, so your private library stayed closed: "
        "bunker relay refused the connection")

    assert dialog._library_label.isVisibleTo(dialog)
    assert "stayed closed" in dialog._library_label.text()


def test_a_library_nobody_has_read_says_that_much(tmp_path, monkeypatch):
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    dialog.bind_private_library(FakePrivateLibrary())

    assert dialog._library_label.isVisibleTo(dialog)
    assert dialog._library_label.text() == _LIBRARY_UNREAD


def test_a_files_own_reason_is_on_that_file(tmp_path, monkeypatch):
    # The reason names one file, so it belongs on that file. It used to
    # be concatenated into the banner along with every other file's,
    # which put a diagnostic dump above a grid and still said nothing
    # about the tile under the pointer.
    from nostr.media.private_library import LibraryFailure

    library = FakePrivateLibrary(failures=[
        LibraryFailure(identifier=CIPHER_SHA,
                       reason="Your signer could not open this file: refused."),
        LibraryFailure(identifier="b" * 64,
                       reason="This record is not a file, so it was skipped."),
    ])
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    dialog.bind_private_library(library)

    tooltip = dialog._grid.item(0).toolTip()
    assert "Your signer could not open this file" in tooltip
    # And not somebody else's reason.
    assert "not a file" not in tooltip


def test_the_banner_counts_rather_than_recites(tmp_path, monkeypatch):
    from nostr.media.private_library import LibraryFailure

    library = FakePrivateLibrary(failures=[
        LibraryFailure(identifier=CIPHER_SHA, reason="Your signer refused."),
        LibraryFailure(identifier="b" * 64, reason="This record is not a file."),
    ])
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    dialog.bind_private_library(library)

    text = dialog._library_label.text()
    assert "2 items" in text
    # One sentence about what it means, not a transcript of the load.
    assert "Your signer refused." not in text
    assert "cannot be used in published work" in text


def test_a_settled_library_says_nothing_at_all(tmp_path, monkeypatch):
    # When every tile is an answer rather than a guess there is nothing
    # to warn about, and a banner that never leaves is furniture.
    library = FakePrivateLibrary(settled=True)
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()],
        visibility=MediaVisibility(library=library),
    )
    dialog.bind_private_library(library)

    assert not dialog._library_label.isVisibleTo(dialog)
    assert dialog._items_by_hash[CIPHER_SHA].text().count("Not checked") == 0


def test_the_banner_clears_when_the_library_finishes(tmp_path, monkeypatch):
    library = FakePrivateLibrary()
    view = MediaVisibility(library=library)
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=view,
    )
    dialog.bind_private_library(library)
    assert dialog._library_label.isVisibleTo(dialog)

    library.settled = True
    library.status_changed.emit("1 file in your private library.")
    library.library_changed.emit()

    assert not dialog._library_label.isVisibleTo(dialog)
    assert view.state_of(CIPHER_SHA) == PUBLIC


def test_the_store_finishing_a_fetch_does_not_wipe_the_warning(
    tmp_path, monkeypatch,
):
    # The bottom status line belongs to the store and is cleared by every
    # refresh. A sentence saying these tiles cannot be trusted must not
    # live there.
    library = FakePrivateLibrary()
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    dialog.bind_private_library(library)
    library.status_changed.emit("Opening your private library...")

    dialog._store.fetch_finished.emit()

    assert dialog._library_label.isVisibleTo(dialog)
    assert "Opening your private library" in dialog._library_label.text()


def test_binding_a_library_repaints_the_grid_when_it_changes(
    tmp_path, monkeypatch,
):
    library = FakePrivateLibrary()
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()],
        visibility=MediaVisibility(library=library),
    )
    dialog.bind_private_library(library)
    assert "Not checked" in dialog._items_by_hash[CIPHER_SHA].text()

    library.settled = True
    library.blobs = {CIPHER_SHA: private()}
    library.library_changed.emit()

    assert "Private" in dialog._items_by_hash[CIPHER_SHA].text()


def test_a_signer_reason_is_shown_as_words_not_rendered_as_markup(
    tmp_path, monkeypatch,
):
    # A signer is somebody else's program and its reasons reach a label.
    # QLabel guesses rich text unless it is told not to, so without this
    # a reason containing markup would be rendered rather than read.
    from PySide6.QtCore import Qt

    library = FakePrivateLibrary()
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )
    dialog.bind_private_library(library)
    library.status_changed.emit("closed: <b>gone</b>")

    assert dialog._library_label.textFormat() == Qt.PlainText
    assert dialog._status_label.textFormat() == Qt.PlainText
    assert "<b>" in dialog._library_label.text()


def test_an_unchecked_encrypted_file_is_marked_wherever_it_shows_up(
    tmp_path, monkeypatch,
):
    """The route users were pushed down, closed at the end of it.

    Both pickers pre-select Images, and a server calls an envelope a byte
    stream, so an unread private picture is missing from the picker. That
    is what sends people to All files, where the same file used to be
    drawn as an ordinary public blob with no mark of any kind.
    """
    library = FakePrivateLibrary()
    dialog = build_dialog(
        tmp_path, monkeypatch, records=[media()],
        visibility=MediaVisibility(library=library), pick_mode=True,
    )
    dialog.bind_private_library(library)

    dialog._filter_combo.setCurrentIndex(1)      # Images
    assert CIPHER_SHA not in dialog._items_by_hash

    dialog._filter_combo.setCurrentIndex(0)      # All files
    assert "Not checked" in dialog._items_by_hash[CIPHER_SHA].text()
    dialog._grid.item(0).setSelected(True)
    assert dialog._notice_label.text() == _PICK_UNKNOWN_NOTICE

    # Once the library opens, the same file is a picture again and says
    # what it actually is.
    library.settled = True
    library.blobs = {CIPHER_SHA: private()}
    library.library_changed.emit()
    dialog._filter_combo.setCurrentIndex(1)

    assert "Private" in dialog._items_by_hash[CIPHER_SHA].text()
