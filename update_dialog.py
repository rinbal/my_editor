#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Software Update: the in-app side of the install guide.

Renders an update_flow.UpdatePlan as numbered steps, the same steps the web
install guide shows, so updating feels like installing did. Choices follow
the usual macOS order and Apple's Human Interface Guidelines:

- Skip This Version on the left, Later and the default action on the right.
  Closing the window means Later; skipping is only ever an explicit click.
  While the update is being installed or the restart started, Esc and the
  close box do nothing: that work can't be stopped halfway.
- Progress and errors appear inside the step they belong to, not in a second
  window, and every error says what to do next.
- Nothing is downloaded or replaced until the default button is clicked.

For AUTOMATIC plans the dialog drives an updater.UpdateInstaller: download
and check the file, let the window write down its open tabs
(``before_restart``, the last point at which the person can still say no),
prepare the update while the window is still open (macOS and the .deb),
then start the swap and emit ``restart_ready`` so the window can close.
Asking first matters for the .deb: preparing it installs it, and from then
on the restart is the only thing left to do. Every other plan hands off to
a URL (the install guide in update mode, or the release notes).

The release notes are shown in the dialog itself, the way Mac apps present
an update, so nobody has to leave the app to decide.
"""

import html
import re

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import theme
from constants import (
    DARK_BORDER, DARK_FG, DARK_MENU_BG, DARK_MUTED_FG,
    LIGHT_BORDER, LIGHT_FG, LIGHT_MENU_BG, LIGHT_MUTED_FG,
    LIGHT_SELECTION, MONO_FONT,
)
from update_flow import AUTOMATIC, DOWNLOAD, PREPARE, RESTART

# Dialog states.
READY = "ready"
DOWNLOADING = "downloading"
PREPARING = "preparing"
FAILED = "failed"            # trying again can help
GUIDE_ONLY = "guide_only"    # it can't: the update guide is the way on
INSTALLED = "installed"      # the update is in place, but the restart failed
RESTARTING = "restarting"

# Which buttons each state shows. One table, so a state can never leave a
# stray button behind. Preparing offers no Cancel: a half-installed package
# or a half-copied app is worse than waiting a few seconds.
_BUTTONS = {
    READY: ("skip", "later", "primary"),
    DOWNLOADING: ("cancel",),
    PREPARING: (),
    FAILED: ("later", "guide", "retry"),
    GUIDE_ONLY: ("later", "guide"),
    INSTALLED: ("later", "retry"),
    RESTARTING: (),
}
_DEFAULT_BUTTON = {READY: "primary", FAILED: "retry", GUIDE_ONLY: "guide",
                   INSTALLED: "retry"}

# What to do after an error, added to the installer's own sentences. "Try
# again" is offered only where trying again can help.
_NEXT_STEP = {
    True: "Try again, or use the update guide.",
    False: "Use the update guide to install this update.",
}
# The guide would only install the same version a second time.
_INSTALLED_NEXT_STEP = ("The update is installed. Try again, or quit MyEditor "
                        "and open it again.")

_NOTES_MAX_HEIGHT = 180


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
    QLabel#update_notes_label {{ font-weight: 600; }}
    QTextBrowser#update_notes {{
        background: {field}; color: {fg}; border: 1px solid {border};
        border-radius: 6px; padding: 4px 8px; font-size: 12px;
    }}
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
    restart_failed = Signal()      # before_restart ran, but no restart is coming

    def __init__(self, version: str, current_version: str, plan, *,
                 release_url: str, asset=None, installer=None,
                 before_restart=None, release_notes: str = "",
                 is_dark: bool = True, parent=None):
        super().__init__(parent)
        self._version = version
        self._plan = plan
        self._asset = asset
        self._installer = installer
        self._before_restart = before_restart or (lambda: True)
        self._release_notes = _notes_body(release_notes)
        self._state = READY
        # An update that is installed already (the .deb) and only waits for
        # the restart. Trying again then restarts, and nothing more.
        self._installed = None

        self.setWindowTitle("Software Update")
        self.setMinimumWidth(520)
        self.setStyleSheet(_stylesheet(is_dark))

        self._rows = [_StepRow(i + 1, step) for i, step in enumerate(plan.steps)]
        self._roles = {step.role: row for step, row in zip(plan.steps, self._rows) if step.role}
        self._buttons = self._make_buttons()
        self._build_layout(current_version, release_url, theme.dialog_link_color(is_dark))

        if installer is not None:
            installer.progress.connect(self._on_progress)
            installer.ready.connect(self._on_downloaded)
            installer.prepared.connect(self._on_prepared)
            installer.declined.connect(self._on_declined)
            installer.failed.connect(self._on_installer_failed)

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
        if self._release_notes:
            content.addSpacing(6)
            content.addWidget(self._notes_view())
        content.addSpacing(8)
        for row in self._rows:
            content.addWidget(row)
            content.addSpacing(4)
        content.addWidget(self._note)
        if not self._release_notes:
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

    def _notes_view(self) -> QWidget:
        label = QLabel("What\u2019s New")
        label.setObjectName("update_notes_label")
        self.notes_view = QTextBrowser()
        self.notes_view.setObjectName("update_notes")
        self.notes_view.setOpenLinks(False)
        self.notes_view.setMarkdown(self._release_notes)
        _quiet_headings(self.notes_view)
        self.notes_view.setMaximumHeight(_NOTES_MAX_HEIGHT)
        self.notes_view.setAccessibleName("Release notes")
        self.notes_view.anchorClicked.connect(
            lambda url: self.link_activated.emit(QUrl(url).toString()))
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(label)
        layout.addWidget(self.notes_view)
        return box

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
        default = _DEFAULT_BUTTON.get(state)
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
        self._roles[DOWNLOAD].set_state("active")
        self._roles[DOWNLOAD].set_progress(-1)
        self._set_state(DOWNLOADING)
        self._installer.start(self._asset)

    def _retry(self) -> None:
        if self._installed is not None:
            self._restart(self._installed)   # never download or install it twice
        else:
            self._start_download()

    def _cancel_download(self) -> None:
        # The installer discards its partial file and emits nothing more.
        self._installer.cancel()
        self._reset_rows()
        self._set_state(READY)

    def reject(self) -> None:
        # Esc and the close box land here. An install or a restart that is
        # under way can't be stopped halfway, and closing the dialog would
        # destroy the installer that is watching it.
        if self._state in (PREPARING, RESTARTING):
            return
        if self._state == DOWNLOADING:
            self._installer.cancel()
        super().reject()

    def done(self, result: int) -> None:
        if self._installed is not None:
            # Installed but not restarted, and the person moved on: the
            # tabs written down for the restart must not reopen later.
            self._installed = None
            self.restart_failed.emit()
        super().done(result)

    # -- installer signals --------------------------------------------------
    def _on_progress(self, percent: int) -> None:
        if self._state == DOWNLOADING:
            self._roles[DOWNLOAD].set_progress(percent)

    def _on_installer_failed(self, message: str, retryable: bool) -> None:
        if self._state == DOWNLOADING:
            self._fail(DOWNLOAD, message, retryable=retryable)
        elif self._state == PREPARING:
            self.restart_failed.emit()
            self._fail(PREPARE, message, retryable=retryable)

    def _on_downloaded(self, path: str) -> None:
        download = self._roles[DOWNLOAD]
        download.progress.hide()
        download.set_state("done")
        # The window writes its tabs down (and asks about anything it can't
        # keep) before anything is installed, so saying no here really does
        # leave everything as it was.
        if not self._before_restart():
            self._installer.discard_download(path)
            self._reset_rows()
            self._note.setText("Update canceled. Nothing was changed.")
            self._note.show()
            self._set_state(READY)
            return
        prepare = self._roles.get(PREPARE)
        if prepare is not None:
            prepare.set_state("active")
            prepare.set_progress(-1)
        self._set_state(PREPARING)
        # Installs with nothing to prepare answer at once with ``prepared``.
        self._installer.prepare(path)

    def _on_declined(self, message: str) -> None:
        self.restart_failed.emit()
        self._reset_rows()
        self._note.setText(message)
        self._note.show()
        self._set_state(READY)

    def _on_prepared(self, path: str) -> None:
        prepare = self._roles.get(PREPARE)
        if prepare is not None:
            prepare.progress.hide()
            prepare.set_state("done")
        self._restart(path)

    def _restart(self, path: str) -> None:
        restart = self._roles[RESTART]
        restart.set_error("")
        restart.set_state("active")
        self._set_state(RESTARTING)
        installed = self._installer.installs_before_restart
        try:
            self._installer.apply(path)
        except Exception as exc:
            # apply() words its own failures; anything else gets one sentence.
            reason = str(exc).strip() if isinstance(exc, RuntimeError) else ""
            reason = reason or "MyEditor couldn't start the update."
            if installed:
                # The new version is in place: only the restart is left, and
                # the tabs stay written down for it.
                self._installed = path
                self._fail(RESTART, reason, next_step=_INSTALLED_NEXT_STEP,
                           state=INSTALLED)
            else:
                self._installer.discard_prepared(path)
                self.restart_failed.emit()
                self._fail(RESTART, reason)
            return
        self._installed = None
        self.restart_ready.emit()
        self.accept()

    def _fail(self, role: str, message: str, *, retryable: bool = True,
              next_step: str = None, state: str = None) -> None:
        row = self._roles[role]
        row.set_state("error")
        row.set_error(f"{message} {next_step or _NEXT_STEP[retryable]}")
        self._set_state(state or (FAILED if retryable else GUIDE_ONLY))


def _quiet_headings(view: QTextBrowser) -> None:
    """Section headings in release notes at body size, in bold.

    Markdown headings render larger than the dialog's own title otherwise,
    which turns the hierarchy upside down: the window's title names what
    this is, the notes are its content.
    """
    doc = view.document()
    heading = QTextCharFormat()
    heading.setFontWeight(QFont.Weight.DemiBold)
    # Markdown sizes headings by a relative adjustment, which wins over a
    # point size, so it is reset rather than overridden.
    heading.setProperty(QTextFormat.Property.FontSizeAdjustment, 0)
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().headingLevel() > 0:
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(heading)
        block = block.next()


def _notes_body(notes: str) -> str:
    """The release notes without their own top heading, which only repeats
    the version the dialog's title already names."""
    text = (notes or "").strip()
    return re.sub(r"\A#[^#\n][^\n]*\n+", "", text).strip()


