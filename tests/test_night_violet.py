# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Observable Night Violet scope, lifecycle and contrast regressions."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTabWidget, QWidget

import theme
from nostr.ui import appearance
from nostr.ui.context_widgets import ContextStatusBar, ContextTabBar, owns_editor
from nostr.ui.save_destination_dialog import SaveDestination, SaveDestinationDialog
from tests.widget_lifetime import delete_new_windows


@pytest.fixture(autouse=True)
def cleanup():
    dark = theme.is_dark_active()
    yield from delete_new_windows()
    theme.apply_app_theme(dark)


def pixel(widget, x=6, y=None):
    image = widget.grab().toImage()
    scale = image.devicePixelRatio()
    return image.pixelColor(round(x * scale), round((y or widget.height() // 2) * scale))


def contrast(fg, bg):
    return theme.contrast_ratio(QColor(fg), QColor(bg))


@pytest.mark.parametrize('dark', [False, True])
def test_light_and_dark_composites_are_legible(dark):
    r = appearance.roles(dark, increased_contrast=False, reduced_effects=False)
    for base in (r.panel, r.side, r.list, r.field, r.control, r.popover, r.hover, r.soft):
        assert contrast(r.text, base) >= 4.5
        assert contrast(r.muted, base) >= 4.5
    for fill in (r.highlight, r.button, r.selection, r.pressed):
        assert contrast('#FFFFFF', fill) >= 4.5
        assert contrast(r.on_accent, fill) >= 3
    for fill in (r.orange, r.payment_pressed):
        assert contrast(r.dusk, fill) >= 4.5
    assert contrast(r.icon, r.soft) >= 3
    assert contrast(appearance.roles(True).icon, r.dusk) >= 3
    for base in (r.panel, r.field, r.control):
        assert contrast(r.edge, base) >= 3
        assert contrast(r.focus, base) >= 3
    # Resolve the strongest static corner glow over its actual panel.
    color, bg = QColor(r.glow), QColor(r.panel)
    alpha = r.glow_alpha / 255
    composite = QColor(*(round(a * alpha + b * (1-alpha)) for a,b in zip(
        color.getRgb()[:3], bg.getRgb()[:3])))
    assert contrast(r.text, composite) >= 4.5
    assert contrast(r.muted, composite) >= 4.5
    assert contrast(r.error, composite) >= 4.5
    assert contrast(r.amount, composite) >= 4.5


@pytest.mark.parametrize('dark', [False, True])
def test_destination_switch_restores_local_save_and_preserves_fields(dark):
    theme.apply_app_theme(dark)
    dialog = SaveDestinationDialog(is_dark=dark)
    dialog.show()
    QApplication.processEvents()
    button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
    local = pixel(button)
    local_card = pixel(dialog._local_card)
    dialog._remember_checkbox.setChecked(True)
    QTest.keyClick(dialog, Qt.Key_Down)
    assert dialog.destination is SaveDestination.NOSTR_DRAFT
    assert pixel(button) != local
    assert dialog._remember_checkbox.isChecked()
    QTest.keyClick(dialog, Qt.Key_Up)
    assert dialog.destination is SaveDestination.LOCAL
    assert pixel(button) == local
    assert pixel(dialog._local_card) == local_card
    theme.apply_app_theme(not dark)
    assert dialog.destination is SaveDestination.LOCAL
    assert dialog._remember_checkbox.isChecked()
    assert dialog.palette().window().color() == theme._palette(not dark).window().color()


def test_ownership_moves_with_tabs_and_never_follows_account_or_name():
    tabs = QTabWidget()
    owner = SimpleNamespace(tabs=tabs, _editor_from_widget=lambda w: w)
    # SimpleNamespace cannot be weak-referenced: use a real non-Qt owner.
    class Owner:
        pass
    owner = Owner()
    owner.tabs = tabs
    owner._editor_from_widget = lambda w: w
    bar = ContextTabBar(owner)
    tabs.setTabBar(bar)
    local, draft, remote = QWidget(), QWidget(), QWidget()
    local._file_path = '/example/nostr-draft.md'
    draft._draft_binding = SimpleNamespace(identifier='draft', owner_pubkey='old account')
    remote._save_destination = SaveDestination.NOSTR_DRAFT
    for widget in (local, draft, remote):
        tabs.addTab(widget, 'Identical title')
    assert [bar.is_nostr(i) for i in range(3)] == [False, True, True]
    bar.moveTab(1, 0)
    assert [bar.is_nostr(i) for i in range(3)] == [True, False, True]
    draft._draft_binding = None
    assert not bar.is_nostr(0)
    assert not owns_editor(None)
    assert not owns_editor(SimpleNamespace(_file_path='local.md', _published_article=object()))
    assert owns_editor(SimpleNamespace(_published_article=object()))


def test_status_origin_expires_and_local_replacement_restores_neutral():
    status = ContextStatusBar()
    status.showMessage('Same text', nostr=True)
    assert status._nostr_message
    status.showMessage('Same text')
    assert not status._nostr_message
    status.showMessage('Failed', 10, nostr=True, error=True)
    assert status._message_error
    # Native Windows timers may be coalesced under CI load. Wait for the
    # observable expiry, without assuming a 40 ms scheduling deadline.
    for _ in range(100):
        if not status.currentMessage():
            break
        QTest.qWait(10)
    assert not status.currentMessage()
    assert not status._nostr_message and not status._message_error


def test_alternate_roles_reach_sheet_and_painter_without_recoloring_local(monkeypatch, tmp_path):
    from tests.test_publish_sheets import article
    from tests.test_drafts_panel import healthy_store, shown_panel
    theme.apply_app_theme(False)
    local = QWidget()
    local.resize(100, 100)
    local.show()
    baseline = pixel(local)
    sheet = article(tmp_path, default_title='Keep my unsaved title')
    panel = shown_panel(healthy_store(), is_dark=False)
    sheet.show()
    sheet._summary_edit.setText('Unsaved summary')
    panel._list.setCurrentRow(1)
    old_roles = appearance.roles
    custom = replace(old_roles(False), panel='#D9F2DF', selection='#205A37', highlight='#306642')
    monkeypatch.setattr(appearance, 'roles', lambda dark, **kw: custom if not dark else old_roles(dark, **kw))
    appearance.refresh_windows(False)
    assert sheet._summary_edit.text() == 'Unsaved summary'
    assert sheet.palette().window().color() == QColor(custom.panel)
    assert panel._list.currentRow() == 1
    panel.activateWindow()
    panel._list.setFocus()
    QApplication.processEvents()
    rect = panel._list.visualItemRect(panel._list.item(1))
    fill = pixel(panel._list.viewport(), rect.right() - 30, rect.center().y())
    assert fill.green() > fill.red() and fill.green() > fill.blue()
    assert pixel(local) == baseline
    assert QApplication.palette().window().color() == theme._palette(False).window().color()


def test_accessibility_refresh_keeps_popover_choices_and_deleted_owners_safe(monkeypatch):
    from nostr.ui.imports_actions import OptionsPopover
    from nostr.ui import native_appearance
    popup = OptionsPopover()
    popup.show_choices(copy=True, full=None, has_images=True)
    popup.copy.setFocus()
    monkeypatch.setattr(native_appearance, '_read_preferences', lambda: (True, True))
    QApplication.instance()._night_violet_observer.refresh()
    assert appearance.roles(False).glow_alpha == 0
    assert popup.copy.isChecked()
    assert popup.full.checkState() is Qt.PartiallyChecked
    popup.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    appearance.refresh_windows(False)
    # Leave the application snapshot as it was before this fixture.
    QApplication.instance()._night_violet_preferences = (False, False)


def test_media_scope_can_return_to_local_without_playback_changes(monkeypatch):
    from nostr.ui.media_player import MediaPlayer
    player = MediaPlayer()
    changes = []
    player.player.playbackStateChanged.connect(changes.append)
    state = player.player.playbackState()
    player.set_nostr(True, False)
    assert player.palette().highlight().color() == QColor(appearance.roles(False).selection)
    player.set_nostr(False, False)
    assert player.palette().highlight().color() == theme._palette(False).highlight().color()
    assert player.player.playbackState() == state
    assert changes == []


def test_reused_media_viewer_clears_nostr_decoration(monkeypatch):
    from nostr.ui.media_viewer import MediaViewer
    from nostr.ui.media_source import PreviewResolver
    from tests.test_media_library import media
    monkeypatch.setattr(PreviewResolver, 'resolve', lambda *args, **kw: None)
    viewer = MediaViewer(loader=None, visibility=None, is_dark=False)
    viewer.play(media(1), 'audio/mp3', 'From Nostr', nostr=True)
    assert viewer.property('nightVioletSurface')
    assert viewer._night_violet_glow.isVisible()
    viewer._failed('Could not load the recording')
    assert viewer.message.palette().color(viewer.message.foregroundRole()) == QColor(appearance.roles(False).error)
    viewer.play(media(1), 'audio/mp3', 'Local file')
    assert not viewer.property('nightVioletSurface')
    assert not viewer._night_violet_glow.isVisible()
    assert viewer.palette().window().color() == theme._palette(False).window().color()
    theme.apply_app_theme(True)
    assert not viewer.property('nightVioletSurface')
    assert not viewer._night_violet_glow.isVisible()


@pytest.mark.parametrize('dark', [False, True])
def test_image_review_selection_leaves_image_pixels_unchanged(dark):
    from PySide6.QtGui import QIcon, QImage
    from nostr.ui.image_review_dialog import ImageReviewDialog
    from tests.test_imports_image_review import Images
    theme.apply_app_theme(dark)
    images = Images()
    source = QImage(96, 96, QImage.Format_RGB32)
    source.fill(QColor('#A8C6CA'))
    images.kept = {'https://fixture.example/photo.png': source}
    dialog = ImageReviewDialog(list(images.kept), image_source=images)
    dialog.show()
    QApplication.processEvents()
    icon = dialog._list.item(0).icon()
    assert icon.pixmap(96, 96, QIcon.Selected).toImage().pixelColor(48, 48) == QColor('#A8C6CA')
    assert dialog._list.palette().highlight().color() == QColor(appearance.roles(dark).selection)
