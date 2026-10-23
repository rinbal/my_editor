# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how Save as Nostr Draft is used without a mouse, and that its
kind cards are never cut off.

What must hold:

  The kinds (Short note, Long-form article) are a radio group the
  keyboard reaches: Tab gets to the chosen one and the arrow keys choose
  the other, which opens the article's fields. Each is named for a screen
  reader. (Review R4: the cards answered the mouse only, and the dialog
  opened with the focus on Cancel.)

  The window takes the height its wrapped hints need, in English and in
  German, and cannot be made smaller than that. (The German "Langer
  Artikel" hint lost its first and last lines at the narrowest width,
  and the English one too.)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import i18n  # noqa: E402
from nostr.ui.stash_kind_dialog import StashKind, StashKindDialog  # noqa: E402
from tests.accessibility import unnamed_controls  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def shown():
    dialogs = []

    def show(**kwargs):
        dialog = StashKindDialog(**kwargs)
        dialog.show()
        QApplication.processEvents()
        dialogs.append(dialog)
        return dialog

    yield show
    for dialog in dialogs:
        dialog.close()


def test_the_kinds_are_a_radio_group_the_keyboard_reaches(shown):
    dialog = shown()
    note, article = dialog._note_card.radio, dialog._article_card.radio
    assert note.focusPolicy() & Qt.FocusPolicy.TabFocus
    note.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(note, Qt.Key.Key_Right)          # the cards sit side by side
    QApplication.processEvents()
    assert article.isChecked() and article.hasFocus()
    assert dialog._article_panel.isVisible()
    QTest.keyClick(article, Qt.Key.Key_Left)
    assert note.isChecked() and not dialog._article_panel.isVisible()
    assert unnamed_controls(dialog) == []


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("kind", [StashKind.NOTE, StashKind.ARTICLE])
def test_no_hint_is_cut_off_even_at_the_narrowest(shown, language, kind):
    i18n.install(language)
    try:
        dialog = shown(default=kind)
        before = dialog.size()
        dialog.resize(1, 1)                      # as small as a person can drag it
        QApplication.processEvents()
        assert dialog.size() == before
        for card in (dialog._note_card, dialog._article_card):
            hint = card.hint_label
            assert hint.height() >= hint.heightForWidth(hint.width()), (language, hint.text())
    finally:
        i18n.install(i18n.ENGLISH)


@pytest.mark.parametrize("dark", [True, False])
def test_save_draft_looks_dimmed_while_it_cannot_be_chosen(shown, dark):
    # An article draft needs a title: until then Save draft is dimmed, and
    # it was drawn as a pale accent with white text, which reads as a
    # chosen action rather than an unavailable one.
    from PySide6.QtGui import QColor
    from nostr.ui.appearance import roles
    import theme
    dialog = shown(default=StashKind.ARTICLE, is_dark=dark)
    save = dialog._save_btn
    assert not save.isEnabled()
    # It opens on the title Save waits for: a dimmed Save can't take the
    # focus, and Cancel would then take the default on Windows and Linux
    # (review P1: the test failed there, with Save never the default).
    assert dialog.focusWidget() is dialog._title_edit
    inside = save.grab().toImage().pixelColor(6, save.height() // 2)
    assert inside == QColor(roles(dark).control)
    dialog._title_edit.setText("A title")
    QApplication.processEvents()
    inside = save.grab().toImage().pixelColor(6, save.height() // 2)
    top, bottom = QColor(roles(dark).highlight), QColor(roles(dark).button)
    for channel in ("red", "green", "blue"):
        lo, hi = sorted((getattr(top, channel)(), getattr(bottom, channel)()))
        assert lo <= getattr(inside, channel)() <= hi
    assert theme.contrast_ratio(QColor("#FFFFFF"), inside) >= 4.5
