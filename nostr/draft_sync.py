# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbound side of NIP-37 drafts: relays → wrap parse → bunker decrypt → store.

``DraftSync`` is the orchestrator the drafts panel binds to. It owns:

  - One ``Subscription`` on the active profile's private relays (the
    relay directory's ``private_relays``, the same set DraftPublishJob
    writes to), filtered to kind-31234 events authored by the profile
    pubkey. When the directory learns a newer relay list for the
    profile, the set is worked out again and the subscription follows.
  - A sequential decryption queue feeding a ``BunkerClient``.
    decrypting one wrap at a time avoids slamming the signer with N
    approval prompts in parallel and matches the bunker's request /
    response cadence.
  - Asking first: a signer app lives on a phone, which is often asleep.
    Before the first draft (after a start, a refresh, a retry, or a
    silence) the signer is asked a question no signer asks a person
    about (``BunkerClient.check_alive``). Nothing comes back: the drafts
    wait, quietly, and it is asked again every minute, so decrypting
    goes on by itself the moment it answers.
  - An auto-refresh ``QTimer`` (5-minute default) that re-issues the
    REQ to pick up drafts produced from other devices, and reads the
    account's draft relay list again, so a list changed on another
    device is followed (nostr/draft_relays.py; an unchanged list costs
    no signer prompt).
  - Moving drafts left behind: when the draft relays changed and a relay
    no longer among them may still hold drafts saved there meanwhile
    (RelayDirectory.drafts_left_behind), each reading ends by moving
    them to where drafts go now (DraftMove), relay by relay, so one that
    is gone does not hold back the others. Each is asked again after
    every reading until its move succeeds.

Cancellation model:
  Each ``start_for`` / ``stop`` cycle increments a monotonic
  ``_generation`` token. Every async callback (relay-list lookup,
  bunker session-pool ready, bunker decrypt response) captures the
  generation in scope and bails out if the orchestrator has since
  moved on. This makes rapid profile switches safe without trying to
  cancel mid-flight RPCs we can't actually recall.

Failures are surfaced on the store as ``DraftState.FAILED`` rows so the
user understands when the signer rejects a draft, rather than seeing a
silent count mismatch. A signer that does not answer is no such failure:
its drafts stay ``LOADING``, waiting for it.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import N_, _, ngettext

from . import failures
from .bunker import (
    BunkerClient, BunkerSessionPool, declined_by_signer, is_signer_silent, signer_failure_words,
)
from .draft_relays import DraftMove, MoveOutcome, PrivateDraftRelays
from .draft_store import DraftState, DraftStore
from .drafts import (
    DRAFT_WRAP_KIND,
    DraftWrapMeta,
    parse_inner_event,
    parse_wrap_event,
    supersedes,
)
from .events import verify_event
from .outbox import RelayDirectory, ask_draft_relays
from .outbox.policy import normalize_relay_url
from .profiles import Profile
from .relay import RelayPool, Subscription


# Default cadence for background refresh. Five minutes balances
# "drafts written on another device show up" against relay chatter.
DEFAULT_REFRESH_INTERVAL_MS: int = 5 * 60 * 1000

# Heuristic signal strings indicating the signer doesn't speak NIP-44
# at all (vs. a per-call permission denial). We latch on these and stop
# spamming the signer with decrypt requests it will never honour.
_BUNKER_UNSUPPORTED_NEEDLES: Tuple[str, ...] = (
    "method not found",
    "unsupported method",
    "unknown method",
    "not implemented",
)

# How often a signer app that did not answer is asked again while drafts
# wait for it: the person opening it on their phone is the cure, and the
# drafts should follow without anyone pressing a button.
SIGNER_RECHECK_MS: int = 60 * 1000


_log = logging.getLogger(__name__)

# What did not happen when the drafts could not be read or opened at all,
# said in place with its cause (lessons 36).
DRAFTS_NOT_READ = N_("Your drafts couldn’t be read")
DRAFTS_NOT_OPENED = N_("Your drafts can’t be opened yet")

