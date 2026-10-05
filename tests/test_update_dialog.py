# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the Software Update dialog's states.

Every state is load-bearing:

  Nothing is downloaded until the default button is clicked, and closing
  the window means Later. Skipping a version is only ever an explicit click.

  A failed download says so inside the Download step and offers both a
  retry and the update guide, never a dead end. Every error reads as the
  installer's own sentences plus one next step, and Try Again is offered
  only where trying again can help.

  The window writes its tabs down (and may ask about unsaved work) before
  anything is installed, so cancelling there leaves nothing behind: the
  downloaded file is deleted and the app keeps running on the old version.

  Installs that prepare the update while the window is open (macOS, .deb)
  report failures inside the Install step, and a closed password prompt is
  not an error. While that step or the restart runs, nothing can interrupt
  it: no Cancel, and Esc and the close box do nothing.

  Only a started swap asks the window to close; whenever no restart is
  coming after the tabs were written down, the window is told, so they are
  not reopened later. An installed .deb that could not restart is tried
  again without downloading or installing it a second time.

  The release notes are shown in the dialog, without their own heading.

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

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import updater  # noqa: E402
import update_dialog  # noqa: E402
from update_dialog import (  # noqa: E402
    DOWNLOADING, FAILED, GUIDE_ONLY, INSTALLED, PREPARING, READY, RESTARTING,
    UpdateDialog, WhatsNewDialog,
)
from update_flow import DOWNLOAD, PREPARE, RESTART, plan_for  # noqa: E402

RELEASE_URL = "https://github.com/rinbal/my_editor/releases/tag/v3.3"
ASSET = SimpleNamespace(name="my-editor-3.3-windows-setup.exe", url="https://github.com/x", size=10)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class FakeInstaller(QObject):
    progress = Signal(int)
    ready = Signal(str)
    prepared = Signal(str)
    declined = Signal(str)
    failed = Signal(str, bool)

    def __init__(self, apply_error=None, prepare_at_once=True, installs=False):
        super().__init__()
        self.started = []
        self.canceled = 0
        self.applied = []
        self.preparing = []
        self.discarded = []
        self.events = []   # what happened, in order
        self.installs_before_restart = installs
        self.apply_error = apply_error
        self._prepare_at_once = prepare_at_once

    def start(self, asset):
        self.started.append(asset)

    def cancel(self):
        self.canceled += 1

    def prepare(self, path):
        self.events.append("prepare")
        self.preparing.append(path)
        if self._prepare_at_once:
            self.prepared.emit(path)

    def discard_download(self, path):
        self.discarded.append(path)
        if os.path.exists(path):
            os.remove(path)

    def discard_prepared(self, path):
        self.discarded.append(path)
        if os.path.exists(path):
            os.remove(path)

    def apply(self, path):
        self.events.append("apply")
        if self.apply_error:
            raise RuntimeError(self.apply_error)
        self.applied.append(path)


def automatic_dialog(installer, before_restart=lambda: True):
    plan = plan_for(updater.WINDOWS_INSTALLER, "3.3", release_url=RELEASE_URL,
                    asset=ASSET, can_self_update=True, machine="AMD64")
    return UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, asset=ASSET,
                        installer=installer, before_restart=before_restart, is_dark=False)


def mac_dialog(installer, before_restart=lambda: True):
    plan = plan_for(updater.MACOS_APP, "3.3", release_url=RELEASE_URL,
                    asset=ASSET, can_self_update=True, machine="arm64")
    return UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, asset=ASSET,
                        installer=installer, before_restart=before_restart, is_dark=False)


def deb_dialog(installer, before_restart=lambda: True):
    plan = plan_for(updater.DEB, "3.3", release_url=RELEASE_URL, asset=ASSET,
                    can_self_update=True, machine="x86_64")
    return UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, asset=ASSET,
                        installer=installer, before_restart=before_restart, is_dark=False)


