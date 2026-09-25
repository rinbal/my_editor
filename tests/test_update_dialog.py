# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the Software Update dialog's states.

Every state is load-bearing:

  Nothing is downloaded until the default button is clicked, and closing
  the window means Later. Skipping a version is only ever an explicit click.

  A failed download says so inside the Download step and offers both a
  retry and the update guide, never a dead end.

  Cancelling the save prompt leaves nothing behind: the downloaded file is
  deleted and the app keeps running on the old version.

  Only a started swap asks the window to close.

The installer is a fake with the real one's signals, so no network or file
swap happens. No modal loop is entered: each state is reached by calling or
clicking what reaches it.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import updater  # noqa: E402
import update_dialog  # noqa: E402
from update_dialog import DOWNLOADING, FAILED, READY, RESTARTING, UpdateDialog  # noqa: E402
from update_flow import plan_for  # noqa: E402

RELEASE_URL = "https://github.com/rinbal/my_editor/releases/tag/v3.3"
ASSET = SimpleNamespace(name="my-editor-3.3-windows-setup.exe", url="https://github.com/x", size=10)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class FakeInstaller(QObject):
    progress = Signal(int)
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, apply_error=None):
        super().__init__()
        self.started = []
        self.canceled = 0
        self.applied = []
        self._apply_error = apply_error

    def start(self, asset):
        self.started.append(asset)

    def cancel(self):
        self.canceled += 1

    def apply(self, path):
        if self._apply_error:
            raise RuntimeError(self._apply_error)
        self.applied.append(path)


def automatic_dialog(installer, before_restart=lambda: True):
    plan = plan_for(updater.WINDOWS_INSTALLER, "3.3", release_url=RELEASE_URL,
                    asset=ASSET, can_self_update=True, machine="AMD64")
    return UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, asset=ASSET,
                        installer=installer, before_restart=before_restart, is_dark=False)


def guided_dialog():
    plan = plan_for(updater.MACOS_APP, "3.3", release_url=RELEASE_URL, machine="arm64")
    return UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, is_dark=True)


def visible_buttons(dialog):
    return sorted(key for key, button in dialog._buttons.items() if not button.isHidden())


def recorder(signal):
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


def test_it_opens_ready_with_the_three_usual_choices():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    assert dialog.state == READY
    assert visible_buttons(dialog) == ["later", "primary", "skip"]
    assert dialog._buttons["primary"].isDefault()
    assert dialog._buttons["primary"].text() == "Update Now"
    assert installer.started == []
    assert dialog.windowTitle() == "Software Update"


def test_update_now_starts_the_download_in_the_first_step():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    dialog._buttons["primary"].click()
    assert installer.started == [ASSET]
    assert dialog.state == DOWNLOADING
    assert visible_buttons(dialog) == ["cancel"]
    installer.progress.emit(40)
    row = dialog._rows[0]
    assert not row.progress.isHidden() and row.progress.value() == 40
    assert row.badge.property("state") == "active"


def test_cancel_stops_the_download_and_returns_to_ready():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    dialog._buttons["primary"].click()
    dialog._buttons["cancel"].click()
    assert installer.canceled == 1
    assert dialog.state == READY
    assert dialog._rows[0].progress.isHidden()


def test_closing_the_window_while_downloading_cancels_it():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    dialog._buttons["primary"].click()
    dialog.reject()
    assert installer.canceled == 1


def test_a_failed_download_explains_itself_and_offers_a_way_on():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    dialog._buttons["primary"].click()
    installer.failed.emit("Connection refused.")
    assert dialog.state == FAILED
    row = dialog._rows[0]
    assert row.badge.property("state") == "error"
    assert "Connection refused." in row.error.text()
    assert visible_buttons(dialog) == ["guide", "later", "retry"]
    assert dialog._buttons["retry"].isDefault()

    dialog._buttons["retry"].click()
    assert len(installer.started) == 2
    assert dialog.state == DOWNLOADING
    assert row.error.isHidden()


def test_the_guide_button_hands_off_to_the_guide_in_update_mode():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    links = recorder(dialog.link_activated)
    dialog._buttons["primary"].click()
    installer.failed.emit("Timed out.")
    dialog._buttons["guide"].click()
    assert links and "update=3.3" in links[0][0]
    assert dialog.result() == QDialog.Accepted


def test_a_finished_download_saves_work_then_starts_the_swap(tmp_path):
    installer = FakeInstaller()
    asked = []
    dialog = automatic_dialog(installer, before_restart=lambda: asked.append(True) or True)
    restarts = recorder(dialog.restart_ready)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"x")

    dialog._buttons["primary"].click()
    installer.ready.emit(str(path))

    assert asked == [True]
    assert installer.applied == [str(path)]
    assert restarts == [()]
    assert dialog.state == RESTARTING
    assert [row.badge.property("state") for row in dialog._rows] == ["done", "done", "active"]


def test_cancelling_the_save_prompt_deletes_the_download_and_changes_nothing(tmp_path):
    installer = FakeInstaller()
    dialog = automatic_dialog(installer, before_restart=lambda: False)
    restarts = recorder(dialog.restart_ready)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"x")

    dialog._buttons["primary"].click()
    installer.ready.emit(str(path))

    assert not path.exists()
    assert installer.applied == []
    assert restarts == []
    assert dialog.state == READY
    assert "Nothing was changed" in dialog._note.text()


def test_a_swap_that_cannot_start_is_reported_in_the_restart_step(tmp_path):
    installer = FakeInstaller(apply_error="Could not launch the installer.")
    dialog = automatic_dialog(installer)
    restarts = recorder(dialog.restart_ready)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"x")

    dialog._buttons["primary"].click()
    installer.ready.emit(str(path))

    assert dialog.state == FAILED
    assert restarts == []
    assert not path.exists()
    assert "Could not launch the installer." in dialog._rows[2].error.text()


def test_skip_is_explicit_and_says_which_version():
    dialog = automatic_dialog(FakeInstaller())
    skipped = recorder(dialog.skip_requested)
    dialog._buttons["skip"].click()
    assert skipped == [("3.3",)]


def test_later_does_not_skip():
    dialog = automatic_dialog(FakeInstaller())
    skipped = recorder(dialog.skip_requested)
    dialog._buttons["later"].click()
    assert skipped == []


def test_a_guided_plan_opens_the_update_guide():
    dialog = guided_dialog()
    links = recorder(dialog.link_activated)
    assert dialog._buttons["primary"].text() == "Open Update Guide"
    dialog._buttons["primary"].click()
    assert links[0][0].startswith("https://rinbal.github.io/my_editor/install/?os=mac")
    assert dialog.result() == QDialog.Accepted


def test_commands_come_with_a_copy_button(qt_app):
    plan = plan_for(updater.DEB, "3.3", release_url=RELEASE_URL,
                    asset=SimpleNamespace(name="my-editor_3.3_amd64.deb", size=1), machine="x86_64")
    dialog = UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, is_dark=False)
    copy = next(b for b in dialog.findChildren(update_dialog.QPushButton) if b.text() == "Copy")
    copy.click()
    assert qt_app.clipboard().text() == "sudo apt install ./my-editor_3.3_amd64.deb"


def test_the_release_link_is_escaped():
    url = 'https://github.com/x"onmouseover="y'
    plan = plan_for(updater.MACOS_APP, "3.3", release_url=url, machine="arm64")
    dialog = UpdateDialog("3.3", "3.2", plan, release_url=url, is_dark=False)
    labels = [l.text() for l in dialog.findChildren(update_dialog.QLabel) if "Release Notes" in l.text()]
    assert labels and '"onmouseover="' not in labels[0]
