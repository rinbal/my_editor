# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How an import is going, in one place: the Imports window's toolbar.

While an import has posts to do, the toolbar shows a button with a ring
that fills as drafts are made, and how far it is ("31 of 48"); two bars
when it is paused; a red dot when posts failed. The button opens the
import's card:

    Inbox
    [=============           ]  31 of 48
    29 created, 2 were already there
    Copying images...
                              [Stop...]  [Pause]

Pause and Resume take effect between two posts; Stop asks first (drafts
already made stay). An import with failed posts offers Try Again, and
the card names what failed and why. Everything a person sees about an
import is in words (Creating drafts, Paused, Failed), never only in a
colour. The Drafts panel shows the same line (activity_line).
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import theme
from i18n import _, ngettext
import theme
from . import appearance

from ..imports.inbox_store import ROW_DONE, ROW_EXISTING, ROW_FAILED, Job
from ..imports.jobs import (
    STAGE_FULL_TEXT,
    STAGE_IMAGES,
    STAGE_PREPARING,
    STAGE_READING,
    STAGE_SAVING,
)
from .imports_glyphs import glyph, is_dark

RUNNING = ("running", "pausing", "stopping")

_STAGES = {
    STAGE_PREPARING: _("Preparing “{title}”…"),
    STAGE_READING: _("Reading “{title}”…"),
    STAGE_FULL_TEXT: _("Fetching the full article of “{title}”…"),
    STAGE_IMAGES: _("Copying the images of “{title}”…"),
    STAGE_SAVING: _("Saving “{title}”…"),
}


def done_count(job: Job) -> int:
    return job.count(ROW_DONE, ROW_EXISTING)


def progress_text(job: Job) -> str:
    """"31 of 48"."""
    return _("{done} of {total}").format(done=done_count(job), total=job.total)


def status_text(job: Job) -> str:
    """What the import is doing, in words."""
    failed = job.count(ROW_FAILED)
    if job.status == "running":
        return _("Creating drafts")
    if job.status == "pausing":
        return _("Pausing after this post")
    if job.status == "stopping":
        return _("Stopping after this post")
    if job.status == "paused":
        return _("Import paused")
    if failed:
        return ngettext("{count} post couldn't be imported",
                        "{count} posts couldn't be imported", failed).format(count=failed)
    return _("Import finished")


def counts_text(job: Job) -> str:
    """"29 created, 2 were already there, 1 failed"."""
    created, existing, failed = job.count(ROW_DONE), job.count(ROW_EXISTING), job.count(
        ROW_FAILED)
    parts = [ngettext("{count} created", "{count} created", created).format(count=created)]
    if existing:
        parts.append(ngettext("{count} was already there", "{count} were already there",
                              existing).format(count=existing))
    if failed:
        parts.append(ngettext("{count} failed", "{count} failed", failed).format(count=failed))
    return ", ".join(parts)


def with_its_word(text: str) -> str:
    """An ellipsis after a space (German writes "wird vorbereitet \u2026")
    stays with its word where a line wraps, instead of starting a line of
    its own."""
    return text.replace(" \u2026", "\u00a0\u2026")


def stage_text(job: Job) -> str:
    """The post being made and what is being done to it."""
    if job.status not in RUNNING:
        return ""
    row = next((r for r in job.rows if r.stage), None)
    if row is None:
        return ""
    return with_its_word(_STAGES.get(row.stage, _STAGES[STAGE_PREPARING]).format(
        title=row.title or _("Untitled")))


def finished_text(job: Job) -> str:
    """The one sentence said when an import went through all its posts."""
    created, existing, failed = job.count(ROW_DONE), job.count(ROW_EXISTING), job.count(
        ROW_FAILED)
    parts = [_("Import finished."),
             ngettext("{count} draft created.", "{count} drafts created.", created).format(
                 count=created)]
    if existing:
        parts.append(ngettext("{count} was already there.", "{count} were already there.",
                              existing).format(count=existing))
    if failed:
        parts.append(ngettext("{count} couldn't be imported.", "{count} couldn't be imported.",
                              failed).format(count=failed))
    return " ".join(parts)


def activity_line(job: Optional[Job]) -> str:
    """One line for the Drafts panel: "Creating drafts, 31 of 48"."""
    if job is None:
        return ""
    if job.status in RUNNING or job.status == "paused":
        return _("{status}, {progress}").format(status=status_text(job),
                                                progress=progress_text(job))
    return status_text(job)


def ring_icon(job: Optional[Job], palette: QPalette, size: int = 18,
              dpr: float = 2.0) -> QIcon:
    """A ring filled as far as the import is, two bars when paused, a dot
    when posts failed."""
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    if job is None:
        return QIcon(pixmap)
    text = palette.color(QPalette.ColorRole.WindowText)
    if job.status == "paused":
        return QIcon(glyph("pause", size, text, dpr))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(2, 2, size - 4, size - 4)
    track = QColor(text)
    track.setAlpha(60)
    painter.setPen(QPen(track, 2))
    painter.drawEllipse(rect)
    share = done_count(job) / job.total if job.total else 0
    painter.setPen(QPen(palette.color(QPalette.ColorRole.Accent), 2,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawArc(rect, 90 * 16, -int(360 * 16 * share))
    if job.count(ROW_FAILED):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.attention_color(is_dark(palette))))
        painter.drawEllipse(QRectF(size - 7, 0, 7, 7))
    painter.end()
    return QIcon(pixmap)


