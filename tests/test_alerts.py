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

  The buttons stand where the Human Interface Guidelines put them, on
  every platform, whatever order Qt's button box would pick there: in a
  row the default at the trailing edge, Cancel beside it and a destructive
  choice at the leading edge; in a stack the default on top and Cancel at
  the bottom. (A German stack once had the default between the other
  two.) Tab follows that order, and the default button has the focus.

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

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QIcon, QPixmap, QColor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QDialogButtonBox, QProxyStyle, QStyle, QWidget,
)

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


@pytest.mark.parametrize("choose, again", [("Try Again", True), ("Cancel", False),
                                           (None, False)], ids=["Try Again", "Cancel", "Esc"])
def test_a_total_failure_offers_try_again_and_cancel(monkeypatch, choose, again):
    # Lessons 36: what did not happen, one sentence, the details behind
    # Details, Try Again the default beside Cancel; only Try Again says yes.
    shown = []

    def run(alert):
        shown.append(alert)
        if choose is None:
            alert.reject()
        else:
            alert.buttons[choose].click()
        return alert.value

    monkeypatch.setattr(Alert, "run", run)
    assert alerts.ask_try_again(None, title="Your data wasn’t saved", message="It didn’t work.",
                                details="errno 28") is again
    alert, = shown
    assert list(alert.buttons) == ["Cancel", "Try Again"]
    assert alert.buttons["Try Again"].isDefault()


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


def seen_order(alert):
    """The buttons' labels as a person sees them: left to right in a row,
    top to bottom in a stack."""
    alert.show()
    QApplication.processEvents()
    spots = {label: button.mapTo(alert, QPoint(0, 0))
             for label, button in alert.buttons.items()}
    alert.close()
    key = ((lambda label: spots[label].y()) if alert.orientation == Qt.Vertical
           else (lambda label: spots[label].x()))
    return sorted(spots, key=key)