class DraftSync(QObject):
    """Drive the draft list for one Nostr profile at a time.

    Signals:
      status_changed(str)      human-readable line for the panel footer.
      bunker_error(str)        terminal: signer rejected NIP-44 entirely;
                               panel should show the unsupported-signer
                               state.
      signer_unreachable(bool) the signer app is not answering, so the
                               drafts not yet open wait for it. Unlike
                               ``bunker_error`` this passes by itself, so
                               it carries a flag rather than a message and
                               is cleared once the signer answers a draft.
    """

    status_changed = Signal(str)
    bunker_error = Signal(str)
    signer_unreachable = Signal(bool)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_directory: RelayDirectory,
        session_pool: BunkerSessionPool,
        store: DraftStore,
        entitled_relays: Optional[Callable[[], Sequence[str]]] = None,
        clock: Callable[[], float] = time.time,
        read_draft_list: Optional[
            Callable[[Profile, Callable[[Optional[List[str]]], None]], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        # Reads the account's own draft relays (NIP-37, kind 10013); a seam
        # for tests.
        self._read_draft_list = read_draft_list or self._read_draft_list_from_relays
        self._draft_list_reader: Optional[PrivateDraftRelays] = None
        self._left_moves: Dict[str, DraftMove] = {}     # relay left behind -> its move
        # For NIP-40: expired wraps are ignored, whichever relay sends them.
        self._clock = clock
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        self._store = store
        # Relays this account has standing on beyond its own list (a
        # membership's). Drafts are written there too, so they are read
        # there too.
        self._entitled_relays = entitled_relays
        relay_directory.changed.connect(self._on_relay_list_changed)

        self._profile: Optional[Profile] = None
        self._subscription: Optional[Subscription] = None
        # Read relays that have not ended their stored drafts yet, and those
        # that asked for a sign-in and did not take it (the relay pool signs
        # in when asked; the signer app may decline).
        self._unanswered: set = set()
        self._asking_sign_in: set = set()
        self._bunker: Optional[BunkerClient] = None
        self._read_relays: List[str] = []

        # Cancellation token. Bumped on every ``stop`` so in-flight
        # callbacks can recognise they're talking to a dead epoch and
        # bail before mutating state for the new profile.
        self._generation: int = 0

        # Decryption queue. ``_pending`` maps identifier → (event_id,
        # ciphertext) of the *most recent* wrap to decrypt for that
        # draft; ``_decrypt_queue`` preserves arrival order. Holding the
        # ciphertext in the dict (rather than on the queue entry) lets a
        # later wrap supersede an earlier queued one without us having
        # to walk the deque.
        self._decrypt_queue: Deque[str] = deque()
        self._decrypt_inflight: Optional[str] = None
        self._pending: Dict[str, Tuple[str, str]] = {}
        self._bunker_unsupported: bool = False

        # A signer that is not answering is a different condition from one
        # that cannot decrypt: it is temporary, and the cure is on the
        # user's phone. While it lasts the drafts wait, nobody is asked for
        # a draft, and the signer is asked whether it is back every minute.
        # ``_signer_answers``: it answered since the last start, refresh,
        # retry or silence, so drafts go straight to it. ``_checking``: a
        # question to it is out, and one at a time is enough.
        self._signer_unreachable: bool = False
        self._signer_answers: bool = False
        self._checking: bool = False
        self._recheck_timer = QTimer(self)
        self._recheck_timer.setSingleShot(True)
        self._recheck_timer.setInterval(SIGNER_RECHECK_MS)
        self._recheck_timer.timeout.connect(self._check_signer)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(DEFAULT_REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self.refresh)

    # -- public API --------------------------------------------------------

    @property
    def active_profile(self) -> Optional[Profile]:
        return self._profile

    def start_for(self, profile: Profile) -> None:
        """Bind to ``profile`` and start streaming + decrypting drafts.

        If the same profile is already active this is a no-op; switching
        profiles cleanly tears down the previous subscription first.
        """
        if (
            self._profile is not None
            and self._profile.user_pubkey == profile.user_pubkey
        ):
            return
        self.stop()
        self._profile = profile
        gen = self._generation  # captured for callbacks below
        self._store.bind_profile(profile.user_pubkey)
        self._store.set_loading(True)
        self.status_changed.emit(_("Looking up your relay list…"))
        # Where this account chose to keep its drafts, if it did: read
        # first, and every draft relay question waits for it, so nothing is
        # saved or looked for elsewhere in the meantime.
        author = profile.user_pubkey.lower()
        directory = self._relay_directory
        directory.expect_draft_relays(author)
        self._read_draft_list(profile, lambda relays: directory.set_draft_relays(author, relays))
        self._ask_relays(lambda relays, g=gen: self._on_relays_ready(g, relays))

    def _read_draft_list_from_relays(
            self, profile: Profile, on_done: Callable[[Optional[List[str]]], None]) -> None:
        # One reader per account, kept until the next account replaces it
        # (it knows the list it decrypted, so reading it again costs no
        # prompt); an older one still answering only updates what it read,
        # by author.
        reader = self._draft_list_reader
        if reader is None or reader.author != profile.user_pubkey.lower():
            if reader is not None:
                reader.deleteLater()
            self._draft_list_reader = PrivateDraftRelays(
                profile=profile, pool=self._relay_pool, session_pool=self._session_pool,
                directory=self._relay_directory, parent=self)
        self._draft_list_reader.read(on_done)

    def _follow_draft_list(self) -> None:
        """Read the account's draft relay list again: drafts follow it if
        another device changed it."""
        author = self._profile.user_pubkey.lower()
        self._read_draft_list(self._profile, lambda relays: self._relay_directory
                              .set_draft_relays(author, relays))

    def stop(self) -> None:
        """Tear down the subscription and invalidate in-flight callbacks.

        Idempotent, so it is safe to call from teardown paths.
        """
        # Bumping the generation token first guarantees any callback
        # that fires after this point sees a stale ``gen`` and exits
        # without touching the next profile's state.
        self._generation += 1
        self._refresh_timer.stop()
        self._recheck_timer.stop()
        for mover in self._left_moves.values():
            mover.stop()
            mover.deleteLater()
        self._left_moves.clear()
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        self._decrypt_queue.clear()
        self._pending.clear()
        self._decrypt_inflight = None
        self._bunker_unsupported = False
        self._signer_unreachable = False
        self._signer_answers = False
        self._checking = False
        self._profile = None
        self._bunker = None
        self._read_relays = []
        self._store.set_loading(False)

    def refresh(self) -> None:
        """Re-subscribe to pull in drafts created on other devices.

        Re-opens the subscription rather than relying on the relay to
        replay backlog through an existing one. The store dedupes by
        ``d`` so we won't get duplicates.
        """
        if self._profile is None:
            return
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        self._store.set_loading(True)
        self.status_changed.emit(_("Refreshing drafts…"))
        # What arrives now is asked for only once the signer app has
        # answered again; drafts that wait for it ask now.
        self._signer_answers = False
        self._follow_draft_list()
        self._open_subscription()
        self.ask_signer_again()

    def ask_signer_again(self) -> None:
        """Drafts waiting for the signer app ask it now, not at the next
        minute: the person may just have opened it (they came back to the
        app, or showed the Drafts panel)."""
        if self._signer_unreachable:
            self._check_signer()

    def refresh_by_hand(self) -> None:
        """Refresh, asked for by the person: a draft relay list the signer
        app did not decrypt is asked about again too."""
        if self._draft_list_reader is not None:
            self._draft_list_reader.ask_again()
        self.refresh()

    def reroute(self) -> None:
        """Work out the private relays again and follow them.

        For when what they depend on changed: a newer relay list (the
        directory says so through ``changed``) or a membership that adds
        or drops a relay. The subscription moves only when the set did.
        """
        if self._profile is None:
            return
        gen = self._generation
        self._ask_relays(lambda relays, g=gen: self._on_rerouted(g, relays))

    def retry_decrypt(self, identifier: str) -> None:
        """Re-queue a previously-failed decryption.

        Typical use: the user dismissed the signer's prompt (Amber's, say)
        for the ``nip44_decrypt`` request. The wrap is still in our store
        with its ciphertext, so retrying is a single fresh bunker
        round-trip with no relay re-fetch needed.

        Safe to call for any state; we only do work if the record
        actually has a ciphertext to decrypt.
        """
        if self._profile is None or self._bunker is None:
            return
        if self._bunker_unsupported:
            return
        record = self._store.get(identifier)
        if record is None or not record.ciphertext:
            return
        # Asked whether it is there first, as after every retry.
        self._signer_answers = False
        # Reset the failure marker so the panel re-renders the row as
        # loading; ``_after_decrypt`` will handle either outcome.
        if record.state is DraftState.FAILED:
            record.state = DraftState.LOADING
            record.failure_reason = ""
            self._store.record_changed.emit(identifier)
        meta = DraftWrapMeta(
            identifier=identifier,
            inner_kind=record.inner_kind,
            event_id=record.event_id,
            pubkey=self._profile.user_pubkey.lower(),
            created_at=record.created_at,
            expiration=record.expiration,
            ciphertext=record.ciphertext,
        )
        self._enqueue_decrypt(meta)
        # While drafts wait, a retry is the person saying the signer app
        # is open: it is asked now, and this draft waits with the others.
        self.ask_signer_again()

    def retry_signer(self) -> None:
        """Try Again: ask the signer app now whether it is there.

        There are two ways the signer can be missing and the caller
        should not have to know which one it is looking at: the handshake
        never completed, which needs a fresh session, or it completed and
        the signer then went quiet, which only needs the question asked
        again (``_check_signer`` does either).
        """
        if self._profile is None or self._bunker_unsupported:
            return
        # The draft relay list too: it may have been left unread while
        # the signer app was away.
        if self._draft_list_reader is not None:
            self._draft_list_reader.ask_again()
        self._check_signer()

    def _requeue_failed_drafts(self) -> None:
        """Re-queue every draft we stopped asking about.

        ``retry_decrypt`` is the single door back into the queue, so a
        draft that failed for its own reasons (a malformed payload, a
        foreign identity) is re-examined here too. That costs one signer
        round-trip and keeps this from needing to know which failures are
        worth retrying.
        """
        for record in list(self._store):
            if record.state is DraftState.FAILED and record.ciphertext:
                self.retry_decrypt(record.identifier)

    # -- internal: cancellation -------------------------------------------

    def _is_current(self, gen: int) -> bool:
        """True if the callback was issued in the current generation."""
        return self._profile is not None and gen == self._generation

    # -- internal: resolve relays, then subscribe -------------------------

    def _ask_relays(self, on_done: Callable[[List[str]], None]) -> None:
        ask_draft_relays(self._relay_directory, self._profile, on_done,
                         entitled=self._entitled_relays, reading=True)

    def _on_relays_ready(self, gen: int, relays: List[str]) -> None:
        if not self._is_current(gen):
            return

        self._read_relays = list(relays)

        self.status_changed.emit(_("Connecting to your signer app…"))
        self._check_signer()

    def _on_relay_list_changed(self, pubkey: str) -> None:
        if self._profile is not None and pubkey == self._profile.user_pubkey.lower():
            self.reroute()

    def _on_rerouted(self, gen: int, relays: List[str]) -> None:
        if not self._is_current(gen) or list(relays) == self._read_relays:
            return
        self._read_relays = list(relays)
        # Before the first subscription opens there is nothing to move;
        # the signer callback opens it on the new set.
        if self._subscription is not None:
            self.refresh()

    def _on_bunker_ready(self, gen: int, client: BunkerClient) -> None:
        if not self._is_current(gen):
            return
        self._checking = False
        self._bunker = client
        # A draft relay list that could not be read while the signer app
        # was away is read now that it answers; one still being read is
        # not asked for a second time.
        reader = self._draft_list_reader
        if reader is not None and reader.last_failure is not None:
            self._follow_draft_list()
        self._open_subscription()
        # A reconnect finds drafts we already gave up on, and the relay
        # will not re-deliver their wraps because the store already holds
        # them at that ``created_at``. Nothing else would ever ask again.
        self._requeue_failed_drafts()
        self._refresh_timer.start()
        # The signer handed over may be one kept from earlier, which says
        # nothing about the phone now: drafts that waited are asked for once
        # it has answered a question (free right after a handshake).
        self._pump_decrypt_queue()
        self.ask_signer_again()

    def _on_bunker_unavailable(self, gen: int, reason: str) -> None:
        if not self._is_current(gen):
            return
        self._checking = False
        silent = is_signer_silent(reason)
        if not silent:
            # Said once, in place, with its cause; the signer's own words
            # only in the log (lessons 36).
            _log.info("signer app unavailable for drafts: %s", (reason or "")[:200])
            self.status_changed.emit(failures.spoken(_(DRAFTS_NOT_OPENED),
                                                     signer_failure_words(reason)))
        # Without a signer we can't decrypt anything, but the drafts are
        # read anyway: the user should see there *are* drafts, just
        # locked. Once: a handshake asked again every minute does not read
        # the relays again every minute.
        if self._subscription is None:
            self._open_subscription()
        # A handshake that went unanswered is the strongest evidence we
        # ever get that nothing is listening: the drafts wait for it.
        if silent:
            self._latch_signer_unreachable()

    def _open_subscription(self) -> None:
        # One subscription at a time: the signer coming back after it was
        # unreachable opens it again, and the old one must not stay open.
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if self._profile is None or not self._read_relays:
            self._store.set_loading(False)
            self.status_changed.emit(failures.spoken(_(DRAFTS_NOT_READ),
                                                     _(failures.CHECK_CONNECTION)))
            return
        filters = [{
            "kinds": [DRAFT_WRAP_KIND],
            "authors": [self._profile.user_pubkey],
        }]
        self._subscription = self._relay_pool.subscribe(self._read_relays, filters)
        self._subscription.event.connect(self._on_wrap_event)
        # Loading is over when every relay has ended one way or another: a
        # relay that cannot be reached must not keep it going for good. The
        # relays left behind are read in the background, as Mail checks an
        # account without holding the window: one that never answers must
        # not keep "Refreshing drafts…" up, and what it sends arrives later.
        left = set(self._relay_directory.drafts_left_behind(self._profile.user_pubkey))
        self._unanswered = {normalize_relay_url(u) or u for u in self._read_relays} - left
        self._asking_sign_in = set()
        for signal in (self._subscription.relay_eose, self._subscription.relay_closed,
                       self._subscription.relay_failed):
            signal.connect(self._on_relay_ended)
        self._subscription.relay_closed.connect(self._on_relay_closed)
        if not self._unanswered:
            self._on_eose()

    def _on_relay_closed(self, url: str, reason: str) -> None:
        if reason.startswith("auth-required:"):
            self._asking_sign_in.add(normalize_relay_url(url) or url)

    def _on_relay_ended(self, url: str, *_reason) -> None:
        self._unanswered.discard(normalize_relay_url(url) or url)
        if not self._unanswered and self._store.is_loading:
            self._on_eose()

    # -- internal: handle inbound wraps -----------------------------------

    def _on_wrap_event(self, event: dict) -> None:
        if self._profile is None:
            return
        meta = parse_wrap_event(event)
        if meta is None:
            return
        if meta.pubkey != self._profile.user_pubkey.lower():
            return  # relay returned an unrelated event; ignore defensively
        # A relay that ignores NIP-40 can still hand out a wrap past its
        # expiration (imported drafts carry one of 90 days): it is gone,
        # and must neither show nor replace what the store holds.
        # A blanked wrap past its expiration still says the draft was
        # deleted, and keeps an older copy out (the store records it).
        if meta.is_expired(self._clock()) and not meta.is_tombstone:
            return
        # Only the account's own signature counts: a relay must not be able
        # to hide a draft behind a forged deletion, or change its text.
        if not verify_event(event):
            return

        existing = self._store.get(meta.identifier)
        if meta.is_tombstone:
            self._store.upsert_skeleton(meta)
            if self._store.get(meta.identifier) is None:
                self._pending.pop(meta.identifier, None)
            return

        # Skip versions the store already has (or that an older relay
        # still hands out) so the same draft is not decrypted again.
        if existing is not None and not supersedes(meta.created_at, meta.event_id,
                                                   existing.created_at, existing.event_id):
            return

        before = self._store.get(meta.identifier)
        self._store.upsert_skeleton(meta)
        after = self._store.get(meta.identifier)
        if after is not None and (before is None or after.event_id == meta.event_id):
            self._enqueue_decrypt(meta)

    def _on_eose(self) -> None:
        self._store.set_loading(False)
        count = len(self._store)
        self.status_changed.emit(
            ngettext("Loaded {n} draft.", "Loaded {n} drafts.", count).format(n=count)
        )
        self._follow_locked_draft_relays()
        self._move_drafts_left_behind()

    def _follow_locked_draft_relays(self) -> None:
        """Drafts saved on relays that show them only after a sign-in that
        did not work (the signer app declined it, or the relay refused it)
        would be read back by no device: when every draft relay is so,
        drafts go to the usual relays until one of them shows drafts again."""
        author = self._profile.user_pubkey.lower()
        chosen = self._relay_directory.draft_relays_of(author) or []
        locked = bool(chosen) and all(
            (normalize_relay_url(url) or url) in self._asking_sign_in for url in chosen)
        self._relay_directory.set_draft_relays_locked(author, locked)
        if locked:
            # One quiet line: saving still works (lessons 36).
            self.status_changed.emit(_(
                "Drafts kept where you chose stay hidden until signing in there works: new "
                "drafts are saved with your posts meanwhile."))

    # -- internal: drafts left on relays no longer used --------------------

    def _move_drafts_left_behind(self) -> None:
        if self._profile is None:
            return
        left = {relay: since for relay, since
                in self._relay_directory.drafts_left_behind(self._profile.user_pubkey).items()
                if relay not in self._left_moves}
        if not left:
            return
        gen = self._generation
        ask_draft_relays(self._relay_directory, self._profile,
                         lambda destination, g=gen: self._start_left_moves(g, left, destination),
                         entitled=self._entitled_relays)

    def _start_left_moves(self, gen: int, left: Dict[str, Optional[int]],
                          destination: List[str]) -> None:
        if not self._is_current(gen):
            return
        for relay, since in left.items():
            if relay in self._left_moves:
                continue
            # Signed events, sent as they are: no signer prompt. A relay
            # that does not answer is asked again after the next reading.
            mover = DraftMove(pool=self._relay_pool, author=self._profile.user_pubkey,
                              source=[relay], destination=destination, must_read=[relay],
                              since=since, clock=self._clock, parent=self)
            self._left_moves[relay] = mover
            mover.finished.connect(
                lambda outcome, g=gen, r=relay: self._left_moved(g, r, outcome))
            mover.start()

    def _left_moved(self, gen: int, relay: str, outcome: MoveOutcome) -> None:
        if not self._is_current(gen):
            return
        mover = self._left_moves.pop(relay, None)
        if mover is not None:
            mover.deleteLater()
        if outcome.ok:
            self._relay_directory.stop_reading_drafts_on(self._profile.user_pubkey, [relay])

    # -- internal: bunker decryption queue --------------------------------

    def _enqueue_decrypt(self, meta: DraftWrapMeta) -> None:
        if self._bunker_unsupported:
            # This signer cannot decrypt at all.
            return
        # Without a signer, or with one that is not answering, the draft
        # waits in the queue like the others and is asked for once it is.
        # Latest-write wins: overwriting ``_pending[id]`` ensures the
        # newest ciphertext is what the pump picks up, even if an older
        # one is currently inflight. When the inflight completes we
        # re-enqueue the identifier (in ``_after_decrypt``) so the newer
        # ciphertext gets its turn, fixing the lost-update race where a
        # wrap arriving during inflight would otherwise be stranded in
        # ``_pending`` forever.
        self._pending[meta.identifier] = (meta.event_id, meta.ciphertext)
        if (
            meta.identifier not in self._decrypt_queue
            and meta.identifier != self._decrypt_inflight
        ):
            self._decrypt_queue.append(meta.identifier)
        self._pump_decrypt_queue()

    def _pump_decrypt_queue(self) -> None:
        if self._decrypt_inflight is not None:
            return
        if self._bunker is None or self._bunker_unsupported:
            return
        if not self._signer_answers:
            # Asked first whether anyone is there: a draft would spend 30
            # seconds finding out, one at a time.
            if self._pending:
                self._check_signer()
            return
        while self._decrypt_queue:
            identifier = self._decrypt_queue.popleft()
            entry = self._pending.pop(identifier, None)
            if entry is None:
                # Tombstoned or otherwise drained, so skip and continue.
                continue
            event_id, ciphertext = entry
            if not ciphertext:
                continue
            self._decrypt_inflight = identifier
            gen = self._generation
            self._bunker.nip44_decrypt_self(
                ciphertext,
                on_success=lambda plaintext, ident=identifier, eid=event_id, g=gen: (
                    self._on_decrypt_success(g, ident, eid, plaintext)
                ),
                on_failure=lambda reason, ident=identifier, eid=event_id, g=gen: (
                    self._on_decrypt_failure(g, ident, eid, reason)
                ),
            )
            return

    def _after_decrypt(self, identifier: str) -> None:
        """Clear inflight bookkeeping and re-pump.

        If a newer wrap arrived while we were waiting on the signer it
        will be sitting in ``_pending[identifier]``, so re-enqueue and the
        next pump round picks it up. Without this, rapid edits to the
        same draft can lose the latest ciphertext.
        """
        self._decrypt_inflight = None
        if identifier in self._pending and identifier not in self._decrypt_queue:
            self._decrypt_queue.append(identifier)
        self._pump_decrypt_queue()

    def _on_decrypt_success(
        self,
        gen: int,
        identifier: str,
        source_event_id: str,
        plaintext: str,
    ) -> None:
        if not self._is_current(gen):
            return
        # Whatever the payload turns out to be, the signer replied, which
        # is the only thing the drafts were waiting for.
        self._clear_signer_unreachable()
        try:
            inner = parse_inner_event(plaintext)
        except ValueError as exc:
            # What was wrong with it is for the log (lessons 36).
            _log.info("draft %s is damaged: %s", identifier[:16], str(exc)[:200])
            self._store.set_failed(identifier, _("This draft is damaged"))
            self._after_decrypt(identifier)
            return

        # Trust boundary: a misbehaving signer could decrypt an inner
        # payload whose declared author differs from our profile. If we
        # promoted such a draft to publish, we'd sign it under our key
        # with a foreign ``pubkey`` field. Reject defensively.
        if self._profile is None:
            self._after_decrypt(identifier)
            return
        declared = str(inner.get("pubkey", "")).lower()
        expected = self._profile.user_pubkey.lower()
        if declared and declared != expected:
            self._store.set_failed(
                identifier,
                _("Saved by another account"),
            )
            self._after_decrypt(identifier)
            return

        self._store.set_decrypted(
            identifier,
            inner=inner,
            source_event_id=source_event_id,
        )
        self._after_decrypt(identifier)

    def _on_decrypt_failure(
        self,
        gen: int,
        identifier: str,
        _source_event_id: str,
        reason: str,
    ) -> None:
        if not self._is_current(gen):
            return
        lowered = (reason or "").lower()
        if any(needle in lowered for needle in _BUNKER_UNSUPPORTED_NEEDLES):
            self._latch_bunker_unsupported()
            return

        if is_signer_silent(reason):
            # Nobody answered, which says nothing about the draft: it waits
            # with the others, first in line (unless a newer version came
            # meanwhile, which wins), until the signer app answers again.
            self._decrypt_inflight = None
            record = self._store.get(identifier)
            if record is not None and record.ciphertext:
                self._pending.setdefault(identifier, (record.event_id, record.ciphertext))
                if identifier not in self._decrypt_queue:
                    self._decrypt_queue.appendleft(identifier)
            self._signer_answers = False
            self._latch_signer_unreachable()
            return

        # A reasoned refusal proves the signer is there, even though this
        # particular draft failed. Its words are the row's, the signer's
        # own only in the log (lessons 36).
        self._clear_signer_unreachable()
        _log.info("draft %s not decrypted: %s", identifier[:16], (reason or "")[:200])
        self._store.set_failed(identifier, _("Your signer app declined to open it")
                               if declined_by_signer(lowered)
                               else _("Your signer app couldn’t open it"))
        self._after_decrypt(identifier)

    # -- internal: is the signer app there? ---------------------------------

    def _check_signer(self) -> None:
        """Ask the signer app whether it is there, one question at a time.

        Without a signer yet (its handshake was not answered) the session
        pool is asked for one, and its handshake is the same question. A
        key kept on this computer is always there. A draft out for
        decryption is a question already.
        """
        if (self._profile is None or self._bunker_unsupported or self._checking
                or self._decrypt_inflight is not None):
            return
        self._recheck_timer.stop()
        self._checking = True
        gen = self._generation
        if self._bunker is None:
            self._session_pool.get(
                self._profile,
                on_ready=lambda client, g=gen: self._on_bunker_ready(g, client),
                on_error=lambda reason, g=gen: self._on_bunker_unavailable(g, reason),
            )
        elif isinstance(self._bunker, BunkerClient):
            self._bunker.check_alive(
                on_answer=lambda g=gen: self._on_signer_answered(g),
                on_silence=lambda _reason, g=gen: self._on_signer_silent(g),
            )
        else:
            self._on_signer_answered(gen)

    def _on_signer_answered(self, gen: int) -> None:
        if not self._is_current(gen):
            return
        self._checking = False
        self._signer_answers = True
        # Drafts that wait stay waiting until the signer answers one of
        # them: an app that answers the question but leaves the draft
        # unapproved on the phone would otherwise bring them back as rows
        # for half a minute, every minute.
        if not self._pending:
            self._clear_signer_unreachable()
        self._pump_decrypt_queue()

    def _on_signer_silent(self, gen: int) -> None:
        if not self._is_current(gen):
            return
        self._checking = False
        self._signer_answers = False
        self._latch_signer_unreachable()

    def _latch_signer_unreachable(self) -> None:
        """Nobody answered: the drafts not open yet wait, and the signer
        app is asked again in a minute."""
        self._recheck_timer.start()
        if self._signer_unreachable:
            return
        self._signer_unreachable = True
        self.signer_unreachable.emit(True)

    def _clear_signer_unreachable(self) -> None:
        """The signer answered: drafts are asked for as they come again."""
        self._recheck_timer.stop()
        if not self._signer_unreachable:
            return
        self._signer_unreachable = False
        self.signer_unreachable.emit(False)

    def _latch_bunker_unsupported(self) -> None:
        """Stop hammering a signer that has no NIP-44 support."""
        self._bunker_unsupported = True
        self.bunker_error.emit(
            _("Your signer app can’t decrypt drafts, so they can’t be shown "
              "for this account.")
        )
        # Mark every loading record so the panel can render the
        # locked / unavailable state per row.
        for record in list(self._store):
            if record.state is DraftState.LOADING:
                self._store.set_failed(
                    record.identifier,
                    _("Your signer app can’t decrypt drafts"),
                )
        self._decrypt_queue.clear()
        self._pending.clear()
        self._decrypt_inflight = None