class WhatsNewDialog(QDialog):
    """What's New, shown once after an update: the release notes in the app.

    One button, Done, because there is nothing to decide. Links in the
    notes open in the browser through the window (``link_activated``).
    """

    link_activated = Signal(str)

    def __init__(self, version: str, notes: str, *, release_url: str = "",
                 is_dark: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("")
        self.setMinimumWidth(480)
        self.setStyleSheet(_stylesheet(is_dark))

        icon = QLabel()
        pixmap = QApplication.windowIcon().pixmap(64, 64)
        if not pixmap.isNull():
            icon.setPixmap(pixmap)
        icon.setFixedSize(64, 64)

        title = QLabel(f"What’s New in MyEditor {version}")
        title.setObjectName("update_title")
        self.notes_view = QTextBrowser()
        self.notes_view.setObjectName("update_notes")
        self.notes_view.setOpenLinks(False)
        self.notes_view.setMarkdown(_notes_body(notes))
        _quiet_headings(self.notes_view)
        self.notes_view.setMinimumHeight(220)
        self.notes_view.setAccessibleName("Release notes")
        self.notes_view.anchorClicked.connect(
            lambda url: self.link_activated.emit(QUrl(url).toString()))

        content = QVBoxLayout()
        content.setSpacing(10)
        content.addWidget(title)
        content.addWidget(self.notes_view, 1)

        buttons = QHBoxLayout()
        if release_url:
            link_color = theme.dialog_link_color(is_dark)
            more = QLabel(f'<a href="{html.escape(release_url)}" '
                          f'style="color:{link_color}">Full Release Notes</a>')
            more.setTextFormat(Qt.RichText)
            more.setOpenExternalLinks(False)
            more.linkActivated.connect(self.link_activated)
            buttons.addWidget(more)
        buttons.addStretch(1)
        self.done_button = QPushButton("Done")
        self.done_button.setDefault(True)
        self.done_button.clicked.connect(self.accept)
        buttons.addWidget(self.done_button)

        grid = QGridLayout(self)
        grid.setContentsMargins(20, 20, 20, 16)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(18)
        grid.addWidget(icon, 0, 0, Qt.AlignTop)
        grid.addLayout(content, 0, 1)
        grid.addLayout(buttons, 1, 0, 1, 2)
