#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Software Update: the in-app side of the install guide.

Renders an update_flow.UpdatePlan as numbered steps, the same steps the web
install guide shows, so updating feels like installing did. Choices follow
the usual macOS order and Apple's Human Interface Guidelines:

- Skip This Version on the left, Later and the default action on the right.
  Closing the window means Later; skipping is only ever an explicit click.
- Progress and errors appear inside the step they belong to, not in a second
  window, and every error says what to do next.
- Nothing is downloaded or replaced until the default button is clicked.

For AUTOMATIC plans the dialog drives an updater.UpdateInstaller: download,
let the window save unsaved work (``before_restart``), then start the swap
and emit ``restart_ready`` so the window can close. Every other plan hands
off to a URL (the install guide in update mode, or the release notes).
"""

import html
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import theme
from constants import (
    DARK_BORDER, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG,
    LIGHT_BORDER, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG,
    LIGHT_SELECTION, MONO_FONT,
)
from update_flow import AUTOMATIC

# Dialog states.
READY = "ready"
DOWNLOADING = "downloading"
FAILED = "failed"
RESTARTING = "restarting"

# Which buttons each state shows. One table, so a state can never leave a
# stray button behind.
_BUTTONS = {
    READY: ("skip", "later", "primary"),
    DOWNLOADING: ("cancel",),
    FAILED: ("later", "guide", "retry"),
    RESTARTING: (),
}

# Step indexes in an AUTOMATIC plan (see update_flow._automatic_plan).
_DOWNLOAD, _SAVE, _RESTART = 0, 1, 2


def _stylesheet(is_dark: bool) -> str:
    """The shared dialog look (theme.dialog_stylesheet) plus the step rows."""
    if is_dark:
        muted, border, field, fg = DARK_MUTED_FG, DARK_BORDER, DARK_MENU_BG, DARK_FG
        accent, ok, err = "#007ACC", "#43A047", "#E57373"
    else:
        muted, border, field, fg = LIGHT_MUTED_FG, LIGHT_BORDER, LIGHT_MENU_BG, LIGHT_FG
        accent, ok, err = LIGHT_SELECTION, "#2E7D32", "#C62828"
    return theme.dialog_stylesheet(is_dark) + f"""
    QLabel#update_title {{ font-size: 16px; font-weight: 600; }}
    QLabel#update_subtitle, QLabel#update_step_detail, QLabel#update_note {{ color: {muted}; }}
    QLabel#update_step_title {{ font-weight: 600; }}
    QLabel#update_step_error {{ color: {err}; }}
    QLabel#update_step_badge {{
        border: 1px solid {border}; border-radius: 11px;
        color: {muted}; font-size: 11px; font-weight: 600;
    }}
    QLabel#update_step_badge[state="active"] {{ background: {accent}; border-color: {accent}; color: #FFFFFF; }}
    QLabel#update_step_badge[state="done"] {{ border-color: {ok}; color: {ok}; }}
    QLabel#update_step_badge[state="error"] {{ border-color: {err}; color: {err}; }}
    QLineEdit#update_command {{
        background: {field}; color: {fg}; border: 1px solid {border};
        border-radius: 4px; padding: 4px 6px; font-family: "{MONO_FONT}"; font-size: 12px;
    }}
    QProgressBar {{
        background: {field}; border: 1px solid {border}; border-radius: 3px;
        max-height: 6px; text-align: center;
    }}
    QProgressBar::chunk {{ background: {accent}; border-radius: 2px; }}
    """


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class _StepRow(QWidget):
    """One numbered step: badge, title, detail, and room for progress or an error."""

    def __init__(self, number: int, step, parent=None):
        super().__init__(parent)
        self._number = number

        self.badge = QLabel(str(number))
        self.badge.setObjectName("update_step_badge")
        self.badge.setFixedSize(22, 22)
        self.badge.setAlignment(Qt.AlignCenter)

        title = QLabel(step.title)
        title.setObjectName("update_step_title")
        self.detail = QLabel(step.detail)
        self.detail.setObjectName("update_step_detail")
        self.detail.setWordWrap(True)

        text = QVBoxLayout()
        text.setContentsMargins(0, 1, 0, 0)
        text.setSpacing(3)
        text.addWidget(title)
        text.addWidget(self.detail)

        if step.command:
            text.addLayout(self._command_row(step.command))

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        text.addWidget(self.progress)

        self.error = QLabel()
        self.error.setObjectName("update_step_error")
        self.error.setWordWrap(True)
        self.error.hide()
        text.addWidget(self.error)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.badge, 0, Qt.AlignTop)
        row.addLayout(text, 1)
        self.set_state("pending")

    def _command_row(self, command: str) -> QHBoxLayout:
        field = QLineEdit(command)
        field.setObjectName("update_command")
        field.setReadOnly(True)
        copy = QPushButton("Copy")
        copy.setAutoDefault(False)

        def on_copy():
            QApplication.clipboard().setText(command)
            copy.setText("Copied")
            QTimer.singleShot(1500, lambda: copy.setText("Copy"))

        copy.clicked.connect(on_copy)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(field, 1)
        row.addWidget(copy)
        return row

    def set_state(self, state: str) -> None:
        """pending, active, done or error."""
        self.badge.setProperty("state", state)
        self.badge.setText({"done": "✓", "error": "!"}.get(state, str(self._number)))
        _repolish(self.badge)

    def set_progress(self, percent: int) -> None:
        """Show the bar; a negative percent means the total is not known yet."""
        if percent < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        self.progress.show()

    def set_error(self, message: str) -> None:
        self.progress.hide()
        self.error.setText(message)
        self.error.setVisible(bool(message))

    def reset(self) -> None:
        self.progress.hide()
        self.set_error("")
        self.set_state("pending")


class UpdateDialog(QDialog):
    """Shows an update plan and carries it out."""

    skip_requested = Signal(str)   # the version the person chose to skip
    link_activated = Signal(str)   # a URL for the window to open in the browser
    restart_ready = Signal()       # the swap is running; the window should close

    def __init__(self, version: str, current_version: str, plan, *,
                 release_url: str, asset=None, installer=None,
                 before_restart=None, is_dark: bool = True, parent=None):
        super().__init__(parent)
        self._version = version
        self._plan = plan
        self._asset = asset
        self._installer = installer
        self._before_restart = before_restart or (lambda: True)
        self._state = READY

        self.setWindowTitle("Software Update")
        self.setMinimumWidth(520)
        self.setStyleSheet(_stylesheet(is_dark))

        self._rows = [_StepRow(i + 1, step) for i, step in enumerate(plan.steps)]
        self._buttons = self._make_buttons()
        self._build_layout(current_version, release_url, theme.dialog_link_color(is_dark))

        if installer is not None:
            installer.progress.connect(self._on_progress)
            installer.ready.connect(self._on_downloaded)
            installer.failed.connect(self._on_download_failed)

        self._set_state(READY)

    # -- layout -------------------------------------------------------------
    def _make_buttons(self) -> dict:
        buttons = {
            "skip": QPushButton("Skip This Version"),
            "later": QPushButton("Later"),
            "primary": QPushButton(self._plan.primary_label),
            "cancel": QPushButton("Cancel"),
            "guide": QPushButton("Open Update Guide"),
            "retry": QPushButton("Try Again"),
        }
        buttons["skip"].clicked.connect(self._skip)
        buttons["later"].clicked.connect(self.reject)
        buttons["primary"].clicked.connect(self._on_primary)
        buttons["cancel"].clicked.connect(self._cancel_download)
        buttons["guide"].clicked.connect(self._open_guide)
        buttons["retry"].clicked.connect(self._retry)
        return buttons

    def _build_layout(self, current_version: str, release_url: str, link_color: str) -> None:
        icon = QLabel()
        pixmap = QApplication.windowIcon().pixmap(64, 64)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        icon.setFixedSize(64, 64)

        title = QLabel(f"MyEditor {self._version} is available")
        title.setObjectName("update_title")
        subtitle = QLabel(f"You have version {current_version}.")
        subtitle.setObjectName("update_subtitle")
        intro = QLabel(self._plan.intro)
        intro.setWordWrap(True)

        self._note = QLabel()
        self._note.setObjectName("update_note")
        self._note.setWordWrap(True)
        self._note.hide()

        notes_link = QLabel(f'<a href="{html.escape(release_url)}" '
                            f'style="color:{link_color}">Release Notes</a>')
        notes_link.setTextFormat(Qt.RichText)
        notes_link.setOpenExternalLinks(False)
        notes_link.linkActivated.connect(self.link_activated)

        content = QVBoxLayout()
        content.setSpacing(6)
        content.addWidget(title)
        content.addWidget(subtitle)
        content.addSpacing(6)
        content.addWidget(intro)
        content.addSpacing(8)
        for row in self._rows:
            content.addWidget(row)
            content.addSpacing(4)
        content.addWidget(self._note)
        content.addWidget(notes_link)

        buttons = QHBoxLayout()
        buttons.addWidget(self._buttons["skip"])
        buttons.addStretch(1)
        for key in ("later", "cancel", "guide", "retry", "primary"):
            buttons.addWidget(self._buttons[key])

        grid = QGridLayout(self)
        grid.setContentsMargins(20, 20, 20, 16)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(18)
        grid.addWidget(icon, 0, 0, Qt.AlignTop)
        grid.addLayout(content, 0, 1)
        grid.addLayout(buttons, 1, 0, 1, 2)

    # -- state --------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        self._state = state
        visible = _BUTTONS[state]
        for key, button in self._buttons.items():
            button.setVisible(key in visible)
            button.setDefault(False)
            button.setAutoDefault(False)
        default = {READY: "primary", FAILED: "retry"}.get(state)
        if default:
            self._buttons[default].setDefault(True)

    def _reset_rows(self) -> None:
        for row in self._rows:
            row.reset()

    # -- actions ------------------------------------------------------------
    def _on_primary(self) -> None:
        if self._plan.mode == AUTOMATIC and self._installer is not None:
            self._start_download()
        else:
            self.link_activated.emit(self._plan.guide_url)
            self.accept()

    def _skip(self) -> None:
        self.skip_requested.emit(self._version)
        self.reject()

    def _open_guide(self) -> None:
        self.link_activated.emit(self._plan.guide_url)
        self.accept()

    def _start_download(self) -> None:
        self._note.hide()
        self._reset_rows()
        self._rows[_DOWNLOAD].set_state("active")
        self._rows[_DOWNLOAD].set_progress(-1)
        self._set_state(DOWNLOADING)
        self._installer.start(self._asset)

    def _retry(self) -> None:
        self._start_download()

    def _cancel_download(self) -> None:
        # The installer discards its partial file and emits nothing more.
        self._installer.cancel()
        self._reset_rows()
        self._set_state(READY)

    def reject(self) -> None:
        if self._state == DOWNLOADING:
            self._installer.cancel()
        super().reject()

    # -- installer signals --------------------------------------------------
    def _on_progress(self, percent: int) -> None:
        if self._state == DOWNLOADING:
            self._rows[_DOWNLOAD].set_progress(percent)

    def _on_download_failed(self, message: str) -> None:
        self._fail(_DOWNLOAD, f"The download didn't finish. {message} "
                              "Try again, or use the update guide.")

    def _on_downloaded(self, path: str) -> None:
        self._rows[_DOWNLOAD].progress.hide()
        self._rows[_DOWNLOAD].set_state("done")
        self._rows[_SAVE].set_state("active")
        if not self._before_restart():
            _discard(path)
            self._reset_rows()
            self._note.setText("Update canceled. Nothing was changed.")
            self._note.show()
            self._set_state(READY)
            return
        self._rows[_SAVE].set_state("done")
        self._rows[_RESTART].set_state("active")
        self._set_state(RESTARTING)
        try:
            self._installer.apply(path)
        except Exception as exc:
            _discard(path)
            self._fail(_RESTART, f"MyEditor couldn't start the update. {exc} "
                                 "Try again, or use the update guide.")
            return
        self.restart_ready.emit()
        self.accept()

    def _fail(self, index: int, message: str) -> None:
        self._rows[index].set_state("error")
        self._rows[index].set_error(message)
        self._set_state(FAILED)


def _discard(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