def capturing(installer, answer=True):
    """A before_restart that logs itself into the installer's event list."""
    def before_restart():
        installer.events.append("capture")
        return answer
    return before_restart


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
    assert dialog._buttons["primary"].text() == "Install Update"
    assert installer.started == []
    assert dialog.windowTitle() == "Software Update"


def test_install_update_starts_the_download_in_the_first_step():
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
    installer.failed.emit("The download didn't finish (Connection refused).", True)
    assert dialog.state == FAILED
    row = dialog._rows[0]
    assert row.badge.property("state") == "error"
    # The installer's sentence as it is, then the one next step.
    assert row.error.text() == ("The download didn't finish (Connection refused). "
                                "Try again, or use the update guide.")
    assert visible_buttons(dialog) == ["guide", "later", "retry"]
    assert dialog._buttons["retry"].isDefault()

    dialog._buttons["retry"].click()
    assert len(installer.started) == 2
    assert dialog.state == DOWNLOADING
    assert row.error.isHidden()


def test_a_damaged_download_reads_as_one_message():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    dialog._buttons["primary"].click()
    installer.failed.emit("The download was damaged or changed on the way, "
                          "so it wasn't installed.", True)
    assert dialog._rows[0].error.text() == (
        "The download was damaged or changed on the way, so it wasn't installed. "
        "Try again, or use the update guide.")


def test_a_failure_that_trying_again_cannot_fix_offers_only_the_guide(tmp_path):
    installer = FakeInstaller(prepare_at_once=False)
    dialog = mac_dialog(installer)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.dmg"))
    installer.failed.emit("The new version didn't pass the macOS integrity check, "
                          "so it wasn't installed.", False)
    assert dialog.state == GUIDE_ONLY
    assert visible_buttons(dialog) == ["guide", "later"]
    assert dialog._buttons["guide"].isDefault()
    text = dialog._roles[PREPARE].error.text()
    assert text.endswith("so it wasn't installed. Use the update guide to install this update.")
    assert "Try again" not in text


def test_a_refused_permission_names_the_guide_once(tmp_path):
    installer = FakeInstaller(prepare_at_once=False)
    plan = plan_for(updater.DEB, "3.3", release_url=RELEASE_URL, asset=ASSET,
                    can_self_update=True, machine="x86_64")
    dialog = UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, asset=ASSET,
                          installer=installer, is_dark=False)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.deb"))
    installer.failed.emit("MyEditor didn't get permission to install the update.", True)
    text = dialog._roles[PREPARE].error.text()
    assert text == ("MyEditor didn't get permission to install the update. "
                    "Try again, or use the update guide.")
    assert text.count("guide") == 1


def test_the_guide_button_hands_off_to_the_guide_in_update_mode():
    installer = FakeInstaller()
    dialog = automatic_dialog(installer)
    links = recorder(dialog.link_activated)
    dialog._buttons["primary"].click()
    installer.failed.emit("The download didn't finish (Timed out).", True)
    dialog._buttons["guide"].click()
    assert links and "update=3.3" in links[0][0]
    assert dialog.result() == QDialog.Accepted


def test_a_finished_download_keeps_the_workspace_then_starts_the_swap(tmp_path):
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
    assert [row.badge.property("state") for row in dialog._rows] == ["done", "active"]


def test_cancelling_before_the_restart_deletes_the_download_and_changes_nothing(tmp_path):
    installer = FakeInstaller()
    dialog = automatic_dialog(installer, before_restart=lambda: False)
    restarts = recorder(dialog.restart_ready)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"x")

    dialog._buttons["primary"].click()
    installer.ready.emit(str(path))

    assert not path.exists()
    assert installer.preparing == [] and installer.applied == []
    assert restarts == []
    assert dialog.state == READY
    assert "Nothing was changed" in dialog._note.text()


