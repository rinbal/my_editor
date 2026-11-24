# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish an explicitly reviewed pair in dependency order.

The UI supplies frozen reviewed payloads, never live editor callbacks. The
target factory receives the exact signed reference event so its source pin
cannot accidentally refer to another revision. Every job uses the durable
journal: restarting this action leaves a successful source untouched.
"""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import QObject, Signal

from .publisher import PublishJob
from .publication_journal import PublicationJournal
from .publish_pipeline import UNCHECKED


class PairPublication(QObject):
    status_changed = Signal(str)
    signed = Signal(str)
    completed = Signal(list)
    failed = Signal(str)
    version_published = Signal(dict)

    def __init__(self, *, source_event: dict, target_factory, relay_pool,
                 relay_directory, session_pool, profile, entitled_relays=(),
                 journal=None, source_expected_event_id=UNCHECKED,
                 target_expected_event_id=UNCHECKED, parent=None):
        super().__init__(parent)
        if source_event.get("pubkey") != profile.user_pubkey:
            raise ValueError("the reviewed source belongs to another account")
        self._source = deepcopy(source_event)
        self._target_factory = target_factory
        self._journal = journal if journal is not None else PublicationJournal()
        self._options = dict(relay_pool=relay_pool, relay_directory=relay_directory,
                             session_pool=session_pool, profile=profile,
                             entitled_relays=entitled_relays, journal=self._journal)
        self._job = None
        self._cancelled = False
        self._running = False
        self._signed_event = None
        self._failure_words = ""
        self._source_expected = source_expected_event_id
        self._target_expected = target_expected_event_id

    @property
    def signed_event(self):
        return self._signed_event

    @property
    def failure_words(self):
        return self._failure_words

    @property
    def unsigned_event(self):
        return self._job.unsigned_event if self._job is not None else self._source

    def start(self):
        if self._running or self._cancelled:
            return
        self._running = True
        self._start_version(self._source, source=True)

    def send_again(self):
        if self._running or self._cancelled:
            return False
        self.start()
        return True

    def cancel(self):
        self._cancelled = True
        if self._job is not None:
            self._job.cancel()

    def _start_version(self, unsigned, *, source):
        if self._cancelled:
            return
        if unsigned.get("pubkey") != self._source.get("pubkey"):
            self._fail("the pair must use the reviewed signing account")
            return
        job = PublishJob(unsigned_event=deepcopy(unsigned), parent=self,
                         expected_event_id=self._source_expected if source else self._target_expected,
                         **self._options)
        self._job = job
        job.status_changed.connect(self.status_changed.emit)
        if not source:
            job.signed.connect(self.signed.emit)
        job.failed.connect(lambda reason: self._fail(reason, job.failure_words))
        job.completed.connect(lambda results: self._version_done(job, source, results))
        job.start()

    def _fail(self, reason, words=""):
        self._running = False
        self._failure_words = words
        if not self._cancelled:
            self.failed.emit(reason)

    def _version_done(self, job, source, results):
        if self._cancelled:
            return
        if not any(ok for _url, ok, _message in results):
            self._running = False
            self.completed.emit(results)
            return
        event = deepcopy(job.signed_event)
        self.version_published.emit(event)
        if source:
            try:
                target = self._target_factory(deepcopy(event))
            except (ValueError, TypeError) as exc:
                self._fail(str(exc))
                return
            self._start_version(target, source=False)
        else:
            self._signed_event = event
            self._running = False
            self.completed.emit(results)
