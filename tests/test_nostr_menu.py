# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the Nostr menu and the account chip as the Human Interface
Guidelines ask for them.

What must hold:

  A command that acts for an account is dimmed while no account is in use,
  instead of answering a click with an alert (which used to point at an
  avatar chip nobody could see). The ways to an account (Create, Connect
  Signer App, Restore) and the membership stay available beside them, so
  the menu itself says what comes first. Back Up Account and Move Key to
  Signer App also need the key kept on this computer.

  The menu groups what you do, then the ways to an account, then the
  account in use; and titles say what the command does in the app's words:
  Connect Signer App…, Sign Out (not "Sign Out Active Profile"), Drafts (a
  panel that shows or hides takes a checkmark, never an ellipsis).

  The account chip is announced as the account, and named by the person's
  name, never by a key in hex.

  Once no account is in use, the Drafts panel goes with the account chip.
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

from test_commands import StandIn  # noqa: E402

PK = "ab" * 32


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def window_with(account):
    """The window's commands and menus, for ``account`` (None: no account)."""
    from main_window import MainWindow
    win = StandIn()
    MainWindow._build_actions(win)
    MainWindow._build_menu(win)
    for name in ("_LOCAL_KEY_COMMANDS", "_DOCUMENT_COMMANDS", "_DOCUMENT_PREFIXES"):
        setattr(win, name, getattr(MainWindow, name))
    win._is_document_command = types.MethodType(MainWindow._is_document_command, win)
    win._document_commands_dimmed = False      # no other window in front
    win.nostr_state = SimpleNamespace(active=account is not None)
    win._profile_store = SimpleNamespace(default=lambda: account)
    MainWindow._update_nostr_commands(win)
    return win


def enabled(win, command_id):
    return win.commands.action(command_id).isEnabled()


WAYS_IN = ("nostr.create_account", "nostr.connect", "nostr.restore_account",
           "nostr.membership")


def test_without_an_account_its_commands_are_dimmed_and_the_ways_in_are_not():
    win = window_with(None)
    for command in win.commands.commands():
        if command.nostr:
            assert not enabled(win, command.id), command.id
    for command_id in WAYS_IN:
        assert enabled(win, command_id), command_id


def test_an_account_in_a_signer_app_has_everything_but_the_key_commands():
    win = window_with(SimpleNamespace(is_local=False, user_pubkey=PK))
    for command in win.commands.commands():
        if command.nostr:
            expected = command.id not in ("nostr.backup_account", "nostr.move_to_signer")
            assert enabled(win, command.id) == expected, command.id


def test_an_account_kept_here_has_every_command():
    win = window_with(SimpleNamespace(is_local=True, user_pubkey=PK))
    assert all(enabled(win, c.id) for c in win.commands.commands() if c.nostr)


def test_a_command_dimmed_for_the_imports_window_gets_the_account_state_later():
    from main_window import MainWindow
    win = window_with(None)
    win._document_commands_dimmed = True
    win.nostr_state.active = True
    win._profile_store = SimpleNamespace(default=lambda: SimpleNamespace(is_local=False))
    MainWindow._update_nostr_commands(win)
    # Publishing is a document command: it stays dimmed while the window is
    # in front, and is remembered as enabled for when it goes.
    assert not enabled(win, "nostr.publish_article")
    assert win._dimmed_were_enabled["nostr.publish_article"] is True
    assert enabled(win, "nostr.media_library")


def nostr_menu(win):
    menu = next(m for m in win.menuBar().findChildren(QMenu)
                if m.parent() is win.menuBar() and "Nostr" in m.title())
    return ["-" if action.isSeparator() else action.objectName() for action in menu.actions()]


def test_the_menu_groups_the_ways_in_apart_from_the_account_in_use():
    entries = nostr_menu(window_with(None))
    ways = entries.index("nostr.create_account")
    assert entries[ways - 1] == "-"
    assert entries[ways:ways + 4] == ["nostr.create_account", "nostr.connect",
                                      "nostr.restore_account", "-"]
    assert entries[ways + 4:] == ["nostr.edit_profile", "nostr.backup_account",
                                  "nostr.move_to_signer", "nostr.sign_out"]


def test_titles_say_what_the_command_does():
    win = window_with(None)
    title = {c.id: c.title for c in win.commands.commands()}
    assert title["nostr.connect"] == "Connect Signer App…"
    assert title["nostr.sign_out"] == "Sign Out"
    assert title["help.about"] == "About MyEditor"


def test_a_command_with_a_checkmark_has_no_ellipsis():
    # An ellipsis promises a window that asks for more; a checkmark says a
    # state is on. One command cannot do both.
    for command in window_with(None).commands.commands():
        if command.checkable:
            assert not command.title.endswith(("…", "...")), command.title


def test_the_chip_is_the_account_and_named_by_the_person():
    from nostr.ui.profile_chip import ProfileChip
    chip = ProfileChip()
    assert chip.accessibleName() == "Account"
    chip.set_profile("Alice", PK)
    assert chip.toolTip() == "Alice"
    chip.set_profile("", PK)
    assert chip.toolTip().startswith("npub1") and PK[:8] not in chip.toolTip()


def test_an_account_without_a_name_is_shown_by_its_public_key():
    from nostr.profiles import Profile
    profile = Profile(user_pubkey=PK, bunker_pubkey="", bunker_relays=[],
                      local_secret_hex="")
    assert profile.npub_short().startswith("npub1")
    assert "…" in profile.npub_short()


def test_the_drafts_panel_goes_when_no_account_is_in_use():
    from main_window import MainWindow
    hidden, toggles = [], []
    win = SimpleNamespace(_hide_drafts_panel=lambda: hidden.append(True),
                          _show_pane_toggles=toggles.append,
                          _update_article_headers=lambda: None,
                          _redraw_badges=lambda: None,
                          _cancel_upload_waits=lambda: None)
    MainWindow._on_nostr_in_use_changed(win, True)
    assert hidden == [] and toggles == [True]
    # The panel and its toolbar toggles go together.
    MainWindow._on_nostr_in_use_changed(win, False)
    assert hidden == [True] and toggles == [True, False]


def test_publishing_is_said_in_words_without_an_event_id():
    """The status bar said "Published to Nostr: 3/5 relays · note1qy2…";
    it now says the note or article is published, and how many relays
    took it only when not all did."""
    from main_window import MainWindow
    said = []
    win = SimpleNamespace(status=SimpleNamespace(showMessage=lambda text, ms=0, **context: said.append(text)))
    win._say_published = lambda results, article: MainWindow._say_published(
        win, results, article=article)
    every = [("wss://a", True, ""), ("wss://b", True, "")]
    some = [("wss://a", True, ""), ("wss://b", False, "blocked")]
    MainWindow._on_nostr_note_published(win, "ab" * 32, every)
    MainWindow._on_nostr_article_published(win, "naddr1xyz", some)
    assert said == ["Your note is published.",
                    "Your article is published; 1 of 2 relays took it."]
    assert not any("note1" in text or "naddr" in text for text in said)
