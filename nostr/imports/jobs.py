# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Imports as durable jobs: pause, stop, resume, survive a restart.

An import of 48 posts with a phone signer takes a while, and the person
may need to stop it, quit, or lose the connection halfway. So an import
is a job saved in the inbox database (inbox_store.py) before anything
runs, one row per post, and :class:`ImportRunner` works through the
rows one at a time (STANDUP's import-jobs.store.js does the same in the
browser):

1. Before the first row, the catalogue says which posts already exist
   (catalogue.py). When no relay answers, the job pauses with the reason
   instead of guessing.
2. A row whose post exists is ``existing``: nothing is signed.
3. A row with a signed draft kept from an earlier try sends that very
   draft again (DraftPublishJob.send_signed): no signer prompt, and an
   edit made since stays, because the older draft loses on the relays.
4. Any other row runs the import pipeline for its post (pipeline.py:
   full text, images, the draft), which asks once more right before
   signing, and the signed draft is kept before it is sent.
5. Each row is saved as it ends. Pause and Stop take effect between two
   rows; Stop is final, and drafts already made stay.

A job ends ``completed`` when every row is done or existing, ``partial``
when some failed (they can be tried again), ``paused`` or ``stopped``.
After a restart a running job is paused and a stopping one stopped
(:meth:`InboxStore.recover_jobs`); nothing resumes on its own.

Each row keeps its post until it is done, so a job resumes without the
original file or a second read of the feed. One job runs at a time per
account.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import _

from ..rss.parser import FeedItem
from . import snapshots
from .catalogue import Existing
from .constants import BATCH_PACE_MS, BATCH_PACE_THRESHOLD
from .inbox_store import (
    DRAFTED,
    ROW_DONE,
    ROW_EXISTING,
    ROW_FAILED,
    ROW_PENDING,
    InboxStore,
    Job,
    JobRow,
)

# What a row is doing, for the progress line (the window words them).
STAGE_PREPARING = "preparing"
STAGE_READING = "reading"
STAGE_FULL_TEXT = "full_text"
STAGE_IMAGES = "images"
STAGE_SAVING = "saving"

# A post's one sentence when no relay kept its draft (lessons 36).
NO_RELAY = _("Its draft wasn't saved online: check your internet connection and try again.")
# A kept draft this close to its end date is made anew instead of sent:
# relays refuse an expired one, and the post would fail for good (L5).
CHECKPOINT_MARGIN = 24 * 3600