class _ButtonLayoutStyle(QProxyStyle):
    """The style a platform brings, as far as button order goes."""

    def __init__(self, layout):
        super().__init__("Fusion")
        self._layout = layout

    def styleHint(self, hint, option=None, widget=None, data=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_DialogButtonLayout:
            return self._layout.value
        return super().styleHint(hint, option, widget, data)


LAYOUTS = [QDialogButtonBox.ButtonLayout.WinLayout, QDialogButtonBox.ButtonLayout.MacLayout,
           QDialogButtonBox.ButtonLayout.KdeLayout, QDialogButtonBox.ButtonLayout.GnomeLayout]


@pytest.fixture(params=LAYOUTS, ids=["windows", "mac", "kde", "gnome"])
def platform_style(request, qt_app):
    """Each platform's preferred button order, in turn. The style the
    tests run with is put back by name afterwards (Fusion)."""
    before = qt_app.style().name() or "Fusion"
    qt_app.setStyle(_ButtonLayoutStyle(request.param))
    yield request.param
    qt_app.setStyle(before)


def test_in_a_row_the_default_is_trailing_with_cancel_beside_it(platform_style):
    alert = Alert(None, title="t", buttons=save_changes_buttons())
    assert alert.orientation == Qt.Horizontal
    assert seen_order(alert) == ["Don't Save", "Cancel", "Save"]


def test_other_choices_sit_beside_cancel_and_the_default_stays_trailing(platform_style):
    alert = Alert(None, title="t", buttons=(Button("Again", "again", DEFAULT),
                                            Button("New", "new", NORMAL),
                                            Button("Cancel", "cancel", CANCEL)))
    assert seen_order(alert) == ["New", "Cancel", "Again"]


def test_a_destructive_choice_stands_away_from_the_default(platform_style):
    alert = Alert(None, title="t", buttons=(Button("Delete", True, DESTRUCTIVE),
                                            Button("Cancel", False, DEFAULT)))
    alert.show()
    QApplication.processEvents()
    delete, cancel = alert.buttons["Delete"], alert.buttons["Cancel"]
    gap = cancel.mapTo(alert, QPoint(0, 0)).x() - delete.mapTo(alert, QPoint(0, 0)).x()
    alert.close()
    assert gap > delete.width() + 8          # not side by side: a space between them
    assert seen_order(alert) == ["Delete", "Cancel"]


def test_in_a_stack_the_default_is_on_top_and_cancel_at_the_bottom(platform_style):
    # German titles, which once stacked with the default in the middle.
    alert = Alert(None, title="t", buttons=(
        Button("Als neu veröffentlichen und ersetzen", "new", NORMAL),
        Button("Abbrechen", "cancel", CANCEL),
        Button("Erneut versuchen", "again", DEFAULT),
        Button("Entwurf löschen und schließen", "drop", DESTRUCTIVE)))
    assert alert.orientation == Qt.Vertical
    assert seen_order(alert) == ["Erneut versuchen", "Als neu veröffentlichen und ersetzen",
                                 "Entwurf löschen und schließen", "Abbrechen"]


def test_tab_follows_the_order_and_starts_on_the_default():
    alert = Alert(None, title="t", buttons=save_changes_buttons(), checkbox="Remember")
    assert alert.focusWidget() is alert.buttons["Save"]
    chain, widget = [], alert.checkbox
    for _ in range(4):
        chain.append(widget)
        widget = widget.nextInFocusChain()
        while widget.focusPolicy() & Qt.TabFocus == 0 or widget is alert:
            widget = widget.nextInFocusChain()
    assert chain == [alert.checkbox, alert.buttons["Don't Save"], alert.buttons["Cancel"],
                     alert.buttons["Save"]]


def test_tab_reaches_the_details_where_they_are_shown():
    # Review L1: the details text came last, after the buttons.
    alert = Alert(None, title="t", buttons=save_changes_buttons(), checkbox="Remember",
                  details="What went wrong, in full.")
    alert._details_toggle.click()
    chain, widget = [], alert._details_toggle
    for _ in range(6):
        chain.append(widget)
        widget = widget.nextInFocusChain()
        while widget.focusPolicy() & Qt.TabFocus == 0 or widget is alert:
            widget = widget.nextInFocusChain()
    assert chain == [alert._details_toggle, alert.details_view, alert.checkbox,
                     alert.buttons["Don't Save"], alert.buttons["Cancel"],
                     alert.buttons["Save"]]


def test_space_on_the_focused_button_chooses_the_default():
    from PySide6.QtTest import QTest
    alert = Alert(None, title="t", buttons=save_changes_buttons())
    alert.show()
    alert.activateWindow()
    QApplication.processEvents()
    QTest.keyClick(QApplication.focusWidget() or alert, Qt.Key_Space)
    assert alert.value == "save"


@pytest.mark.parametrize("mac, chosen", [(True, "save"), (False, "cancel")])
def test_return_on_a_focused_button_follows_the_system(monkeypatch, mac, chosen):
    # Review R2: on a Mac, Return chooses the default whichever button has
    # the focus (Space presses the focused one); on Windows and Linux,
    # Return presses the focused button, as their own dialogs do.
    from PySide6.QtTest import QTest
    monkeypatch.setattr(alerts, "IS_MAC", mac)
    alert = Alert(None, title="t", buttons=save_changes_buttons())
    alert.show()
    alert.activateWindow()
    QApplication.processEvents()
    cancel = alert.buttons["Cancel"]
    cancel.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    QTest.keyClick(cancel, Qt.Key_Return)
    assert alert.value == chosen


@pytest.mark.parametrize("mac", [True, False])
@pytest.mark.parametrize("is_dark", [False, True])
def test_a_focused_destructive_button_can_be_read(monkeypatch, mac, is_dark):
    # Review S1: on Windows and Linux the focused button becomes the default,
    # and the default's accent fill put the red "Don't Save" at 1.2:1. It
    # keeps the plain fill and its red label; the ring marks the focus.
    from PySide6.QtCore import QPoint
    from tests.test_theme import _contrast_ratio
    monkeypatch.setattr(alerts, "IS_MAC", mac)
    alert = Alert(None, title="t", buttons=save_changes_buttons(), is_dark=is_dark)
    alert.show()
    alert.activateWindow()
    QApplication.processEvents()
    try:
        button = alert.buttons["Don't Save"]
        button.setFocus(Qt.FocusReason.TabFocusReason)
        QApplication.processEvents()
        fill = button.grab().toImage().pixelColor(QPoint(5, 4)).name()
        red = theme._DIALOG_DESTRUCTIVE[is_dark]
        assert _contrast_ratio(red, fill) >= 4.5, (fill, red)
        edge = button.grab().toImage().pixelColor(1, button.height() // 2)
        assert edge == QColor(theme.focus_ring_color(is_dark))
    finally:
        alert.close()


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
    assert alert.orientation == Qt.Vertical
    assert seen_order(alert) == ["Cancel", "Save as .html", "Save as .rtf",
                                 "Save as .txt Anyway"]


def test_two_short_buttons_sit_side_by_side():
    alert = Alert(None, title="t", buttons=(Button("Cancel", False, CANCEL),
                                            Button("OK", True, DEFAULT)))
    assert alert.orientation == Qt.Horizontal
    assert seen_order(alert) == ["Cancel", "OK"]


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
