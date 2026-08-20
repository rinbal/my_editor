# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscribed-feed list, synced as one encrypted kind 30078 event.

The user's feed subscriptions are stored as a single NIP-78 replaceable
event so they sync across devices via their relays and survive cache
wipes. The content is NIP-44 self-encrypted: neither the subscription
list nor its URLs leak to relays in plaintext.

Payload shape (inside the ciphertext)::

    { "feeds": [ { "url": "...", "title": "...",
                   "last_fetched_at": 1716000000 } ],
      "updated_at": 1716000000 }

Disciplines, mirroring the reference store:

- optimistic local update + a JSON cache file for instant first paint,
- debounced publish so rapid add/remove/refresh batches into one signed
  event per window (one signer prompt, not five),
- a relay refresh never clobbers unsynced local edits,
- ``flush()`` for logout/quit so a pending debounce still ships,
- ``last_fetched_at`` per feed powers the "since last visit" scope.

Every external boundary (queries, publishing, bunker crypto, timer,
clock, cache path) is injectable, so the whole store is testable
without relays, a signer, or wall-clock time.
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .. import CLIENT_NAME
from ..events import build_event
from ..outbox import RelayListCache, select_draft_publish_relays
from ..bunker import BunkerSessionPool
from ..profiles import Profile
from ..relay import RelayPool
from .constants import (
    FEED_LIST_DTAG,
    SUBSCRIPTIONS_DEBOUNCE_MS,
    SUBSCRIPTIONS_KIND,
)
from .registry import can_resolve_source
from .sources.nostr import RelayQueryAdapter

_log = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".config" / "my_editor"


@dataclass(frozen=True)
class FeedSubscription:
    url: str
    title: str = ""
    last_fetched_at: int = 0


def _normalise(url: str) -> str:
    return (url or "").strip().lower()


