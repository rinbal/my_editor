# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the app's one alert (alerts.py), the fix for issue #37.

Stock QMessageBox took its layout, button styling and text tint from the
Linux desktop theme, and call sites patched that with their own button
stylesheets. That is how Move to Trash ended up as white text on a white
button, and how "Delete file" showed up in the window manager's title bar
as a second headline. So these tests hold the alert to its contract:

  The headline is inside the alert; the title bar stays empty.

  Exactly one button is the default, Escape answers Cancel (or the default
  when there is no Cancel), and a destructive button is red and never the
  default.

  Names and error text are shown as text, never interpreted as markup.

  No stock QMessageBox call is left in the app, so the old look can't come
  back one call site at a time.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QIcon, QPixmap, QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

import alerts  # noqa: E402
import theme  # noqa: E402
from alerts import CANCEL, DEFAULT, DESTRUCTIVE, NORMAL, Alert, Button  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    icon = QPixmap(64, 64)
    icon.fill(QColor("#202020"))
    app.setWindowIcon(QIcon(icon))
    yield app


def save_changes_buttons():
    return (Button("Don't Save", "discard", DESTRUCTIVE),
            Button("Cancel", "cancel", CANCEL),
            Button("Save", "save", DEFAULT))


def test_the_headline_is_in_the_alert_and_the_title_bar_stays_empty():
    alert = Alert(None, title="Move “notes.txt” to the Trash?", message="m")
    assert alert.windowTitle() == ""
    assert alert.title_label.text() == "Move “notes.txt” to the Trash?"
    assert alert.title_label.objectName() == "alert_title"


def test_an_alert_without_a_message_hides_the_message_line():
    alert = Alert(None, title="You're up to date")
    assert alert.message_label.isHidden()


def test_each_button_answers_with_its_value():
    for label, value in (("Don't Save", "discard"), ("Cancel", "cancel"), ("Save", "save")):
        alert = Alert(None, title="t", buttons=save_changes_buttons())
        alert.buttons[label].click()
        assert alert.value == value
        assert alert.result() == QDialog.Accepted


def test_escape_answers_cancel():
    alert = Alert(None, title="t", buttons=save_changes_buttons())
    alert.reject()
    assert alert.value == "cancel"


def test_escape_answers_the_default_when_there_is_no_cancel():
    alert = Alert(None, title="t", buttons=(Button("Upload", True, NORMAL),
                                            Button("Keep Local", False, DEFAULT)))
    alert.reject()
    assert alert.value is False


def test_exactly_one_default_button():
    alert = Alert(None, title="t", buttons=save_changes_buttons())
    defaults = [label for label, b in alert.buttons.items() if b.isDefault()]
    assert defaults == ["Save"]
    with pytest.raises(ValueError):
        Alert(None, title="t", buttons=(Button("A", 1, NORMAL),))
    with pytest.raises(ValueError):
        Alert(None, title="t", buttons=(Button("A", 1, DEFAULT), Button("B", 2, DEFAULT)))


def test_roles_map_to_the_platforms_button_order():
    alert = Alert(None, title="t", buttons=save_changes_buttons())
    box = alert.button_box
    assert box.buttonRole(alert.buttons["Save"]) == QDialogButtonBox.AcceptRole
    assert box.buttonRole(alert.buttons["Cancel"]) == QDialogButtonBox.RejectRole
    assert box.buttonRole(alert.buttons["Don't Save"]) == QDialogButtonBox.DestructiveRole


@pytest.mark.parametrize("is_dark", [False, True])
def test_a_destructive_button_is_red_text_on_its_own_theme(is_dark):
    alert = Alert(None, title="t", buttons=save_changes_buttons(), is_dark=is_dark)
    assert alert.buttons["Don't Save"].objectName() == "destructive"
    css = theme.dialog_stylesheet(is_dark).lower()
    assert "qpushbutton#destructive" in css
    assert "color: white" not in css
    assert "transparent" not in css


def test_names_are_shown_as_text_not_markup():
    alert = Alert(None, title="Delete “<b>x</b>”?", message="<i>y</i>")
    assert alert.title_label.textFormat() == Qt.PlainText
    assert alert.message_label.textFormat() == Qt.PlainText


def test_details_start_hidden_and_show_on_request():
    alert = Alert(None, title="t", details="line one\nline two")
    assert alert.details_view.isHidden()
    toggle = next(b for b in alert.findChildren(alerts.QPushButton) if b.text() == "Show Details")
    toggle.click()
    assert not alert.details_view.isHidden()
    assert toggle.text() == "Hide Details"


def test_the_checkbox_reports_its_state():
    alert = Alert(None, title="t", checkbox="Remember this choice")
    assert alert.checked is False
    alert.checkbox.setChecked(True)
    assert alert.checked is True


def test_long_button_titles_stack_instead_of_overflowing():
    alert = Alert(None, title="t", buttons=(
        Button("Save as .txt Anyway", "anyway", DESTRUCTIVE),
        Button("Save as .html", "html", NORMAL),
        Button("Save as .rtf", "rtf", NORMAL),
        Button("Cancel", "cancel", DEFAULT)))
    assert alert.button_box.orientation() == Qt.Vertical


def test_two_short_buttons_sit_side_by_side():
    alert = Alert(None, title="t", buttons=(Button("Cancel", False, CANCEL),
                                            Button("OK", True, DEFAULT)))
    assert alert.button_box.orientation() == Qt.Horizontal


def test_the_caution_badge_marks_the_icon():
    plain = alerts._icon_pixmap(False).toImage()
    marked = alerts._icon_pixmap(True).toImage()
    assert plain != marked


def test_confirm_destructive_is_true_only_for_the_action(monkeypatch):
    answers = iter([True, False])

    class Fake:
        def __init__(self, parent, **kwargs):
            self.kwargs = kwargs
            buttons = kwargs["buttons"]
            assert [b.role for b in buttons] == [DESTRUCTIVE, DEFAULT]
            assert buttons[1].label == "Cancel"

        def run(self):
            return next(answers)

    monkeypatch.setattr(alerts, "Alert", Fake)
    assert alerts.confirm_destructive(None, title="t", action="Move to Trash") is True
    assert alerts.confirm_destructive(None, title="t", action="Move to Trash") is False


def test_the_theme_is_read_from_the_applied_palette(qt_app):
    theme.apply_app_theme(True)
    assert theme.is_dark_active() is True
    theme.apply_app_theme(False)
    assert theme.is_dark_active() is False


def _app_sources():
    skip = {".venv", "venv", "tests", "build", "dist", ".git", ".claude", "__pycache__"}
    for folder, subfolders, files in os.walk(ROOT):
        subfolders[:] = [d for d in subfolders if d not in skip]
        for name in files:
            if name.endswith(".py"):
                yield Path(folder, name)


def test_no_stock_message_box_calls_are_left_in_the_app():
    offenders = []
    for path in _app_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bQMessageBox\s*[.(]", line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == []
