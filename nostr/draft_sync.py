# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbound side of NIP-37 drafts: relays → wrap parse → bunker decrypt → store.

``DraftSync`` is the orchestrator the drafts panel binds to. It owns:

  - One ``Subscription`` on the active profile's read relays, filtered to
    kind-31234 events authored by the profile pubkey.
  - A sequential decryption queue feeding a ``BunkerClient``.
    decrypting one wrap at a time avoids slamming the signer with N
    approval prompts in parallel and matches the bunker's request /
    response cadence.
  - An auto-refresh ``QTimer`` (5-minute default) that re-issues the
    REQ to pick up drafts produced from other devices.

Cancellation model:
  Each ``start_for`` / ``stop`` cycle increments a monotonic
  ``_generation`` token. Every async callback (relay-list lookup,
  bunker session-pool ready, bunker decrypt response) captures the
  generation in scope and bails out if the orchestrator has since
  moved on. This makes rapid profile switches safe without trying to
  cancel mid-flight RPCs we can't actually recall.

Failures are surfaced on the store as ``DraftState.FAILED`` rows so the
user understands when the signer rejects a draft, rather than seeing a
silent count mismatch.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from . import DEFAULT_RELAYS
from .bunker import BunkerClient, BunkerSessionPool, is_signer_silent
from .draft_store import DraftState, DraftStore
from .drafts import (
    DRAFT_WRAP_KIND,
    DraftWrapMeta,
    parse_inner_event,
    parse_wrap_event,
)
from .outbox import RelayList, RelayListCache
from .profiles import Profile
from .relay import RelayPool, Subscription


# Default cadence for background refresh. Five minutes balances
# "drafts written on another device show up" against relay chatter.
DEFAULT_REFRESH_INTERVAL_MS: int = 5 * 60 * 1000

# Cap on the number of read relays we'll subscribe to per profile.
# Mirrors ``outbox.RELAY_CAP`` to keep WebSocket fan-out predictable.
READ_RELAY_CAP: int = 10

# Heuristic signal strings indicating the signer doesn't speak NIP-44
# at all (vs. a per-call permission denial). We latch on these and stop
# spamming the signer with decrypt requests it will never honour.
_BUNKER_UNSUPPORTED_NEEDLES: Tuple[str, ...] = (
    "method not found",
    "unsupported method",
    "unknown method",
    "not implemented",
)

# A silent signer (see ``bunker.is_signer_silent``) is a recoverable
# condition whose cure is on the user's phone, so it is latched
# separately from an incapable one and can be cleared.
#
# What a row says once we have stopped asking on its behalf.
_SIGNER_SILENT_REASON = "your signer did not answer"

# How many drafts in a row may time out before we stop asking. The queue
# is serialized, so one timeout can be a single dropped reply but two in
# a row means nothing is listening, and every further attempt costs the
# user another 30 seconds of a progress line that cannot advance.
_SILENCE_LIMIT: int = 2


class DraftSync(QObject):
    """Drive the draft list for one Nostr profile at a time.

    Signals:
      status_changed(str)      human-readable line for the panel footer.
      bunker_error(str)        terminal: signer rejected NIP-44 entirely;
                               panel should show the unsupported-signer
                               state.
      signer_unreachable(bool) the signer is not answering at all. Unlike
                               ``bunker_error`` this is recoverable, so it
                               carries a flag rather than a message and is
                               cleared once the signer replies again.
    """

    status_changed = Signal(str)
    bunker_error = Signal(str)
    signer_unreachable = Signal(bool)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        store: DraftStore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._store = store

        self._profile: Optional[Profile] = None
        self._subscription: Optional[Subscription] = None
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
        # user's phone. We latch it so the queue stops rather than serving
        # every remaining draft its own 30-second timeout, which for a
        # 54-draft library is close to half an hour of a progress line
        # that will never advance.
        self._signer_unreachable: bool = False
        self._consecutive_silences: int = 0

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
        self.status_changed.emit("Looking up your relay list…")

        # Always include bunker relays as a seed. They're the most
        # likely place the user's recent activity shows up even before
        # the NIP-65 list lands.
        seed_relays = list(dict.fromkeys(profile.bunker_relays))
        self._relay_list_cache.fetch(
            profile.user_pubkey,
            relays=seed_relays,
            on_done=lambda rl, g=gen: self._on_relay_list_ready(g, rl),
        )

    def stop(self) -> None:
        """Tear down the subscription and invalidate in-flight callbacks.

        Idempotent, so it is safe to call from teardown paths.
        """
        # Bumping the generation token first guarantees any callback
        # that fires after this point sees a stale ``gen`` and exits
        # without touching the next profile's state.
        self._generation += 1
        self._refresh_timer.stop()
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        self._decrypt_queue.clear()
        self._pending.clear()
        self._decrypt_inflight = None
        self._bunker_unsupported = False
        self._signer_unreachable = False
        self._consecutive_silences = 0
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
        self.status_changed.emit("Refreshing drafts…")
        self._open_subscription()

    def retry_decrypt(self, identifier: str) -> None:
        """Re-queue a previously-failed decryption.

        Typical use: the signer (e.g. Amber) didn't approve the first
        ``nip44_decrypt`` request in time, or the user dismissed the
        prompt. The wrap is still in our store with its ciphertext, so
        retrying is a single fresh bunker round-trip with no relay
        re-fetch needed.

        Safe to call for any state; we only do work if the record
        actually has a ciphertext to decrypt.
        """
        if self._profile is None or self._bunker is None:
            return
        if self._bunker_unsupported:
            return
        # Retrying one row is the user telling us the signer is awake, so
        # the whole queue gets to run again, not just this draft.
        self._clear_signer_unreachable()
        record = self._store.get(identifier)
        if record is None or not record.ciphertext:
            return
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

    def retry_signer(self) -> None:
        """Ask again after the user tells us their signer is awake.

        There are two ways the signer can be missing and the caller
        should not have to know which one it is looking at: the handshake
        never completed, which needs a fresh session, or it completed and
        the signer then went quiet, which only needs the queue restarted.
        """
        if self._profile is None or self._bunker_unsupported:
            return
        self._clear_signer_unreachable()
        if self._bunker is None:
            gen = self._generation
            self.status_changed.emit("Connecting to your signer…")
            self._session_pool.get(
                self._profile,
                on_ready=lambda client, g=gen: self._on_bunker_ready(g, client),
                on_error=lambda reason, g=gen: self._on_bunker_unavailable(g, reason),
            )
            return
        self._requeue_failed_drafts()

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

    # -- internal: resolve relay list, then subscribe ---------------------

    def _on_relay_list_ready(self, gen: int, relay_list: RelayList) -> None:
        if not self._is_current(gen):
            return

        self._read_relays = _select_read_relays(
            relay_list,
            bunker_relays=self._profile.bunker_relays if self._profile else (),
        )

        self.status_changed.emit("Connecting to your signer…")
        self._session_pool.get(
            self._profile,
            on_ready=lambda client, g=gen: self._on_bunker_ready(g, client),
            on_error=lambda reason, g=gen: self._on_bunker_unavailable(g, reason),
        )

    def _on_bunker_ready(self, gen: int, client: BunkerClient) -> None:
        if not self._is_current(gen):
            return
        self._bunker = client
        self._clear_signer_unreachable()
        self._open_subscription()
        # A reconnect finds drafts we already gave up on, and the relay
        # will not re-deliver their wraps because the store already holds
        # them at that ``created_at``. Nothing else would ever ask again.
        self._requeue_failed_drafts()
        self._refresh_timer.start()

    def _on_bunker_unavailable(self, gen: int, reason: str) -> None:
        if not self._is_current(gen):
            return
        # Without a signer we can't decrypt anything, so surface the
        # failure but keep the wraps as skeleton rows: the user should
        # see there *are* drafts, just locked.
        self.status_changed.emit(f"Signer unavailable: {reason}")
        self._open_subscription()
        # A handshake that went unanswered is the strongest evidence we
        # ever get that nothing is listening, so there is no reason to
        # spend two draft timeouts rediscovering it.
        if is_signer_silent(reason):
            self._latch_signer_unreachable()

    def _open_subscription(self) -> None:
        if self._profile is None or not self._read_relays:
            self._store.set_loading(False)
            self.status_changed.emit("No relays available to fetch drafts.")
            return
        filters = [{
            "kinds": [DRAFT_WRAP_KIND],
            "authors": [self._profile.user_pubkey],
        }]
        self._subscription = self._relay_pool.subscribe(self._read_relays, filters)
        self._subscription.event.connect(self._on_wrap_event)
        self._subscription.eose.connect(self._on_eose)

    # -- internal: handle inbound wraps -----------------------------------

    def _on_wrap_event(self, event: dict) -> None:
        if self._profile is None:
            return
        meta = parse_wrap_event(event)
        if meta is None:
            return
        if meta.pubkey != self._profile.user_pubkey.lower():
            return  # relay returned an unrelated event; ignore defensively

        existing = self._store.get(meta.identifier)
        if meta.is_tombstone:
            self._store.upsert_skeleton(meta)
            self._pending.pop(meta.identifier, None)
            return

        # Skip stale wraps so we don't re-decrypt the same draft.
        if existing is not None and meta.created_at <= existing.created_at:
            return

        self._store.upsert_skeleton(meta)
        self._enqueue_decrypt(meta)

    def _on_eose(self) -> None:
        self._store.set_loading(False)
        count = len(self._store)
        self.status_changed.emit(
            f"Loaded {count} draft{'' if count == 1 else 's'}."
        )

    # -- internal: bunker decryption queue --------------------------------

    def _enqueue_decrypt(self, meta: DraftWrapMeta) -> None:
        if self._bunker_unsupported or self._bunker is None:
            # Nothing we can do until the signer is back.
            return
        if self._signer_unreachable:
            # Wraps keep arriving from the relays after we have given up
            # asking. Marking them says so, where leaving them loading
            # would show a row decrypting with nothing on its way.
            self._store.set_failed(meta.identifier, _SIGNER_SILENT_REASON)
            return
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
        if self._signer_unreachable:
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
        # is the only thing the unreachable latch is about.
        self._clear_signer_unreachable()
        try:
            inner = parse_inner_event(plaintext)
        except ValueError as exc:
            self._store.set_failed(identifier, f"malformed draft payload: {exc}")
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
                "inner draft is signed by a different identity",
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
            self._store.set_failed(identifier, _SIGNER_SILENT_REASON)
            self._consecutive_silences += 1
            if self._consecutive_silences >= _SILENCE_LIMIT:
                self._latch_signer_unreachable()
                return
            self._after_decrypt(identifier)
            return

        # A reasoned refusal proves the signer is there, so it clears the
        # silence tally even though this particular draft failed.
        self._consecutive_silences = 0
        self._store.set_failed(identifier, reason or "decryption failed")
        self._after_decrypt(identifier)

    def _latch_signer_unreachable(self) -> None:
        """Stop asking a signer that is not there.

        Every draft still waiting is marked so the panel can offer a
        retry per row, and the queue is emptied so a signer that comes
        back does not first have to chew through a backlog of requests
        the user has long stopped caring about.
        """
        if self._signer_unreachable:
            return
        self._signer_unreachable = True
        self._decrypt_queue.clear()
        self._pending.clear()
        self._decrypt_inflight = None
        for record in list(self._store):
            if record.state is DraftState.LOADING:
                self._store.set_failed(record.identifier, _SIGNER_SILENT_REASON)
        self.signer_unreachable.emit(True)

    def _clear_signer_unreachable(self) -> None:
        """The signer answered, so drop the latch and the tally."""
        self._consecutive_silences = 0
        if not self._signer_unreachable:
            return
        self._signer_unreachable = False
        self.signer_unreachable.emit(False)

    def _latch_bunker_unsupported(self) -> None:
        """Stop hammering a signer that has no NIP-44 support."""
        self._bunker_unsupported = True
        self.bunker_error.emit(
            "This signer doesn't support NIP-44 encryption, so "
            "private drafts are unavailable for this profile."
        )
        # Mark every loading record so the panel can render the
        # locked / unavailable state per row.
        for record in list(self._store):
            if record.state is DraftState.LOADING:
                self._store.set_failed(
                    record.identifier,
                    "signer lacks NIP-44 support",
                )
        self._decrypt_queue.clear()
        self._pending.clear()
        self._decrypt_inflight = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _select_read_relays(
    relay_list: RelayList,
    *,
    bunker_relays,
    cap: int = READ_RELAY_CAP,
) -> List[str]:
    """Choose the relay set to subscribe to for the user's drafts.

    Order of preference:
      1. NIP-65 read relays the user has explicitly published.
      2. NIP-65 write relays, because drafts are stashed there, so they're
         the next-best source if no read set exists.
      3. Bunker relays the profile was paired through.
      4. Curated defaults as a last-resort backstop so a brand-new
         profile with no published lists still resolves *something*.

    De-duplicated, capped at ``cap``. Distinct from the publish-relay
    selector (``outbox.select_publish_relays``) which deliberately
    blends our curated set at the *front*. For reads, we honour the
    user's choices first.
    """
    seen: set[str] = set()
    out: List[str] = []

    def add(urls) -> None:
        for raw in urls:
            url = raw.strip() if isinstance(raw, str) else ""
            if not url:
                continue
            key = url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(url)
            if len(out) >= cap:
                return

    add(relay_list.read)
    if len(out) < cap:
        add(relay_list.write)
    if len(out) < cap:
        add(bunker_relays)
    if not out:
        # Nothing user-specific to consult, so fall back to curated set
        # so the subscription has somewhere to land.
        add(DEFAULT_RELAYS)
    return out
