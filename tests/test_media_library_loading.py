# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading the private library is work, and work is not an error.

What the user met: every tile stamped NOT CHECKED, and an amber banner
reading "Your private library has not been read yet, so this app cannot
tell which of these files are private. This record does not name a file,
so it was skipped. 3 more could not be opened." Three unrelated
complaints in one sentence, above a grid of files that were all fine.

Two separate faults produced that, and both are pinned here. The load
takes a signer round-trip per file, so there is a window where the
answers genuinely are not in yet, and reporting a verdict during that
window is reporting one the app has not reached. And the banner spoke
for the load rather than for the user.

The softening is strictly cosmetic and strictly bounded: what is
undecided while a load runs is the badge, never the permission.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nostr.media.media_visibility import PRIVATE, PUBLIC, UNKNOWN
from nostr.ui.media_library_dialog import _LIBRARY_CHECKING, _PENDING

from test_media_library_visibility import (  # noqa: E402
    CIPHER_SHA,
    FakePrivateLibrary,
    build_dialog,
    media,
    private,
    _unchecked,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    return build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
    )


def tile_text(dlg):
    return dlg._grid.item(0).text()


def banner(dlg):
    return dlg._library_label.text() if dlg._library_label.isVisibleTo(dlg) else ""


# --------------------------------------------------------------------- #
# While the load is running                                             #
# --------------------------------------------------------------------- #

def test_a_running_load_does_not_stamp_a_verdict_on_every_tile(dialog):
    dialog.bind_private_library(FakePrivateLibrary(loading=True))
    assert dialog._shown_state(CIPHER_SHA) == _PENDING
    assert "Not checked" not in tile_text(dialog)


def test_a_running_load_says_it_is_working_not_that_it_failed(dialog):
    dialog.bind_private_library(FakePrivateLibrary(loading=True))
    assert banner(dialog) == _LIBRARY_CHECKING
    # And it does not wear the colour the dialog uses for trouble.
    assert dialog._library_label.property("tone") == "working"


def test_the_working_line_is_about_progress_not_absence(dialog):
    dialog.bind_private_library(FakePrivateLibrary(loading=True))
    text = banner(dialog).lower()
    assert "checking" in text
    assert "cannot" not in text and "not been read" not in text


def test_a_finished_load_that_settled_says_nothing_at_all(dialog):
    dialog.bind_private_library(FakePrivateLibrary(settled=True))
    assert banner(dialog) == ""


def test_a_load_that_finished_unresolved_does_wear_the_warning(dialog):
    # The window has closed and the answer never came: now it is a
    # finding, and it gets the amber it deserves.
    dialog.bind_private_library(FakePrivateLibrary(loading=False))
    assert banner(dialog)
    assert dialog._library_label.property("tone") != "working"
    assert dialog._shown_state(CIPHER_SHA) == UNKNOWN
    assert "Not checked" in tile_text(dialog)


def test_the_verdict_appears_when_the_load_finishes(dialog):
    library = FakePrivateLibrary(loading=True)
    dialog.bind_private_library(library)
    assert "Not checked" not in tile_text(dialog)

    library.loading = False
    library.library_changed.emit()
    assert "Not checked" in tile_text(dialog)
    assert banner(dialog)


# --------------------------------------------------------------------- #
# The softening is cosmetic only                                        #
# --------------------------------------------------------------------- #

def test_a_pending_file_is_still_unknown_to_the_gate(dialog):
    dialog.bind_private_library(FakePrivateLibrary(loading=True))
    # What the grid draws is softened; what the publish gate asks is not.
    assert dialog._shown_state(CIPHER_SHA) == _PENDING
    assert dialog._visibility.state_of(CIPHER_SHA) == UNKNOWN
    assert dialog._visibility.is_unknown(CIPHER_SHA)


def test_the_picker_says_it_is_checking_rather_than_refusing(
    tmp_path, monkeypatch,
):
    dlg = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
        pick_mode=True,
    )
    dlg.bind_private_library(FakePrivateLibrary(loading=True))
    dlg._grid.setCurrentRow(0)
    dlg._refresh_pick_notice()

    notice = dlg._notice_label.text().lower()
    assert "checking" in notice
    # "cannot be used" is the wrong thing to tell someone whose answer is
    # two seconds away.
    assert "cannot be used" not in notice


def test_the_picker_refuses_once_the_load_is_actually_done(
    tmp_path, monkeypatch,
):
    dlg = build_dialog(
        tmp_path, monkeypatch, records=[media()], visibility=_unchecked(),
        pick_mode=True,
    )
    dlg.bind_private_library(FakePrivateLibrary(loading=False))
    dlg._grid.setCurrentRow(0)
    dlg._refresh_pick_notice()
    assert "cannot be used" in dlg._notice_label.text().lower()


def test_a_known_private_file_is_never_softened(tmp_path, monkeypatch):
    # Only UNKNOWN is ever softened. A file the library has answered for
    # keeps its badge whatever else is still loading.
    from nostr.media.media_visibility import MediaVisibility

    from test_media_library_visibility import FakeLibrary

    dlg = build_dialog(
        tmp_path, monkeypatch, records=[media()],
        visibility=MediaVisibility(library=FakeLibrary([private()], vouches=False)),
    )
    dlg.bind_private_library(FakePrivateLibrary(loading=True))
    assert dlg._shown_state(CIPHER_SHA) == PRIVATE
    assert "Private" in tile_text(dlg)


def test_with_no_library_bound_nothing_is_pending(dialog):
    # An account with no signer has no private library, and every blob is
    # simply public. That answer is not softened either.
    assert not dialog._library_pending()


# --------------------------------------------------------------------- #
# The status a dialog was not around to hear                            #
# --------------------------------------------------------------------- #

def test_a_dialog_built_after_the_load_asks_instead_of_assuming(dialog):
    # The library outlives the dialog. Opening the media library a second
    # time used to report a library read minutes ago as never read,
    # because this instance never saw status_changed go by.
    library = FakePrivateLibrary(
        loading=False, status="12 files in your private library.",
    )
    dialog.bind_private_library(library)
    assert "12 files in your private library." in banner(dialog)
    assert "has not been read yet" not in banner(dialog)
