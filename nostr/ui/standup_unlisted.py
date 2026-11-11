# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reporting a public work again, quietly, until its publication lists
it (RG-M4-b 3; editorial contract 12.4 ``/published``;
:mod:`nostr.standup.unlisted` keeps the record and the pauses).

A publish run whose report did not arrive hands the signed article over
(:meth:`QuietReport.hand_over`): the work is public, so the run, the
list, the pill and the detail say so ("„Titel“ ist öffentlich.
<Publikation> erfährt es gleich."), and this reports it again. One the
server refused because its publication lists another work at the link
name (409 ``address_taken``) is only kept as that: trying again would
meet the same answer.

Each report is a signed request. With the key on this computer no one is
asked, so the whole schedule runs: after a growing pause, for seven
days, after a restart from when the Einreichungen view first opens
(never at launch). Through a signer app the calm rule holds (the lead's
fix to the M4 open list): right after the publish, in that run, at most
:data:`QUICK_TRIES` tries with the same pauses; after that a try only
rides along with the next signer action the person takes here anyway
(:class:`RideAlong`), and never more than one such unannounced request
per run, shared with the cleanup of the private copies.

Nothing is said either way (lessons 36): a report that arrives makes the
work listed, which the rows show. The record stays until the View lists
the work, or no longer waits for this report (another state, a newer
version), restart or not; the seven days end only the quiet tries
(RG-M4-c 3). While a quiet try is left the work "learns of it shortly";
once none is, the row says it is public and offers Erneut senden
(:meth:`QuietReport.send_again`, the person's own request). A signer app
that declined is not asked again in this run; an answer that will not
change (the work is another revision's, the person no longer its
writer) ends the record. No try and no ride is spent on a work the View
already lists.

A work the writer published on another device has no record here. When
the server lists another work at its link name (12.4 ``address_taken``),
the detail's read looks at its address on the relays it goes to
(:meth:`QuietReport.look`); a run about to sign reads them anyway. Where
they show this version, credited to the writer, it is public: kept as
the refused report is ("„Titel“ ist öffentlich, aber …", Rückfrage
stellen), and nothing is signed.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject

from .. import bech32
from ..standup import following as rules
from ..standup import formats, publishing, unlisted
from ..standup.errors import PASSING, Code
from .standup_account import live
from .standup_publish_words import LINK_RELAYS

# What a later try may not meet again: kept, and asked again after the pause.
_AGAIN = frozenset({*PASSING, Code.RATE_LIMITED, Code.SIGNER_UNREACHABLE, Code.UNAVAILABLE,
                    Code.BAD_RESPONSE, Code.AUTH_REQUIRED, Code.AUTH_INVALID,
                    Code.SESSION_EXPIRED})
# Through a signer app: the quiet tries right after the publish, in its run.
QUICK_TRIES = 3


class RideAlong:
    """At most one unannounced signer request per run (per account), and
    only right after a signer action the person took: the riders are
    asked in turn, the first that asks something uses it up."""

    def __init__(self) -> None:
        self._riders: List[Callable[[], bool]] = []
        self.used = False

    def join(self, rider: Callable[[], bool]) -> None:
        """``rider()`` asks the signer app once and says True, or says
        False when it has nothing to ask."""
        self._riders.append(rider)

    def person_signed(self) -> None:
        """The person's own signer action went through."""
        if self.used:
            return
        for rider in self._riders:
            if rider():
                self.used = True
                return


class QuietReport(QObject):
    """One per account's Following. ``record`` is the account's
    :class:`unlisted.Unlisted`; ``here()`` says the key is on this
    computer; ``ride_along`` the account's :class:`RideAlong`;
    ``read(unsigned, on_done)`` reads the address of ``unsigned`` on the
    relays it goes to (``standup_publish_job.read_relays``; None: never);
    ``later(ms, call)`` and ``clock`` are seams."""

    def __init__(self, following, account, *, record: unlisted.Unlisted,
                 later: Callable[[int, Callable[[], None]], None],
                 here: Callable[[], bool], ride_along: RideAlong,
                 read: Optional[Callable[[dict, Callable[[dict], None]], None]] = None,
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._following, self._account, self._record = following, account, record
        self._later, self._clock, self._here, self._read = later, clock, here, read
        self._looked: set = set()                  # (sid, revision hash) read on the relays
        self._listed: Dict[str, Callable] = {}     # sid -> the run's answer, in this run
        self._quick: Dict[str, int] = {}           # sid -> quiet tries after its publish
        self._waiting: set = set()                 # a try is due or on its way
        self._resting: set = set()                 # the signer app declined: not this run
        self._resumed = self._stopped = False
        ride_along.join(self._ride)

    @property
    def record(self) -> unlisted.Unlisted:
        return self._record

    def state(self, sid: str) -> str:
        """For a work kept here :data:`rules.NOT_LISTED`, :data:`rules.PUBLIC`
        while a quiet try is left (due or on its way; with the key here,
        before the view first opens), else :data:`rules.UNREPORTED`;
        ``""`` for none."""
        entry = self._record.entry(sid)
        if entry is None:
            return ""
        if entry.get("taken"):
            return rules.NOT_LISTED
        if sid in self._waiting or (self._here() and not self._resumed and not self._stopped
                                    and not unlisted.over(entry, self._clock())):
            return rules.PUBLIC
        return rules.UNREPORTED

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
        self._quick[sid] = 0
        self._schedule(sid)

    def resume(self) -> None:
        """Once per run, when the view first opens: with the key here every
        work kept is reported again when due, for its seven days (through
        a signer app only as a ride-along, or by hand)."""
        if self._resumed or self._stopped:
            return
        self._resumed = True
        for sid in self._record.sids():
            self._schedule(sid)
        for sid in self._record.sids():
            self._following.publishing_changed(sid)

    def send_again(self, sid: str, on_done: Callable[[Optional[object]], None]) -> None:
        """Erneut senden: the report, now, as the person's own request
        (never the run's ride-along). ``on_done(None)`` once it arrived,
        else with why not."""
        entry = self._record.entry(sid)
        if self._stopped or entry is None or entry.get("taken") or sid in self._waiting:
            return
        self._resting.discard(sid)
        self._waiting.add(sid)
        self._following.publishing_changed(sid)
        self._try(sid, by_hand=on_done)

    def look(self, sid: str, version) -> None:
        """The server lists another work at the link name of ``sid``, the
        writer's to publish, opened here as ``version``: unless a record
        says how it stands, its address is read on the relays once per
        version and run (:meth:`found`)."""
        row = self._following.row(sid)
        view = row.view if row is not None else None
        if self._stopped or self._read is None or view is None \
                or row.situation is not rules.Situation.PUBLISH or version is None \
                or version.opened is None or version.revision_hash != view.revision_hash \
                or self._kept(sid, version.revision) or (sid, view.revision_hash) in self._looked:
            return
        self._looked.add((sid, view.revision_hash))
        revision, writer = version.opened.revision, self._account.writer
        try:
            d = str(revision["work"]["d"])
            unsigned = publishing.published_template(
                revision, writer=writer, d=d, published_at=0, created_at=0,
                media_urls=publishing.addresses_before(publishing.media_to_publish(revision), ()))
        except (KeyError, TypeError, publishing.NotPublishable):
            return
        address = f"{formats.ARTICLE_KIND}:{writer}:{d}"
        self._read(unsigned, lambda relays: self.found(sid, version, address=address,
                                                       relays=relays))

    def found(self, sid: str, version, *, address: str, relays) -> bool:
        """Whether the relays' read of ``address`` (12.4) shows ``version``,
        a first publication, public, credited to the writer, while its
        publication lists another work there: then it is kept as that
        (never signed or sent again), and the run, the list and the
        detail say so."""
        if self._stopped or version.opened.verified.correction:
            return False
        shown = unlisted.found_public(version.opened.revision, writer=self._account.writer,
                                      address=address, relays=relays)
        if shown is None:
            return False
        if not self._kept(sid, version.revision):
            event, holding = shown
            naddr = bech32.encode_naddr(formats.tag_value(event, "d"), event["pubkey"],
                                        formats.ARTICLE_KIND, holding[:LINK_RELAYS])
            self.hand_over(sid, event=event, naddr=naddr, revision=version.revision, taken=True)
        run = self._following.publishing.get(sid)
        if run is not None:
            run.kept_public()
        self._following.publishing_changed(sid)
        return True

    def _kept(self, sid: str, revision: int) -> bool:
        """Whether a record says how this revision of ``sid`` stands."""
        entry = self._record.entry(sid)
        return entry is not None and int(entry.get("revision", 0)) == revision

    def forget(self, sid: str) -> None:
        """The work is listed (or no longer this writer's to report)."""
        self._record.forget(sid)
        self._listed.pop(sid, None)

    def _settled(self, sid: str) -> bool:
        """Whether the View no longer waits for this report: it lists the
        work, it is in another state, or a newer version is there. Such a
        record goes, with no request."""
        entry, row = self._record.entry(sid), self._following.row(sid)
        view = row.view if row is not None else None
        if entry is None or view is None:
            return False
        if row.situation in rules.PUBLISHED_HERE and view.revision <= int(entry["revision"]):
            return False
        self.forget(sid)
        return True

    def stop(self) -> None:
        self._stopped = True
        self._listed.clear()

    # -- trying --------------------------------------------------------------------------

    def _may_wait(self, sid: str) -> bool:
        """Whether a try may come on its own, after its pause."""
        return self._here() or self._quick.get(sid, QUICK_TRIES) < QUICK_TRIES

    def _schedule(self, sid: str) -> None:
        entry = self._record.entry(sid)
        if self._stopped or entry is None or entry.get("taken") or sid in self._waiting \
                or sid in self._resting or not self._may_wait(sid) \
                or unlisted.over(entry, self._clock()):
            return
        self._waiting.add(sid)
        wait = max(0.0, float(entry.get("next", 0)) - self._clock())
        self._later(int(wait * 1000), lambda: self._try(sid))

    def _ride(self) -> bool:
        """The ride-along: one kept work reported now, through a signer
        app (with the key here the schedule needs none); one tried long
        enough goes without a request."""
        if self._stopped or self._here():
            return False
        now = self._clock()
        for sid in self._record.sids():
            entry = self._record.entry(sid)
            if entry is not None and not entry.get("taken") and not unlisted.over(entry, now) \
                    and sid not in self._waiting and sid not in self._resting \
                    and not self._settled(sid):
                self._waiting.add(sid)
                self._try(sid, riding=True)
                return True
        return False

    def _try(self, sid: str, *, riding: bool = False,
             by_hand: Optional[Callable[[Optional[object]], None]] = None) -> None:
        if self._stopped:
            return
        entry, now = self._record.entry(sid), self._clock()
        quiet = by_hand is None
        if entry is None or entry.get("taken") or self._settled(sid) or quiet and (
                unlisted.over(entry, now) or not (riding or unlisted.due(entry, now))):
            self._waiting.discard(sid)
            self._following.publishing_changed(sid)
            return
        if quiet and not riding and not self._here():
            self._quick[sid] = self._quick.get(sid, 0) + 1

        def reported(view) -> None:
            self._waiting.discard(sid)
            self._following.serve(view)
            answer = self._listed.get(sid)
            self.forget(sid)
            if answer is not None:
                answer(view)
            if by_hand is not None:
                by_hand(None)

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
            if by_hand is not None:
                by_hand(error)

        self._following.api_for(sid).published(
            sid, naddr=entry["naddr"], event=entry["event"], revision=entry["revision"],
            on_success=live(self._account, reported), on_failure=live(self._account, not_reported))


__all__ = ["QUICK_TRIES", "QuietReport", "RideAlong"]
