# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reporting a public work again, quietly, until its publication lists
it (RG-M4-b 3; editorial contract 12.4 ``/published``;
:mod:`nostr.standup.unlisted` keeps the record and the pauses).

A publish run whose report did not arrive hands the signed article over
(:meth:`QuietReport.hand_over`): the work is public, so the run, the
list, the pill and the detail say so ("»Titel« ist öffentlich.
<Publikation> erfährt es gleich."), and this reports it again after a
growing pause. One the server refused because its publication lists
another work at the link name (409 ``address_taken``) is only kept as
that: trying again would meet the same answer.

Nothing is said either way (lessons 36): a report that arrives makes the
work listed, which the rows show. After a restart the record is read
again when the Einreichungen view first opens (never at launch, so
starting the app asks no signer). A signer app that declined is not
asked again in this run; an answer that will not change (the work is
another revision's, the person no longer its writer) ends the record.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject

from ..standup import following as rules
from ..standup import unlisted
from ..standup.errors import PASSING, Code
from .standup_account import live

# What a later try may not meet again: kept, and asked again after the pause.
_AGAIN = frozenset({*PASSING, Code.RATE_LIMITED, Code.SIGNER_UNREACHABLE, Code.UNAVAILABLE,
                    Code.BAD_RESPONSE, Code.AUTH_REQUIRED, Code.AUTH_INVALID,
                    Code.SESSION_EXPIRED})


class QuietReport(QObject):
    """One per account's Following. ``record`` is the account's
    :class:`unlisted.Unlisted`; ``later(ms, call)`` and ``clock`` are seams."""

    def __init__(self, following, account, *, record: unlisted.Unlisted,
                 later: Callable[[int, Callable[[], None]], None],
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._following, self._account, self._record = following, account, record
        self._later, self._clock = later, clock
        self._listed: Dict[str, Callable] = {}     # sid -> the run's answer, in this run
        self._waiting: set = set()                 # a try is due or on its way
        self._resting: set = set()                 # the signer app declined: not this run
        self._resumed = self._stopped = False

    @property
    def record(self) -> unlisted.Unlisted:
        return self._record

    def state(self, sid: str) -> str:
        """:data:`rules.PUBLIC` or :data:`rules.NOT_LISTED` for a work kept
        here, else ``""``."""
        entry = self._record.entry(sid)
        if entry is None:
            return ""
        return rules.NOT_LISTED if entry.get("taken") else rules.PUBLIC

    def states(self) -> Dict[str, str]:
        return {sid: self.state(sid) for sid in self._record.sids()}

    def hand_over(self, sid: str, *, event: dict, naddr: str, revision: int,
                  taken: bool = False, on_listed: Optional[Callable] = None) -> None:
        """The report of ``sid``'s public ``event`` did not arrive (``taken``:
        refused as another work's link name). ``on_listed(view)`` once a
        later report arrived in this run."""
        self._record.keep(sid, unlisted.first(event=event, naddr=naddr, revision=revision,
                                              now=self._clock(), taken=taken))
        if on_listed is not None:
            self._listed[sid] = on_listed
        self._schedule(sid)

    def resume(self) -> None:
        """Once per run, when the view first opens: every work kept from
        before is reported again when due; one tried long enough goes."""
        if self._resumed or self._stopped:
            return
        self._resumed = True
        now = self._clock()
        for sid in self._record.sids():
            entry = self._record.entry(sid)
            if entry is not None and unlisted.over(entry, now):
                self._record.forget(sid)
            else:
                self._schedule(sid)

    def forget(self, sid: str) -> None:
        """The work is listed (or no longer this writer's to report)."""
        self._record.forget(sid)
        self._listed.pop(sid, None)

    def stop(self) -> None:
        self._stopped = True
        self._listed.clear()

    # -- trying --------------------------------------------------------------------------

    def _schedule(self, sid: str) -> None:
        entry = self._record.entry(sid)
        if self._stopped or entry is None or entry.get("taken") or sid in self._waiting \
                or sid in self._resting:
            return
        self._waiting.add(sid)
        wait = max(0.0, float(entry.get("next", 0)) - self._clock())
        self._later(int(wait * 1000), lambda: self._try(sid))

    def _try(self, sid: str) -> None:
        if self._stopped:
            return
        entry = self._record.entry(sid)
        if entry is None or not unlisted.due(entry, self._clock()):
            self._waiting.discard(sid)
            if entry is not None and unlisted.over(entry, self._clock()):
                self.forget(sid)
            return

        def reported(view) -> None:
            self._waiting.discard(sid)
            self._following.serve(view)
            answer = self._listed.get(sid)
            self.forget(sid)
            if answer is not None:
                answer(view)

        def not_reported(error) -> None:
            self._waiting.discard(sid)
            code = getattr(error, "code", "")
            taken = code == Code.CONFLICT and \
                (getattr(error, "detail", {}) or {}).get("reason") == "address_taken"
            if taken or code in _AGAIN:
                self._record.keep(sid, unlisted.after_try(entry, self._clock(), taken=taken))
                self._schedule(sid)
            elif code == Code.SIGNER_DECLINED:
                self._resting.add(sid)
            else:
                self.forget(sid)              # an answer a later try would meet again
            self._following.publishing_changed(sid)

        self._following.api_for(sid).published(
            sid, naddr=entry["naddr"], event=entry["event"], revision=entry["revision"],
            on_success=live(self._account, reported), on_failure=live(self._account, not_reported))


__all__ = ["QuietReport"]