class FeedSubscriptionStore(QObject):
    """The user's feed subscriptions, per active profile.

    Signals:
      feeds_changed()        the list changed (any reason)
      sync_status(str)       human-readable sync progress / errors
    """

    feeds_changed = Signal()
    sync_status = Signal(str)

    def __init__(
        self,
        *,
        session_pool: BunkerSessionPool,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        cache_dir: Optional[Path] = None,
        query=None,
        publisher: Optional[Callable[..., None]] = None,
        scheduler: Optional[Callable[..., Callable[[], None]]] = None,
        clock: Optional[Callable[[], int]] = None,
        debounce_ms: int = SUBSCRIPTIONS_DEBOUNCE_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session_pool = session_pool
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._cache_dir = Path(cache_dir) if cache_dir else _CACHE_DIR
        self._query = query if query is not None else (
            RelayQueryAdapter(relay_pool, parent=self)
            if relay_pool is not None else None
        )
        self._publisher = publisher or self._default_publisher
        self._scheduler = scheduler or self._default_scheduler
        self._clock = clock or (lambda: int(_time.time()))
        self._debounce_ms = debounce_ms

        self._profile: Optional[Profile] = None
        self._feeds: List[FeedSubscription] = []
        # True while local edits haven't reached relays; a relay refresh
        # must not clobber them (the pending publish wins).
        self._dirty = False
        self._cancel_timer: Optional[Callable[[], None]] = None
        self._publish_in_flight = False

    # -- profile binding ---------------------------------------------------

    def bind_profile(self, profile: Optional[Profile]) -> None:
        """Switch to (or clear) the active profile and load its list."""
        if profile is self._profile:
            return
        self._cancel_pending_timer()
        self._profile = profile
        self._feeds = []
        self._dirty = False
        if profile is None:
            self.feeds_changed.emit()
            return
        self._feeds = self._load_cache()
        self.feeds_changed.emit()
        self._refresh_from_relays()

    # -- read API ----------------------------------------------------------

    @property
    def feeds(self) -> List[FeedSubscription]:
        return list(self._feeds)

    def has_feed(self, url: str) -> bool:
        target = _normalise(url)
        return any(_normalise(f.url) == target for f in self._feeds)

    def get(self, url: str) -> Optional[FeedSubscription]:
        target = _normalise(url)
        for feed in self._feeds:
            if _normalise(feed.url) == target:
                return feed
        return None

    # -- mutations ---------------------------------------------------------

    def add_feed(self, url: str, title: str = "") -> dict:
        """Subscribe. The registry is the validation gate, so anything a
        resolver claims (feed URL, Nostr address, ...) is storable and
        nothing else ever syncs to relays."""
        cleaned = (url or "").strip()
        if not cleaned or not can_resolve_source(cleaned):
            return {"added": False, "invalid": True}
        if self.has_feed(cleaned):
            return {"added": False, "duplicate": True}
        self._feeds.append(FeedSubscription(url=cleaned, title=title or ""))
        self._after_mutation()
        return {"added": True}

    def remove_feed(self, url: str) -> bool:
        target = _normalise(url)
        kept = [f for f in self._feeds if _normalise(f.url) != target]
        if len(kept) == len(self._feeds):
            return False
        self._feeds = kept
        self._after_mutation()
        return True

    def mark_fetched(self, url: str, when: Optional[int] = None) -> None:
        stamp = int(when if when is not None else self._clock())
        self._update_where(url, lambda f: replace(f, last_fetched_at=stamp))

    def update_title(self, url: str, title: str) -> None:
        self._update_where(url, lambda f: replace(f, title=title or ""))

    def _update_where(self, url: str, updater) -> bool:
        target = _normalise(url)
        changed = False
        updated: List[FeedSubscription] = []
        for feed in self._feeds:
            if _normalise(feed.url) == target:
                updated.append(updater(feed))
                changed = True
            else:
                updated.append(feed)
        if changed:
            self._feeds = updated
            self._after_mutation()
        return changed

    def flush(self) -> None:
        """Publish any debounced changes immediately (logout / quit)."""
        self._cancel_pending_timer()
        if self._dirty:
            self._publish_now()

    # -- internals: mutation plumbing --------------------------------------

    def _after_mutation(self) -> None:
        self._dirty = True
        self._save_cache()
        self.feeds_changed.emit()
        self._schedule_publish()

    def _schedule_publish(self) -> None:
        self._cancel_pending_timer()
        self._cancel_timer = self._scheduler(
            self._debounce_ms, self._on_debounce_fired)

    def _on_debounce_fired(self) -> None:
        self._cancel_timer = None
        self._publish_now()

    def _cancel_pending_timer(self) -> None:
        if self._cancel_timer is not None:
            try:
                self._cancel_timer()
            finally:
                self._cancel_timer = None

    def _default_scheduler(
        self, ms: int, fn: Callable[[], None]
    ) -> Callable[[], None]:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(fn)
        timer.start(ms)

        def _cancel() -> None:
            timer.stop()
            timer.deleteLater()

        return _cancel

    # -- internals: local cache --------------------------------------------

    def _cache_path(self) -> Optional[Path]:
        if self._profile is None:
            return None
        return self._cache_dir / f"feed_sources_{self._profile.user_pubkey}.json"

    def _load_cache(self) -> List[FeedSubscription]:
        path = self._cache_path()
        if path is None or not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return _parse_payload(payload)

    def _save_cache(self) -> None:
        path = self._cache_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._payload(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            _log.warning("feed cache write failed: %s", exc)

    def _payload(self) -> dict:
        return {
            "feeds": [
                {
                    "url": f.url,
                    **({"title": f.title} if f.title else {}),
                    **({"last_fetched_at": f.last_fetched_at}
                       if f.last_fetched_at else {}),
                }
                for f in self._feeds
            ],
            "updated_at": int(self._clock()),
        }

    # -- internals: relay sync ---------------------------------------------

    def _relays_for(self, relay_list, profile: Profile) -> List[str]:
        return select_draft_publish_relays(
            relay_list, bunker_relays=profile.bunker_relays)

    def _refresh_from_relays(self) -> None:
        profile = self._profile
        if profile is None or self._query is None:
            return
        self.sync_status.emit("Syncing feed subscriptions…")

        def _on_relay_list(relay_list) -> None:
            if self._profile is not profile:
                return
            relays = self._relays_for(relay_list, profile)
            self._query.latest(
                relays,
                [{
                    "kinds": [SUBSCRIPTIONS_KIND],
                    "authors": [profile.user_pubkey],
                    "#d": [FEED_LIST_DTAG],
                    "limit": 1,
                }],
                _on_event,
            )

        def _on_event(event) -> None:
            if self._profile is not profile:
                return
            if not event or not str(event.get("content") or "").strip():
                self.sync_status.emit("")
                return
            self._decrypt_and_adopt(profile, str(event["content"]))

        self._relay_list_cache.fetch(
            profile.user_pubkey,
            relays=list(dict.fromkeys(profile.bunker_relays)),
            on_done=_on_relay_list,
        )

    def _decrypt_and_adopt(self, profile: Profile, ciphertext: str) -> None:
        def _on_ready(client) -> None:
            client.nip44_decrypt_self(
                ciphertext,
                on_success=_adopt,
                on_failure=lambda reason: self.sync_status.emit(
                    f"Couldn't decrypt the synced feed list: {reason}"),
            )

        def _adopt(plaintext: str) -> None:
            if self._profile is not profile:
                return
            try:
                payload = json.loads(plaintext)
            except (ValueError, TypeError):
                self.sync_status.emit("")
                return
            if self._dirty:
                # Local unsynced edits win; the pending publish will
                # overwrite the relay state with the merged result.
                self.sync_status.emit("")
                return
            self._feeds = _parse_payload(payload)
            self._save_cache()
            self.feeds_changed.emit()
            self.sync_status.emit("")

        self._session_pool.get(
            profile,
            on_ready=_on_ready,
            on_error=lambda reason: self.sync_status.emit(
                f"Couldn't reach the signer to sync feeds: {reason}"),
        )

    def _publish_now(self) -> None:
        profile = self._profile
        if profile is None:
            return
        if self._publish_in_flight:
            # Re-schedule so the latest state still ships once the
            # in-flight publish settles.
            self._schedule_publish()
            return
        self._publish_in_flight = True
        self._dirty = False
        plaintext = json.dumps(self._payload(), separators=(",", ":"))

        def _fail(reason: str) -> None:
            self._publish_in_flight = False
            self._dirty = True  # keep the change queued for a retry
            self.sync_status.emit(
                f"Couldn't sync feed subscriptions: {reason}")

        def _on_ready(client) -> None:
            client.nip44_encrypt_self(
                plaintext,
                on_success=lambda ciphertext: _sign(client, ciphertext),
                on_failure=_fail,
            )

        def _sign(client, ciphertext: str) -> None:
            unsigned = build_event(
                kind=SUBSCRIPTIONS_KIND,
                content=ciphertext,
                tags=[
                    ["d", FEED_LIST_DTAG],
                    ["client", CLIENT_NAME],
                    ["encrypted", "nip44_v2"],
                ],
                pubkey_hex=profile.user_pubkey,
            )
            client.sign_event(
                unsigned,
                on_success=_publish,
                on_failure=_fail,
            )

        def _publish(signed: dict) -> None:
            def _on_relay_list(relay_list) -> None:
                relays = self._relays_for(relay_list, profile)
                self._publisher(relays, signed, on_done=_done)

            self._relay_list_cache.fetch(
                profile.user_pubkey,
                relays=list(dict.fromkeys(profile.bunker_relays)),
                on_done=_on_relay_list,
            )

        def _done(accepted: int, total: int) -> None:
            self._publish_in_flight = False
            if accepted > 0:
                self.sync_status.emit("")
            else:
                self._dirty = True
                self.sync_status.emit(
                    "Feed subscriptions saved locally; no relay accepted "
                    "the sync yet.")

        self._session_pool.get(profile, on_ready=_on_ready, on_error=_fail)

    def _default_publisher(self, relays, signed, *, on_done) -> None:
        try:
            job = self._relay_pool.publish(list(relays), signed)
        except Exception as exc:  # noqa: BLE001, publish must never raise into UI
            _log.warning("subscription publish failed: %s", exc)
            on_done(0, len(list(relays)))
            return
        job.all_done.connect(
            lambda results: on_done(
                sum(1 for _, ok, _ in results if ok), len(results)))


def _parse_payload(payload) -> List[FeedSubscription]:
    """Validated subscriptions out of a payload dict; junk rows drop."""
    rows = payload.get("feeds") if isinstance(payload, dict) else None
    out: List[FeedSubscription] = []
    seen: set = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        # Drop anything that can't be a source (over-length blobs, XML
        # pastes persisted by an older bug, ...) so bad rows prune on
        # load instead of sticking around forever.
        if not url or not can_resolve_source(url):
            continue
        key = _normalise(url)
        if key in seen:
            continue
        seen.add(key)
        try:
            last = int(row.get("last_fetched_at") or 0)
        except (TypeError, ValueError):
            last = 0
        out.append(FeedSubscription(
            url=url,
            title=str(row.get("title") or ""),
            last_fetched_at=max(0, last),
        ))
    return out