def test_a_swap_that_cannot_start_is_reported_in_the_restart_step(tmp_path):
    installer = FakeInstaller(apply_error="MyEditor couldn't open the installer.")
    dialog = automatic_dialog(installer)
    restarts = recorder(dialog.restart_ready)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"x")

    dialog._buttons["primary"].click()
    installer.ready.emit(str(path))

    assert dialog.state == FAILED
    assert restarts == []
    assert not path.exists()
    assert dialog._roles[RESTART].error.text() == (
        "MyEditor couldn't open the installer. Try again, or use the update guide.")


def test_an_unexpected_error_while_starting_the_swap_is_one_plain_sentence(tmp_path):
    installer = FakeInstaller()
    installer.apply = lambda path: {}["APPIMAGE"]   # a KeyError, not a worded failure
    dialog = automatic_dialog(installer)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "setup.exe"))
    assert dialog._roles[RESTART].error.text() == (
        "MyEditor couldn't start the update. Try again, or use the update guide.")


def test_a_swap_that_cannot_start_tells_the_window(tmp_path):
    installer = FakeInstaller(apply_error="MyEditor couldn't start the update helper.")
    dialog = automatic_dialog(installer)
    failures = recorder(dialog.restart_failed)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"x")
    dialog._buttons["primary"].click()
    installer.ready.emit(str(path))
    assert failures == [()]


def test_a_mac_update_writes_the_tabs_down_then_prepares_before_anything_closes(tmp_path):
    installer = FakeInstaller(prepare_at_once=False)
    dialog = mac_dialog(installer, before_restart=capturing(installer))
    restarts = recorder(dialog.restart_ready)
    assert [s.role for s in dialog._plan.steps] == [DOWNLOAD, PREPARE, RESTART]

    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.dmg"))
    assert dialog.state == PREPARING
    assert visible_buttons(dialog) == []          # nothing half-installed to cancel
    assert dialog._roles[PREPARE].badge.property("state") == "active"
    assert installer.events == ["capture", "prepare"] and restarts == []

    staged = str(tmp_path / ".MyEditor.app.update")
    installer.prepared.emit(staged)
    assert installer.events == ["capture", "prepare", "apply"]
    assert installer.applied == [staged]
    assert restarts == [()]


def test_a_failed_preparation_is_reported_in_the_install_step(tmp_path):
    installer = FakeInstaller(prepare_at_once=False)
    dialog = mac_dialog(installer)
    failures = recorder(dialog.restart_failed)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.dmg"))
    installer.failed.emit("The downloaded disk image couldn't be opened.", True)
    assert dialog.state == FAILED
    row = dialog._roles[PREPARE]
    assert row.badge.property("state") == "error"
    assert "couldn't be opened" in row.error.text()
    assert "didn't finish" not in row.error.text()   # not worded as a download error
    assert visible_buttons(dialog) == ["guide", "later", "retry"]
    assert failures == [()]   # the tabs written down for the restart are let go


def test_a_closed_password_prompt_is_not_an_error(tmp_path):
    installer = FakeInstaller(prepare_at_once=False, installs=True)
    dialog = deb_dialog(installer)
    failures = recorder(dialog.restart_failed)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.deb"))
    installer.declined.emit("The update wasn't installed because the password prompt "
                            "was closed. Nothing was changed.")
    assert dialog.state == READY
    assert "Nothing was changed" in dialog._note.text()
    assert all(row.badge.property("state") == "pending" for row in dialog._rows)
    assert failures == [()]


def test_saying_no_to_the_save_question_installs_nothing(tmp_path):
    # The .deb is installed by the Install step, so the question has to come
    # before it: afterwards "Nothing was changed" would no longer be true.
    installer = FakeInstaller(prepare_at_once=False, installs=True)
    dialog = deb_dialog(installer, before_restart=capturing(installer, answer=False))
    package = tmp_path / "x.deb"
    package.write_bytes(b"x")
    dialog._buttons["primary"].click()
    installer.ready.emit(str(package))
    assert installer.events == ["capture"]
    assert installer.preparing == [] and installer.applied == []
    assert not package.exists()
    assert dialog.state == READY
    assert dialog._note.text() == "Update canceled. Nothing was changed."