class ImportRunner(QObject):
    """Runs one account's import jobs.

    Signals:
      job_changed(str)       a job's status or one of its rows changed (id)
      job_finished(str)      a job stopped running: paused, stopped,
                             partial or completed (id)
      draft_created(str)     a draft was made and accepted (identifier)
    """

    job_changed = Signal(str)
    job_finished = Signal(str)
    draft_created = Signal(str)

    def __init__(self, *, store: InboxStore, catalogue, profile,
                 item_job_factory: Callable[..., QObject],
                 resend_factory: Callable[..., QObject],
                 pacer: Optional[Callable[[int, Callable[[], None]], None]] = None,
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._catalogue = catalogue
        self._profile = profile
        self._item_job_factory = item_job_factory
        self._resend_factory = resend_factory
        self._pacer = pacer or (lambda ms, fn: QTimer.singleShot(ms, fn))
        self._now = clock
        self._job: Optional[Job] = None
        self._existing = Existing()
        self._current = None          # the item job or resend in flight
        self._row_outcome: Dict[str, object] = {}
        self._generation = 0

    def set_profile(self, profile) -> None:
        """The same account signs another way now: the next posts are
        asked about and signed through ``profile``."""
        self._profile = profile

    # -- reading -------------------------------------------------------------

    @property
    def running(self) -> Optional[Job]:
        """The job running now (also while it pauses or stops)."""
        return self._job

    def busy(self) -> bool:
        return self._job is not None

    # -- creating ------------------------------------------------------------

    def create(self, *, label: str, source_type: str,
               posts: Sequence[Tuple[FeedItem, str]], source_url: str = "",
               options: Optional[dict] = None) -> Job:
        """A paused job for ``posts`` (an item and the key of its source,
        or "" when it has none), saved before it runs."""
        rows = []
        for item, source_key in posts:
            try:
                d_tag = snapshots.identifier_of(item)
            except ValueError:
                continue
            rows.append(JobRow(index=len(rows), d_tag=d_tag,
                               title=item.title or item.link or "",
                               source_key=source_key,
                               snapshot=snapshots.to_snapshot(item)))
        return self._store.create_job(label=label, source_type=source_type, rows=rows,
                                      source_url=source_url, options=options)

    # -- controlling -----------------------------------------------------------

    def run(self, job_id: str) -> bool:
        """Start, resume, or try the failed rows again. False when another
        job is running, or this one is stopped or done."""
        if self._job is not None:
            return False
        job = self._store.job(job_id)
        if job is None or job.status in ("stopped", "completed"):
            return False
        self._generation += 1
        self._job = job
        job.status, job.error = "running", ""
        for row in job.rows:
            if row.status == ROW_FAILED:
                row.status, row.error = ROW_PENDING, ""
                self._store.save_row(job.id, row)
        self._store.save_job(job)
        self.job_changed.emit(job.id)
        generation = self._generation
        waiting = [row.d_tag for row in job.rows if row.status == ROW_PENDING]
        self._catalogue.look_up(
            self._profile, waiting,
            on_ready=lambda found: self._on_catalogue(generation, found),
            on_unavailable=lambda reason: self._on_unavailable(generation, reason))
        return True

    def pause(self) -> None:
        """Pause after the post that is being made."""
        self._ask("pausing")

    def stop(self) -> None:
        """Stop after the post that is being made; drafts made stay."""
        self._ask("stopping")

    def stop_paused(self, job_id: str) -> None:
        """Stop a job that is not running (paused or partial)."""
        job = self._store.job(job_id)
        if job is None or job.finished and job.status != "partial":
            return
        if self._job is not None and self._job.id == job_id:
            self.stop()
            return
        job.status = "stopped"
        self._store.save_job(job)
        self.job_changed.emit(job.id)
        self.job_finished.emit(job.id)

    def halt(self, reason: str = "") -> None:
        """Pause now, for good (the account changed, the app quits): the
        row in flight is left as it is and tried again on resume."""
        job = self._job
        if job is None:
            return
        self._generation += 1
        if self._current is not None:
            cancel = getattr(self._current, "cancel", None)
            if cancel is not None:
                cancel()
            _release(self._current)
            self._current = None
        job.status, job.error = "paused", reason
        self._store.save_job(job)
        self._job = None
        self.job_changed.emit(job.id)
        self.job_finished.emit(job.id)

    def _ask(self, status: str) -> None:
        job = self._job
        if job is None:
            return
        job.status = status
        self._store.save_job(job)
        self.job_changed.emit(job.id)
        if self._current is None:
            self._settle_between_rows()

    # -- running ---------------------------------------------------------------

    def _on_unavailable(self, generation: int, reason: str) -> None:
        if generation != self._generation or self._job is None:
            return
        job = self._job
        job.status, job.error = "paused", reason
        self._store.save_job(job)
        self._job = None
        self.job_changed.emit(job.id)
        self.job_finished.emit(job.id)

    def _on_catalogue(self, generation: int, found: Existing) -> None:
        if generation != self._generation or self._job is None:
            return
        self._existing = found
        self._next_row()

    def _is_imported(self, d_tag: str) -> bool:
        return d_tag in self._existing or self._catalogue.known_locally(d_tag) is not None

    def _next_row(self) -> None:
        job = self._job
        if job is None:
            return
        if job.status in ("pausing", "stopping"):
            self._settle_between_rows()
            return
        while True:
            row = next((r for r in job.rows if r.status == ROW_PENDING), None)
            if row is None:
                self._finish(job)
                return
            if not self._is_imported(row.d_tag):
                break
            # Already there: nothing to sign, no need to pace.
            state = self._existing.states.get(row.d_tag) or self._catalogue.known_locally(
                row.d_tag) or DRAFTED
            self._store.mark_imported(row.d_tag, state)
            row.status, row.stage = ROW_EXISTING, ""
            self._store.save_row(job.id, row)
            self.job_changed.emit(job.id)
        if row.signed_event and _expires_soon(row.signed_event, self._now()):
            row.signed_event = None
            self._store.save_row(job.id, row)
        if row.signed_event:
            self._resend(row)
        else:
            self._make(row)

    def _after_row(self) -> None:
        job = self._job
        if job is None:
            return
        if job.total > BATCH_PACE_THRESHOLD and job.status == "running":
            generation = self._generation
            self._pacer(BATCH_PACE_MS,
                        lambda: generation == self._generation and self._next_row())
        else:
            self._next_row()

    def _end_row(self, row: JobRow, status: str, error: str = "") -> None:
        job = self._job
        row.status, row.error = status, error
        row.stage = ""
        self._current = None
        if job is None:
            return
        self._store.save_row(job.id, row)
        self.job_changed.emit(job.id)
        if status == ROW_DONE:
            self._store.mark_imported(row.d_tag)
            self.draft_created.emit(row.d_tag)
        self._after_row()

    def _set_stage(self, row: JobRow, stage: str) -> None:
        if self._job is None:
            return
        row.stage = stage
        self.job_changed.emit(self._job.id)

    def _feed_url(self, row: JobRow) -> str:
        job = self._job
        if row.source_key:
            source = self._store.source(row.source_key)
            if source is not None:
                return source.url
        return job.source_url if job else ""

    def _options(self, row: JobRow) -> dict:
        """A row's options: its source's defaults (``by_source``), and over
        them what the person chose for this run."""
        chosen = dict(self._job.options) if self._job else {}
        by_source = chosen.pop("by_source", {}) or {}
        options = dict(by_source.get(row.source_key, {}))
        options.update(chosen)
        return options

    def _make(self, row: JobRow) -> None:
        generation = self._generation
        job_id = self._job.id
        item = snapshots.from_snapshot(row.snapshot or {})
        options = self._options(row)
        outcome = {"accepted": None, "existing": False, "failed": ""}
        self._set_stage(row, STAGE_PREPARING)
        job = self._item_job_factory(
            items=[item], feed_url=self._feed_url(row),
            fetch_full_text=bool(options.get("fetch_full_text", True)),
            rehost_images=bool(options.get("rehost_images", True)),
            skip_image_urls=set(options.get("skip_image_urls", ())),
            is_imported=self._is_imported, parent=self)
        self._current = job

        def live() -> bool:
            return generation == self._generation and self._job is not None \
                and self._job.id == job_id

        def keep_signed(_index: int, event: dict) -> None:
            if live():
                row.signed_event = event
                row.stage = STAGE_SAVING
                self._store.save_row(job_id, row)

        def published(_index: int, accepted: int, _total: int) -> None:
            outcome["accepted"] = accepted

        job.item_signed.connect(keep_signed)
        job.item_existing.connect(lambda *_a: outcome.update(existing=True))
        job.item_failed.connect(lambda _i, reason: outcome.update(failed=reason))
        job.item_published.connect(published)
        job.item_resolving_from_nostr.connect(
            lambda *_a: live() and self._set_stage(row, STAGE_READING))
        job.item_extracting.connect(
            lambda *_a: live() and self._set_stage(row, STAGE_FULL_TEXT))
        job.item_mirroring.connect(
            lambda *_a: live() and self._set_stage(row, STAGE_IMAGES))
        job.item_started.connect(
            lambda *_a: live() and self._set_stage(row, STAGE_PREPARING))
        job.completed.connect(lambda *_a: live() and self._settle_made(row, outcome))
        # Let go of it once it is done (deferred: it is still in its own
        # signal then). A 2000-post import used to keep 2000 of them.
        job.completed.connect(lambda *_a: _release(job))
        job.start()

    def _settle_made(self, row: JobRow, outcome: dict) -> None:
        if outcome["existing"]:
            self._store.mark_imported(row.d_tag, self._catalogue.known_locally(row.d_tag)
                                      or DRAFTED)
            self._end_row(row, ROW_EXISTING)
        elif outcome["accepted"]:
            self._end_row(row, ROW_DONE)
        elif row.signed_event:
            self._end_row(row, ROW_FAILED, NO_RELAY)
        else:
            self._end_row(row, ROW_FAILED, str(outcome["failed"] or NO_RELAY))

    def _resend(self, row: JobRow) -> None:
        generation = self._generation
        job_id = self._job.id
        self._set_stage(row, STAGE_SAVING)
        job = self._resend_factory(identifier=row.d_tag, parent=self)
        self._current = job

        def live() -> bool:
            return generation == self._generation and self._job is not None \
                and self._job.id == job_id

        def completed(results) -> None:
            if not live():
                return
            if any(ok for _url, ok, _message in results):
                self._end_row(row, ROW_DONE)
            else:
                self._end_row(row, ROW_FAILED, NO_RELAY)

        job.completed.connect(completed)
        # Its one sentence (DraftPublishJob.words), the signer's own
        # reason only where none was given (lessons 36).
        job.failed.connect(lambda reason: live() and self._end_row(
            row, ROW_FAILED, getattr(job, "words", "") or reason))
        job.completed.connect(lambda *_a: _release(job))
        job.failed.connect(lambda *_a: _release(job))
        try:
            job.send_signed(row.signed_event)
        except ValueError:
            # Not a draft this account signed for this post: sign anew.
            row.signed_event = None
            self._current = None
            self._make(row)

    def _settle_between_rows(self) -> None:
        job = self._job
        if job is None:
            return
        if job.status == "pausing":
            job.status = "paused"
        elif job.status == "stopping":
            job.status = "stopped"
        else:
            return
        self._store.save_job(job)
        self._job = None
        self.job_changed.emit(job.id)
        self.job_finished.emit(job.id)

    def _finish(self, job: Job) -> None:
        failed = job.count(ROW_FAILED)
        job.status = "partial" if failed else "completed"
        self._store.save_job(job)
        self._job = None
        self.job_changed.emit(job.id)
        self.job_finished.emit(job.id)


def _expires_soon(event: dict, now: float) -> bool:
    for tag in event.get("tags", []) if isinstance(event, dict) else []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "expiration":
            try:
                return int(tag[1]) <= now + CHECKPOINT_MARGIN
            except (TypeError, ValueError):
                return False
    return False


def _release(job) -> None:
    """Delete a row's finished job on the next turn of the event loop."""
    delete = getattr(job, "deleteLater", None)
    if delete is not None:
        delete()


def unfinished(jobs: List[Job]) -> List[Job]:
    """Jobs that still have rows to do: paused, running, or partial."""
    return [job for job in jobs if job.status in ("paused", "running", "pausing",
                                                  "stopping", "partial")]
