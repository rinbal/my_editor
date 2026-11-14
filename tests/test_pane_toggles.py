# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how the side panels are shown and hidden.

What must hold (Apple's guidelines: "Provide multiple ways to reveal
hidden panes... a toolbar button or a menu command, including a keyboard
shortcut", and the toolbar's trailing edge holds "buttons that open
nearby inspectors"):

  The end of the format toolbar, outside its overflow chevron, has a
  toggle for Article Details and one for the Drafts panel, shown only
  while a Nostr account is in use. Each says whether its pane is open
  (checked), carries a name and its key in the tooltip, and opens or
  closes its pane. The Drafts toggle closes the panel whichever view it
  shows. Neither panel has a close button of its own any more; the
  Drafts panel's action menu has Hide Drafts.

  While the Drafts panel is closed and a submission holds something not
  read yet, the Drafts toggle carries the dot the Einreichungen tab
  shows, and a screen reader hears "Drafts, New". With the panel open
  the tab says it, so the toggle does not.
"""

from tests.app_process import run_window_script

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QToolButton
app = QApplication(sys.argv[:1])
import main_window
from nostr.profiles import Profile
from nostr.ui.published_view import PANEL_VIEW
main_window.MainWindow._maybe_auto_check_for_updates = lambda self: None

w = main_window.MainWindow()
w.resize(1300, 760)
w.show()
app.processEvents()
details, drafts = w._details_toggle, w._drafts_toggle
r = {"without_account": [details.isVisibleTo(w), drafts.isVisibleTo(w)]}
w.nostr_state._active = True
w.nostr_state.changed.emit(True)
w._update_nostr_commands()                      # as an account coming into use does
w._drafts_panel.set_active_profile(Profile(user_pubkey="a" * 64, bunker_pubkey="b" * 64,
                                           bunker_relays=["wss://relay.example"],
                                           local_secret_hex="c" * 64, display_name="Alice"))
app.processEvents()
r["with_account"] = [details.isVisibleTo(w), drafts.isVisibleTo(w)]
r["outside_chevron"] = [not w.format_toolbar.isAncestorOf(b) and w.format_bar.isAncestorOf(b)
                        for b in (details, drafts)]
r["names"] = [details.accessibleName(), drafts.accessibleName()]
r["tooltips"] = [details.toolTip(), drafts.toolTip()]
r["keys"] = [w.act_article_details.shortcut().toString(),
             w.act_nostr_drafts.shortcut().toString()]
drafts.click(); app.processEvents()
r["drafts_open"] = [w._drafts_panel.isVisible(), drafts.isChecked()]
w._drafts_panel.show_view(PANEL_VIEW)
drafts.click(); app.processEvents()
r["published_closed"] = [w._drafts_panel.isVisible(), drafts.isChecked()]
w._on_toggle_drafts_panel(); app.processEvents()          # the menu command
r["menu_follows"] = drafts.isChecked()
details.click(); app.processEvents()
r["details_open"] = [w._article_details_panel.isVisible(), details.isChecked(),
                     w.act_article_details.isChecked()]
r["close_buttons"] = [b.text() for p in (w._drafts_panel, w._article_details_panel)
                      for b in p.findChildren(QToolButton) if b.text() == "×"]
panel = w._drafts_panel
panel._update_actions_menu()
hide = [a for a in panel._actions_menu.actions() if a.text() == "Hide Drafts"]
r["hide_in_menu"] = len(hide)
hide[0].trigger(); app.processEvents()
r["hidden_by_menu"] = [panel.isVisible(), drafts.isChecked()]
def dot():
    # The icon's top corner, where only the dot is drawn.
    return drafts.icon().pixmap(18, 18).toImage().pixelColor(14, 3).alpha() > 200
unread = w._submissions_view.unread_changed
r["mark"] = [[drafts.accessibleName(), dot()]]
unread.emit(True); app.processEvents()
r["mark"].append([drafts.accessibleName(), dot()])
drafts.click(); app.processEvents()                      # the panel open: its tab says it
r["mark"].append([drafts.accessibleName(), dot()])
drafts.click(); app.processEvents()
r["mark"].append([drafts.accessibleName(), dot()])
unread.emit(False); app.processEvents()
r["mark"].append([drafts.accessibleName(), dot()])
w.nostr_state._active = False
w.nostr_state.changed.emit(False)
w._update_nostr_commands()
app.processEvents()
r["account_gone"] = [details.isVisibleTo(w), drafts.isVisibleTo(w),
                     w._article_details_panel.isVisible()]
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_toolbar_toggles_show_and_hide_the_side_panels(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["without_account"] == [False, False]
    assert r["with_account"] == [True, True]
    assert r["outside_chevron"] == [True, True]
    assert r["names"] == ["Article Details", "Drafts"]
    for tooltip, keys in zip(r["tooltips"], r["keys"]):
        assert keys and tooltip.endswith(")"), tooltip
    assert r["drafts_open"] == [True, True]
    assert r["published_closed"] == [False, False]
    assert r["menu_follows"] is True
    assert r["details_open"] == [True, True, True]
    assert r["close_buttons"] == []
    assert r["hide_in_menu"] == 1
    assert r["hidden_by_menu"] == [False, False]
    assert r["mark"] == [["Drafts", False], ["Drafts, New", True], ["Drafts", False],
                         ["Drafts, New", True], ["Drafts", False]]
    assert r["account_gone"] == [False, False, False]