class ActivityButton(QToolButton):
    """The toolbar's import progress (see the module docstring)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("imports_activity")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(18, 18))
        self.setAccessibleName(_("Import Progress"))
        self.hide()

    def show_job(self, job: Optional[Job]) -> None:
        self.setVisible(job is not None)
        if job is None:
            return
        failed = job.count(ROW_FAILED)
        if job.status in RUNNING or job.status == "paused":
            short = progress_text(job)
        else:
            short = ngettext("{count} failed", "{count} failed", failed).format(count=failed)
        self.setText(short)
        self.setIcon(ring_icon(job, self.palette(), dpr=self.devicePixelRatioF()))
        spoken = _("Import progress: {status}, {progress}").format(
            status=status_text(job), progress=progress_text(job))
        self.setAccessibleName(spoken)
        self.setToolTip(spoken)


class JobCard(QFrame):
    """The import's card, a popover under the activity button.

    Signals:
      pause()          Pause
      resume(str)      Resume, or Try Again (the import's id)
      stop(str)        Stop (the import's id); the window asks first
    """

    pause = Signal()
    resume = Signal(str)
    stop = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("imports_job_card")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName(_("Import"))
        self._job: Optional[Job] = None
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("imports_job_title")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        column.addWidget(self.title)
        self.status = _line("imports_job_status")
        column.addWidget(self.status)
        row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName(_("Import Progress"))
        row.addWidget(self.bar, 1)
        self.progress = _line("imports_job_progress")
        row.addWidget(self.progress)
        column.addLayout(row)
        self.counts = _line("imports_job_counts")
        column.addWidget(self.counts)
        self.stage = _line("imports_job_stage")
        column.addWidget(self.stage)
        self.problem = _line("imports_job_problem")
        column.addWidget(self.problem)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.stop_button = QPushButton(_("Stop…"))
        self.stop_button.setObjectName("imports_job_stop")
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(self._stop)
        buttons.addWidget(self.stop_button)
        self.go_button = QPushButton(_("Pause"))
        self.go_button.setObjectName("imports_job_go")
        self.go_button.setAutoDefault(False)
        self.go_button.clicked.connect(self._go)
        buttons.addWidget(self.go_button)
        column.addLayout(buttons)
        self.setMinimumWidth(320)
        self.setMaximumWidth(420)
        self.apply_theme(theme.is_dark_active())
        appearance.bind(self)

    def apply_theme(self, dark: bool) -> None:
        appearance.popover(self, dark)

    def show_job(self, job: Optional[Job]) -> None:
        self._job = job
        if job is None:
            self.hide()
            return
        self.title.setText(job.label or _("Import"))
        self.status.setText(status_text(job))
        self.bar.setRange(0, max(1, job.total))
        self.bar.setValue(done_count(job))
        self.progress.setText(progress_text(job))
        self.counts.setText(counts_text(job))
        stage = stage_text(job)
        self.stage.setText(stage)
        self.stage.setVisible(bool(stage))
        self.problem.setText(self._problem(job))
        self.problem.setVisible(bool(self.problem.text()))
        running = job.status in RUNNING
        if running:
            self.go_button.setText(_("Pause"))
            self.go_button.setEnabled(job.status == "running")
        elif job.count(ROW_FAILED) and job.status != "paused":
            self.go_button.setText(_("Try Again"))
            self.go_button.setEnabled(True)
        else:
            self.go_button.setText(_("Resume"))
            self.go_button.setEnabled(True)
        self.stop_button.setEnabled(job.status != "stopping")

    @staticmethod
    def _problem(job: Job) -> str:
        """Why it paused, and what failed (the first three)."""
        lines: List[str] = []
        if job.error:
            lines.append(job.error)
        failed = [row for row in job.rows if row.status == ROW_FAILED]
        for row in failed[:3]:
            lines.append(_("“{title}”: {reason}").format(
                title=row.title or _("Untitled"), reason=row.error or _("Unknown problem")))
        if len(failed) > 3:
            more = len(failed) - 3
            lines.append(ngettext("and {count} more", "and {count} more", more).format(
                count=more))
        return "\n".join(lines)

    def _go(self) -> None:
        job = self._job
        if job is None:
            return
        if job.status in RUNNING:
            self.pause.emit()
        else:
            self.resume.emit(job.id)

    def _stop(self) -> None:
        job = self._job
        if job is None:
            return
        self.hide()
        self.stop.emit(job.id)

    def open_below(self, anchor: QWidget) -> None:
        self.adjustSize()
        point = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 4))
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            point.setX(max(screen.left(), min(point.x(), screen.right() - self.width())))
        self.move(point)
        self.show()
        self.go_button.setFocus(Qt.FocusReason.PopupFocusReason)


def _line(name: str) -> QLabel:
    label = QLabel()
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label
