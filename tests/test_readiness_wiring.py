"""The window hands the page's line and Article Details one Publication
Check verdict, live, and asks for STANDUP's while STANDUP is in use."""

from tests.app_process import run_window_script

SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
main_window.MainWindow._maybe_auto_check_for_updates = lambda self: None

def settle(seconds=0.6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)

w = main_window.MainWindow()
w.resize(1600, 800)
w.show()
w.nostr_state._active = True
w._on_nostr_in_use_changed(True)
w.act_nostr_submissions.setVisible(False)          # STANDUP not in use
ed = w.new_tab()
ed.setPlainText("Kite festival at the lake\n\nThree days of wind.")
w.act_article_details.setChecked(True)
settle()
panel = w._article_details_panel
line = ed._article_header.header.readiness
r = {"line": line.text(), "panel": panel.readiness._headline.text()}
panel.summary_edit.setPlainText("Three days of wind, forty kites and what to bring next year.")
settle()
r["line_after_summary"] = line.text()
w.act_article_details.setChecked(False)
settle()
line.click()
settle()
r["opened"] = w.act_article_details.isChecked() and panel.isAncestorOf(app.focusWidget())
w.act_nostr_submissions.setVisible(True)           # STANDUP in use
w._show_article_from_text()
r["standup"] = line.text()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_page_and_article_details_follow_one_verdict(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["line"] == "Ready to Publish · 3 suggestions"
    assert r["panel"] == "Ready to Publish"
    assert r["line_after_summary"] == "Ready to Publish · 2 suggestions"
    assert r["opened"]
    assert r["standup"].startswith("Ready for STANDUP · ")
