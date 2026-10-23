# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the panel shows while drafts wait for the signer app.

Reported with two screenshots: a red line "Your signer app is not
responding" with a Try Again button at the top, an empty state under it
with an alert, the same title, a paragraph and a second Try Again, the
same words again in the window's status bar; and after a retry a red
"48 drafts could not be decrypted" over 48 identical rows "(no title)",
each with a red alert.

A signer app on a phone that is asleep is the ordinary case, so the panel
says it once, quietly, and gets out of the way:

  - one line at the top of the list, in secondary text with a small lock,
    with one Try Again link;
  - the drafts that wait are that line, not a row each;
  - drafts already open list as always, and the ones that open later
    join them.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QPushButton

from nostr.ui.context_widgets import ContextStatusBar
from nostr.draft_store import DraftStore
from nostr.drafts import DraftWrapMeta
from nostr.profiles import Profile
from nostr.ui.drafts_common import THEME_TOKENS
from nostr.ui.drafts_panel import DraftsPanel
from tests.accessibility import out_of_tab_reach, unnamed_controls
from tests.widget_lifetime import delete_new_windows


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every window and panel a test makes is deleted after it: left to
    the cycle collector, one without a parent can crash it."""
    yield from delete_new_windows()


PK = "a" * 64


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def make_profile() -> Profile:
    return Profile(
        user_pubkey=PK, bunker_pubkey="b" * 64,
        bunker_relays=["wss://relay.example"], local_secret_hex="c" * 64,
        display_name="Alice",
    )


def wrap(identifier: str, created_at: int = 1700000000) -> DraftWrapMeta:
    return DraftWrapMeta(
        identifier=identifier, inner_kind=0, event_id=identifier + "e",
        pubkey=PK, created_at=created_at, expiration=None, ciphertext="ct",
    )


def locked_store(count: int = 48) -> DraftStore:
    """Every draft present, none of them readable. The reported state."""
    store = DraftStore()
    store.bind_profile(PK)
    for i in range(count):
        store.upsert_skeleton(wrap(f"d{i}", created_at=1700000000 + i))
    return store


def open_draft(store: DraftStore, identifier: str, text: str) -> None:
    store.set_decrypted(identifier, inner={"kind": 1, "content": text, "tags": []})


def panel(store: DraftStore, *, is_dark: bool = True) -> DraftsPanel:
    p = DraftsPanel(is_dark=is_dark)
    p.set_active_profile(make_profile())
    p.bind_store(store)
    return p


def waiting(store: DraftStore, **kw) -> DraftsPanel:
    p = panel(store, **kw)
    p.set_signer_unreachable(True)
    return p


def status_of(p: DraftsPanel):
    return p._status_line()


def showing_placeholder(p: DraftsPanel) -> bool:
    return p._body_stack.currentIndex() == 1


def listed(p: DraftsPanel):
    return [p._list.item(row).data(Qt.UserRole) for row in range(p._list.count())]


def shown(p: DraftsPanel, kind):
    return [w for w in p.findChildren(kind) if w.isVisibleTo(p)]


def try_again_buttons(p: DraftsPanel):
    return [b for b in shown(p, QPushButton) if b.text() == "Try Again"]


# --------------------------------------------------------------------- #
# One quiet line                                                        #
# --------------------------------------------------------------------- #

def test_drafts_that_wait_are_one_line_not_a_row_each():
    p = waiting(locked_store(48))
    assert listed(p) == []
    assert status_of(p) == ("48 drafts are locked until your signer app answers.", False)
    assert p._locked_label.isVisibleTo(p)
    assert p._locked_label.text() == "48 drafts are locked until your signer app answers."


def test_one_draft_reads_as_one():
    p = waiting(locked_store(1))
    assert status_of(p)[0] == "1 draft is locked until your signer app answers."


def test_one_try_again_and_no_message_under_the_line():
    p = waiting(locked_store())
    assert len(try_again_buttons(p)) == 1
    # The empty state used to say it all again, with a second button.
    assert not showing_placeholder(p)
    assert not p._empty_action.isVisibleTo(p)


def test_the_line_is_quiet_not_red():
    for dark in (True, False):
        p = waiting(locked_store(), is_dark=dark)
        label = p._locked_label
        label.ensurePolished()
        colour = label.palette().color(label.foregroundRole()).name().lower()
        assert colour == THEME_TOKENS[dark]["muted"].lower()
        assert colour != THEME_TOKENS[dark]["error_fg"].lower()


def test_a_small_lock_goes_with_the_line():
    p = waiting(locked_store())
    assert p._locked_symbol.isVisibleTo(p)
    assert not p._locked_symbol.pixmap().isNull()
    # Decoration: the sentence says it.
    assert p._locked_symbol.accessibleName() == ""


def test_drafts_already_open_list_as_always():
    store = locked_store(4)
    open_draft(store, "d0", "First")
    open_draft(store, "d1", "Second")
    p = waiting(store)
    assert sorted(listed(p)) == ["d0", "d1"]
    assert status_of(p)[0] == "2 drafts are locked until your signer app answers."


def test_the_draft_someone_is_reading_keeps_its_place():
    # The signer can go quiet at any moment: the list must not jump back
    # to its first row, or lose what was selected, when the rows still
    # decrypting leave for the line.
    store = locked_store(12)
    for i in range(9):
        open_draft(store, f"d{i}", f"Draft {i}")
    p = panel(store)
    p._list.setCurrentItem(p._items["d3"])
    p.set_signer_unreachable(True)
    assert p._list.currentItem() is p._items["d3"]
    assert p.selected_identifiers() == ["d3"]
    p.set_signer_unreachable(False)
    assert p._list.currentItem() is p._items["d3"]
    assert len(listed(p)) == 12


def test_a_draft_that_opens_joins_the_list_and_the_line_counts_down():
    store = locked_store(3)
    p = waiting(store)
    open_draft(store, "d2", "Finally")
    assert listed(p) == ["d2"]
    assert status_of(p)[0] == "2 drafts are locked until your signer app answers."


def test_a_newer_version_that_cannot_open_yet_waits_with_the_others():
    store = locked_store(2)
    open_draft(store, "d0", "Readable")
    p = waiting(store)
    store.upsert_skeleton(wrap("d0", created_at=1800000000))
    assert listed(p) == []
    assert status_of(p)[0] == "2 drafts are locked until your signer app answers."


def test_try_again_asks_the_host():
    p = waiting(locked_store())
    seen = []
    p.retry_signer.connect(lambda: seen.append(True))
    try_again_buttons(p)[0].click()
    assert seen == [True]


def test_try_again_is_a_link():
    p = waiting(locked_store())
    button = try_again_buttons(p)[0]
    assert button.cursor().shape() == Qt.PointingHandCursor
    assert button.isFlat()
    assert button.font().pointSizeF() == p._locked_label.font().pointSizeF()


def test_when_the_signer_answers_the_line_goes_and_the_drafts_open():
    p = waiting(locked_store(3))
    p.set_signer_unreachable(False)
    assert not p._locked_label.isVisibleTo(p)
    assert try_again_buttons(p) == []
    assert len(listed(p)) == 3
    assert status_of(p) == ("Decrypting 0 of 3", False)


def test_with_nothing_locked_there_is_no_line():
    store = locked_store(2)
    open_draft(store, "d0", "One")
    open_draft(store, "d1", "Two")
    p = waiting(store)
    assert status_of(p) == ("", False)
    assert try_again_buttons(p) == []


def test_an_incapable_signer_still_wins_because_no_retry_can_help():
    p = waiting(locked_store(2))
    p.set_signer_unsupported(True)
    assert status_of(p)[0] == "Your signer app can’t decrypt drafts"
    assert not p._locked_label.isVisibleTo(p)
    assert try_again_buttons(p) == []


def test_a_search_that_finds_nothing_still_says_so():
    store = locked_store(2)
    open_draft(store, "d0", "Pineapple")
    p = waiting(store)
    p._search_edit.setText("zzzznomatch")
    assert showing_placeholder(p)
    assert p._empty_action.text() == "Clear Search"
    p._empty_action.click()
    assert p._search_edit.text() == ""
    assert not showing_placeholder(p)


def test_every_control_has_a_name_and_is_in_reach():
    p = waiting(locked_store())
    assert unnamed_controls(p) == []
    assert out_of_tab_reach(p) == []


# --------------------------------------------------------------------- #
# The shared empty-state button                                         #
# --------------------------------------------------------------------- #

def test_a_placeholder_with_no_remedy_hides_the_button():
    p = DraftsPanel()
    p.set_active_profile(None)
    assert showing_placeholder(p)
    assert not p._empty_action.isVisibleTo(p)


def test_every_control_has_a_name_in_each_empty_state():
    """A screen reader names the list and the empty state's button in
    every state: no drafts, drafts waiting, nothing matching, and back."""
    empty_store = DraftStore()
    empty_store.bind_profile(PK)
    empty = panel(empty_store)
    assert showing_placeholder(empty)
    assert not empty._empty_action.isVisibleTo(empty)
    assert unnamed_controls(empty) == []
    assert empty._list.accessibleName() == "Drafts"

    store = locked_store(2)
    open_draft(store, "d0", "Pineapple")
    searching = waiting(store)
    searching._search_edit.setText("zzzznomatch")
    assert showing_placeholder(searching)
    assert unnamed_controls(searching) == []
    searching._search_edit.setText("")
    assert unnamed_controls(searching) == []


# --------------------------------------------------------------------- #
# The window                                                            #
# --------------------------------------------------------------------- #

def test_the_window_status_bar_stays_quiet():
    from main_window import MainWindow
    host = SimpleNamespace(_drafts_panel=MagicMock(), status=ContextStatusBar())
    MainWindow._on_draft_sync_signer_unreachable(host, True)
    host._drafts_panel.set_signer_unreachable.assert_called_once_with(True)
    assert host.status.currentMessage() == ""


def test_coming_back_to_the_app_asks_the_signer_again():
    from main_window import MainWindow
    host = SimpleNamespace(_draft_sync=MagicMock())
    MainWindow._on_application_state_changed(host, Qt.ApplicationState.ApplicationInactive)
    host._draft_sync.ask_signer_again.assert_not_called()
    MainWindow._on_application_state_changed(host, Qt.ApplicationState.ApplicationActive)
    host._draft_sync.ask_signer_again.assert_called_once_with()


def test_quitting_stops_the_asking_before_the_signer_goes(monkeypatch):
    from main_window import MainWindow
    from tests.test_quit_sends_sources import closing_host
    order = []
    host = closing_host(monkeypatch, order)
    host._draft_sync = SimpleNamespace(stop=lambda: order.append("drafts stopped"))
    MainWindow.closeEvent(host, QCloseEvent())
    assert order.index("drafts stopped") < order.index("signer closed")


# --------------------------------------------------------------------- #
# Said whole                                                            #
# --------------------------------------------------------------------- #

NARROW_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import i18n
i18n.install(sys.argv[2])
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import theme
app.setStyle(theme.AppStyle("Fusion"))
from nostr.ui import drafts_panel as dp
from tests.test_drafts_panel_signer_silence import locked_store, panel
r = {}
for state in ("unreachable", "unsupported"):
    p = panel(locked_store(0 if state == "unsupported" else 48))
    p.resize(dp.MIN_PANEL_WIDTH, 480)
    p.show()
    app.processEvents()
    getattr(p, f"set_signer_{state}")(True)
    app.processEvents()
    text, _problem = p._status_line()
    if state == "unreachable":
        # The waiting line wraps: every word is there, however narrow.
        label = p._locked_label
        r[state] = [text, label.heightForWidth(label.width()), label.height(),
                    label.text() == text, label.wordWrap()]
    else:
        label = p._status_label
        r[state] = [text, label.fontMetrics().horizontalAdvance(text), label.width()]
    p.close()
print("RESULT " + json.dumps(r, ensure_ascii=False))
"""


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_signer_lines_fit_the_narrowest_panel_whole(tmp_path, language):
    # Review N3: the German "Deine Signer-App kann Entwürfe nicht
    # entschlüsseln" needed 307 px in a 236 px line and lost its verb to
    # the ellipsis. A status line is said whole or not at all.
    from tests.app_process import run_window_script
    r = run_window_script(NARROW_SCRIPT, tmp_path, language)
    text, needs, room, whole, wraps = r["unreachable"]
    assert whole and wraps and needs <= room, r["unreachable"]
    text, needs, room = r["unsupported"]
    assert needs <= room, r["unsupported"]