def test_esc_and_the_close_box_cannot_interrupt_an_install(tmp_path):
    installer = FakeInstaller(prepare_at_once=False, installs=True)
    dialog = deb_dialog(installer)
    closed = recorder(dialog.finished)
    dialog.show()
    try:
        dialog._buttons["primary"].click()
        installer.ready.emit(str(tmp_path / "x.deb"))
        assert dialog.state == PREPARING

        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        dialog.close()
        dialog.reject()
        assert dialog.isVisible() and dialog.state == PREPARING
        assert closed == [] and installer.canceled == 0

        # The step still finishes and the restart goes ahead.
        installer.prepared.emit("/opt/my-editor/my-editor")
        assert installer.applied == ["/opt/my-editor/my-editor"]
    finally:
        dialog.hide()


def test_esc_still_means_later_before_anything_started():
    dialog = automatic_dialog(FakeInstaller())
    dialog.show()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.Rejected


def test_an_installed_package_that_cannot_restart_only_retries_the_restart(tmp_path):
    installer = FakeInstaller(prepare_at_once=False, installs=True,
                              apply_error="MyEditor couldn't start the update helper.")
    dialog = deb_dialog(installer, before_restart=capturing(installer))
    failures = recorder(dialog.restart_failed)
    restarts = recorder(dialog.restart_ready)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.deb"))
    installer.prepared.emit("/opt/my-editor/my-editor")

    assert dialog.state == INSTALLED
    assert visible_buttons(dialog) == ["later", "retry"]   # the guide would install it again
    assert dialog._roles[RESTART].error.text() == (
        "MyEditor couldn't start the update helper. The update is installed. "
        "Try again, or quit MyEditor and open it again.")
    assert failures == []   # the tabs stay written down for the restart

    installer.apply_error = None
    dialog._buttons["retry"].click()
    assert installer.events == ["capture", "prepare", "apply", "apply"]
    assert len(installer.started) == 1 and len(installer.preparing) == 1
    assert installer.applied == ["/opt/my-editor/my-editor"]
    assert restarts == [()] and failures == []


def test_leaving_an_installed_package_unrestarted_lets_the_tabs_go(tmp_path):
    installer = FakeInstaller(prepare_at_once=False, installs=True,
                              apply_error="MyEditor couldn't start the update helper.")
    dialog = deb_dialog(installer)
    failures = recorder(dialog.restart_failed)
    dialog._buttons["primary"].click()
    installer.ready.emit(str(tmp_path / "x.deb"))
    installer.prepared.emit("/opt/my-editor/my-editor")
    dialog._buttons["later"].click()
    assert failures == [()]
    assert installer.discarded == []   # an installed package is never "undone"


NOTES = "# MyEditor v3.3\n\n## Highlights\n\n- **A universal importer** - more sources.\n"


def test_the_release_notes_are_shown_in_the_dialog_without_their_heading():
    plan = plan_for(updater.WINDOWS_INSTALLER, "3.3", release_url=RELEASE_URL,
                    asset=ASSET, can_self_update=True, machine="AMD64")
    dialog = UpdateDialog("3.3", "3.2", plan, release_url=RELEASE_URL, asset=ASSET,
                          installer=FakeInstaller(), release_notes=NOTES, is_dark=False)
    text = dialog.notes_view.toPlainText()
    assert "A universal importer" in text
    assert "MyEditor v3.3" not in text
    links = [l for l in dialog.findChildren(update_dialog.QLabel) if "Release Notes" in l.text()]
    assert links == []   # the notes are right there; no link out needed


def test_whats_new_shows_the_notes_with_one_done_button():
    dialog = WhatsNewDialog("3.4", NOTES, release_url=RELEASE_URL, is_dark=True)
    assert "A universal importer" in dialog.notes_view.toPlainText()
    assert "MyEditor v3.3" not in dialog.notes_view.toPlainText()
    assert dialog.done_button.isDefault()
    buttons = [b.text() for b in dialog.findChildren(update_dialog.QPushButton)]
    assert buttons == ["Done"]


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
