# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The person's sources, synced as one encrypted kind 30078 event.

The list is shared with EINUNDZWANZIG STANDUP: same event (``d`` =
``einundzwanzig:feed-sources``), same payload, same ``["encrypted",
"nip44"]`` tag (feed_list.py describes the payload and the merge). A
source followed in one app shows up in the other, and neither app's
edits are lost to the other's.

Disciplines:

- Optimistic local update, and a JSON cache file per account for an
  instant first paint. The cache also keeps the version last seen on
  the relays and whether local edits still have to go out, so edits
  made just before quitting are sent on the next launch.
- Debounced publish, so rapid edits become one signed event (one
  signer prompt, not five).
- Merge, never overwrite: before publishing, the newest list is read
  again and both apps' edits since the last sync are merged
  (feed_list.merge). A relay answer that arrives while a publish is on
  its way is merged the same way, so it cannot undo an edit.
- Only a newer list counts: the store remembers which event the last
  list seen came from (its time, then its id, as NIP-01 orders
  replaceable events), and a copy that is not newer (a slow or fallback
  relay that kept an old one) neither adds nor removes anything.
- No answer is never "no list": a read counts only when one of the
  account's own relays answered it. Without that, nothing is published
  (the change waits, here and in the cache, for a read that is
  answered), and the old list of this app is not marked as merged.
- A list this app cannot read (saved with older encryption, or the
  signer is unreachable) is never written over: that would delete the
  other app's sources. The edits stay here until it can be read.
- Only real changes are published. A source's last read time travels
  along with one, or when the person opens a manual source; automatic
  checks never cause a publish (a signer prompt each time).
- The list this app wrote before (``myeditor:feed-sources``) is merged
  into the shared one once per account, and never written again.
- ``flush()`` for sign-out and quit, and :meth:`wait_until_settled` so
  quitting can wait (a few seconds at most) for it to arrive.

Every external boundary (queries, publishing, signer, timer, clock,
cache path) is injectable, so the whole store is testable without
relays, a signer, or wall-clock time.
"""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal

import user_dirs
from atomic_file import write_json
from i18n import _

from .. import CLIENT_NAME
from ..bunker import BunkerSessionPool, is_signer_silent
from ..drafts import supersedes
from ..events import build_event, verify_event
from ..outbox import RelayDirectory, ask_private_relays
from ..outbox.policy import normalize_relay_url, replacement_created_at
from ..profiles import Profile
from ..queries import fetch_events
from ..relay import RelayPool
from .constants import SUBSCRIPTIONS_DEBOUNCE_MS, SUBSCRIPTIONS_KIND
from .feed_list import (
    FEED_LIST_DTAG,
    LEGACY_FEED_LIST_DTAG,
    FeedList,
    FeedSubscription,
    SourceOptions,
    is_storable_url,
    merge,
    read_payload,
    write_payload,
)
from .registry import can_resolve_source

__all__ = ["FeedSubscription", "FeedSubscriptionStore", "SourceOptions"]

_log = logging.getLogger(__name__)

_CACHE_VERSION = 2

# What a list read from the relays can turn out to be.
_UNREADABLE = "unreadable"
_NO_ANSWER = "no answer"

# What the list's status says (sync_status), in plain words: the window
# shows it (CP2 review N3: nothing heard these since the Feeds page went).
SYNCING = _("Reading your list of sources\u2026")      # under way: not shown
NOT_READ = _("Your list of sources couldn't be read online, so MyEditor shows the copy "
             "kept on this computer.")
NOT_SHARED = _("Your latest changes to your sources are kept on this computer for now: "
               "the list couldn't be saved online. MyEditor tries again on its own.")
# Why a list could not be read goes to the log, never into these words:
# it is English, and seldom something a person can act on (CP3 review L5).
UNREADABLE = _("Your list of sources couldn't be read.")
SIGNER_SILENT = _("Your signer app didn't answer, so your list of sources couldn't be read.")
# Pauses before an unshared change is sent again, then every half hour.
RETRY_DELAYS_MS = (2 * 60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000)


class _ListQuery(QObject):
    """Asks relays for events and says which relays answered."""

    def __init__(self, pool: RelayPool, parent: Optional[QObject] = None, *,
                 timeout_ms: int = 8_000) -> None:
        super().__init__(parent)
        self._pool = pool
        self._timeout_ms = timeout_ms

    def events(self, relays, filters, on_done) -> None:
        """``on_done(events, answered)``: every validly signed event, and
        the relays that answered (sent all their stored events); one that
        refused or could not be reached is not among them."""
        try:
            fetch_events(self._pool, list(relays), list(filters),
                         lambda fetched: on_done(list(fetched.events), set(fetched.answered)),
                         timeout_ms=self._timeout_ms, parent=self)
        except Exception:  # noqa: BLE001, settle the callback contract
            on_done([], set())


def _mark(event: Optional[dict]) -> Optional[Tuple[int, str]]:
    """Which event a list came from: its time and id."""
    if not isinstance(event, dict):
        return None
    try:
        return int(event.get("created_at", 0)), str(event.get("id") or "")
    except (TypeError, ValueError):
        return None

_OLD_ENCRYPTION = _(
    "Your list of sources was saved with an older kind of encryption. Open "
    "it once in the app that saved it; until then, changes made here stay "
    "on this computer.")


class FeedSubscriptionStore(QObject):
    """The person's sources, per active profile.

    Signals:
      feeds_changed()        the list changed (any reason)
      sync_status(str)       human-readable sync progress / errors
      settled()              nothing is waiting to be published any more
    """

    feeds_changed = Signal()
    sync_status = Signal(str)
    settled = Signal()

    def __init__(
        self,
        *,
        session_pool: BunkerSessionPool,
        relay_pool: RelayPool,
        relay_directory: RelayDirectory,
        cache_dir: Optional[Path] = None,
        query=None,
        publisher: Optional[Callable[..., None]] = None,
        scheduler: Optional[Callable[..., Callable[[], None]]] = None,
        clock: Optional[Callable[[], int]] = None,
        debounce_ms: int = SUBSCRIPTIONS_DEBOUNCE_MS,
        entitled_relays: Optional[Callable[[], Sequence[str]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session_pool = session_pool
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._entitled_relays = entitled_relays
        self._cache_dir = Path(cache_dir) if cache_dir else user_dirs.config_dir()
        self._query = query if query is not None else (
            _ListQuery(relay_pool, parent=self) if relay_pool is not None else None)
        self._publisher = publisher or self._default_publisher
        self._scheduler = scheduler or self._default_scheduler
        self._clock = clock or (lambda: int(_time.time()))
        self._debounce_ms = debounce_ms

        self._profile: Optional[Profile] = None
        # Bumped on every profile change; a callback for an older one
        # leaves the current state alone.
        self._generation = 0
        self._local = FeedList()
        # The version last known to be on the relays (None: never seen),
        # and which event it came from (its time and id).
        self._base: Optional[FeedList] = None
        self._base_mark: Optional[Tuple[int, str]] = None
        # True while local edits haven't reached relays.
        self._dirty = False
        # Tries since a change could not be shared (the pause grows), and
        # whether the timer that runs is such a pause.
        self._retries = 0
        self._retry_waiting = False
        # The next publish also sends read times (a manual source opened).
        self._send_read_times = False
        self._legacy_merged = False
        self._cancel_timer: Optional[Callable[[], None]] = None
        # Publishes on their way, by generation; one at a time per account.
        self._runs: Dict[int, int] = {}
        # Edits made while a publish was on its way go out right after it.
        self._publish_after_run = False
        self._read_only = False

    # -- profile binding ---------------------------------------------------

    def bind_profile(self, profile: Optional[Profile]) -> None:
        """Switch to (or clear) the active profile and load its list.

        Edits still waiting for the previous profile are sent first; that
        publish finishes on its own and only touches that profile's cache.
        """
        if profile is self._profile:
            return
        if self._profile is not None and (self._dirty or self._cancel_timer is not None):
            self.flush()
        self._cancel_pending_timer()
        self._generation += 1
        self._profile = profile
        self._local, self._base, self._base_mark = FeedList(), None, None
        self._dirty = self._send_read_times = self._legacy_merged = False
        self._retries = 0
        self._known = False
        self._publish_after_run = False
        if profile is None:
            self.feeds_changed.emit()
            return
        self._load_cache()
        self.feeds_changed.emit()
        self.refresh()

    def reload(self) -> None:
        """Read the list again, this computer's copy first, then the relays:
        another MyEditor may have changed it since it was read here (the one
        that kept the inbox before this one, CP1 review L7). A change still
        waiting here is never dropped: then only the relays are asked, and
        what they answer is merged as always. A process that was read only
        has none: the copy it reads is the other one's."""
        profile = self._profile
        if profile is None:
            return
        if self._dirty or self._cancel_timer is not None:
            self.refresh()
            return
        self._profile = None
        self.bind_profile(profile)

    def update_profile(self, profile: Profile) -> None:
        """The same account signs another way now (a signer app paired,
        or its key restored here): what comes next is signed and read
        through the new profile. Nothing is reloaded and nothing waiting
        is lost; another account is a switch (``bind_profile``)."""
        if (self._profile is None or profile is None
                or profile.user_pubkey.lower() != self._profile.user_pubkey.lower()):
            self.bind_profile(profile)
            return
        self._profile = profile

    @property
    def read_only(self) -> bool:
        """Whether another MyEditor on this computer keeps this account's
        inbox and writes the list, so this one only reads it."""
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool) -> None:
        """Read only, nothing is written to this computer's copy and nothing
        is published: the copy stays the other one's, a change it could not
        send yet included, for when this one takes over (``reload``). Read
        times stay in memory."""
        self._read_only = value
        if value:
            # A change waiting in the copy read at binding is the other
            # MyEditor's to send.
            self._cancel_pending_timer()
            self._dirty = False

    # -- read API ----------------------------------------------------------

    @property
    def known(self) -> bool:
        """Whether the list is known: read from this computer's copy, or
        from the relays. Until then a source missing from it may only not
        be loaded yet, so nothing may be removed because of it."""
        return self._known

    def _mark_known(self) -> None:
        if not self._known:
            self._known = True
            self.feeds_changed.emit()

    @property
    def feeds(self) -> List[FeedSubscription]:
        return self._local.feeds()

    def has_feed(self, url: str) -> bool:
        return self._local.row(url) is not None

    def get(self, url: str) -> Optional[FeedSubscription]:
        row = self._local.row(url)
        if row is None:
            return None
        return next((f for f in self.feeds if f.url == row["url"].strip()), None)

    def options_for(self, url: str) -> SourceOptions:
        """The defaults for new drafts from ``url`` (both on until changed)."""
        return self._local.options_for(url)

    @property
    def is_busy(self) -> bool:
        """Whether something is still waiting to be published, or on its way."""
        return self._cancel_timer is not None or any(self._runs.values())

    # -- mutations ---------------------------------------------------------

    def add_feed(self, url: str, title: str = "") -> dict:
        """Follow a source. The registry is the validation gate, so anything
        a resolver claims (feed URL, Nostr address, ...) is storable and
        nothing else ever syncs to relays."""
        result = self._add(url, title)
        if result.get("added"):
            self._after_mutation()
        return result

    def add_feeds(self, feeds: Sequence[Tuple[str, str]]) -> List[dict]:
        """``add_feed`` for many sources (a list of sources): one change of
        the list, one sync, one publish. Returns each one's result."""
        results = [self._add(url, title) for url, title in feeds]
        if any(result.get("added") for result in results):
            self._after_mutation()
        return results

    def _add(self, url: str, title: str) -> dict:
        cleaned = (url or "").strip()
        if not is_storable_url(cleaned) or not can_resolve_source(cleaned):
            return {"added": False, "invalid": True}
        if not self._local.add(cleaned, title or ""):
            return {"added": False, "duplicate": True}
        return {"added": True}

    def remove_feed(self, url: str) -> bool:
        if not self._local.remove(url):
            return False
        self._after_mutation()
        return True

    def update_title(self, url: str, title: str) -> None:
        if self._local.set_title(url, title or ""):
            self._after_mutation()

    def set_source_options(self, url: str, *, rehost_images: Optional[bool] = None,
                           fetch_full_text: Optional[bool] = None) -> None:
        """Remember a source's defaults for new drafts (synced)."""
        values = {}
        if rehost_images is not None:
            values["rehostImages"] = bool(rehost_images)
        if fetch_full_text is not None:
            values["fetchFullText"] = bool(fetch_full_text)
        if values and self._local.set_options(url, **values):
            self._after_mutation()

    def mark_fetched(self, url: str, when: Optional[int] = None, *,
                     publish: bool = False) -> None:
        """Note when a source was last read. Kept here, and sent along with
        the next real change; ``publish`` sends it now (the person opened
        a manual source, as STANDUP does)."""
        stamp = int(when if when is not None else self._clock())
        if not self._local.set_fetched(url, stamp):
            return
        if publish and not self._read_only:
            self._send_read_times = True
            self._after_mutation()
        else:
            self._save_cache()
            self.feeds_changed.emit()

    def flush(self) -> None:
        """Publish any debounced changes immediately (sign-out / quit)."""
        self._cancel_pending_timer()
        if self._dirty:
            self._publish_now()
        elif not self.is_busy:
            self.settled.emit()

    def wait_until_settled(self, timeout_ms: int) -> bool:
        """Run the event loop until nothing is waiting to be published, at
        most ``timeout_ms``. True when everything was sent (or nothing
        needed to be). For quitting: the signer and relay sockets close
        right after."""
        if not self.is_busy:
            return True
        loop = QEventLoop()
        self.settled.connect(loop.quit)
        QTimer.singleShot(max(0, int(timeout_ms)), loop.quit)
        try:
            if self.is_busy:
                loop.exec()
        finally:
            self.settled.disconnect(loop.quit)
        return not self.is_busy

    def refresh(self) -> None:
        """Read the list from the relays and merge it in."""
        profile = self._profile
        if profile is None or self._query is None:
            return
        generation = self._generation
        self.sync_status.emit(SYNCING)

        def _on_relays(relays) -> None:
            if generation != self._generation:
                return
            self._read_list(profile, relays, FEED_LIST_DTAG,
                            lambda result, event: _on_shared(relays, result, event))

        def _on_shared(relays, result, event) -> None:
            if generation != self._generation:
                return
            if result == _NO_ANSWER:
                # Asked again on the next launch or refresh; until then the
                # list is not known and nothing is taken for missing.
                self.sync_status.emit(NOT_READ)
                return
            if isinstance(result, FeedList):
                self._adopt(result, event)
            if result != _UNREADABLE:
                # Answered: a change that waits here goes out now.
                self._send_waiting()
            if self._legacy_merged:
                if result != _UNREADABLE:
                    self.sync_status.emit("")
                    self._mark_known()
                return
            self._read_list(profile, relays, LEGACY_FEED_LIST_DTAG,
                            lambda legacy, _event: _on_legacy(legacy, result))

        def _on_legacy(legacy, shared_result) -> None:
            if generation != self._generation:
                return
            if legacy in (_UNREADABLE, _NO_ANSWER):
                return  # tried again on the next launch, never marked done
            if isinstance(legacy, FeedList):
                self._merge_legacy(legacy)
            self._legacy_merged = True
            self._save_cache()
            if shared_result != _UNREADABLE:
                self.sync_status.emit("")
                self._mark_known()

        self._with_relays(profile, _on_relays, reading=True)

    # -- internals: merging --------------------------------------------------

    def _newer_than_base(self, event: Optional[dict]) -> bool:
        """Whether a list read from the relays is newer than the last one
        seen (NIP-01: the later time, then the lower id)."""
        mark = _mark(event)
        if self._base_mark is None or mark is None:
            return True
        return supersedes(mark[0], mark[1], *self._base_mark)

    def _adopt(self, remote: FeedList, event: Optional[dict] = None) -> None:
        """Merge a version read from the relays into the local list: only
        one newer than the last seen. An older copy, kept by a slow or a
        fallback relay, would otherwise remove what the other app added
        since, or bring back what was removed here."""
        if event is not None and not self._newer_than_base(event):
            return
        merged = merge(self._base, self._local, remote)
        changed = merged != self._local
        self._local = merged
        self._base = remote
        if event is not None:
            self._base_mark = _mark(event)
        self._dirty = not self._read_only and (
            not merged.same_sources(remote) or self._send_read_times)
        self._save_cache()
        if changed:
            self.feeds_changed.emit()
        if self._dirty:
            self._schedule_publish()

    def _merge_legacy(self, legacy: FeedList) -> None:
        """Add the sources of this app's older list that are not here yet."""
        added = False
        for row in legacy.rows:
            if self._local.row(row["url"]) is None:
                self._local.rows.append(dict(row))
                added = True
        if added:
            self._after_mutation()

    # -- internals: mutation plumbing --------------------------------------

    def _send_waiting(self) -> None:
        """The relays answered a read: a change waiting here goes out now,
        also while the pause after a try that failed runs (CP3 review L3:
        it waited up to half an hour)."""
        if (self._dirty and not any(self._runs.values())
                and (self._cancel_timer is None or self._retry_waiting)):
            self._schedule_publish()

    def _retry_later(self) -> None:
        """Send a change that could not be shared again after a pause that
        grows (two minutes, five, fifteen, then every half hour). Each try
        reads first, so a signer is asked only once the list can be read."""
        if self._cancel_timer is not None:
            return
        delay = RETRY_DELAYS_MS[min(self._retries, len(RETRY_DELAYS_MS) - 1)]
        self._retries += 1
        self._cancel_timer = self._scheduler(delay, self._on_debounce_fired)
        self._retry_waiting = True

    def _after_mutation(self) -> None:
        if self._read_only:
            # The other MyEditor merges and shares the list (an older list
            # of this app's included); here it is only shown.
            self.feeds_changed.emit()
            return
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
        self._retry_waiting = False
        self._publish_now()

    def _cancel_pending_timer(self) -> None:
        self._retry_waiting = False
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

    def _cache_path(self, pubkey: Optional[str] = None) -> Optional[Path]:
        pubkey = pubkey or (self._profile.user_pubkey if self._profile else None)
        if not pubkey:
            return None
        return self._cache_dir / f"feed_sources_{pubkey}.json"

    def _load_cache(self) -> None:
        path = self._cache_path()
        if path is None or not path.exists():
            return
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(cached, dict):
            return
        self._known = True
        if cached.get("version") != _CACHE_VERSION:
            # This app's older cache: the list it synced on its own. It is
            # merged with the shared list as soon as that is read.
            self._local = read_payload(cached)
            return
        self._local = read_payload(cached.get("local"))
        base = cached.get("base")
        self._base = read_payload(base) if isinstance(base, dict) else None
        mark = cached.get("baseEvent")
        if (isinstance(mark, list) and len(mark) == 2 and isinstance(mark[0], int)
                and isinstance(mark[1], str)):
            self._base_mark = (mark[0], mark[1])
        self._dirty = bool(cached.get("dirty"))
        self._legacy_merged = bool(cached.get("legacyMerged"))

    def _save_cache(self) -> None:
        if self._profile is None or self._read_only:
            return
        self._write_cache(self._profile.user_pubkey, self._local, self._base,
                          self._dirty, self._legacy_merged, self._base_mark)

    def _write_cache(self, pubkey: str, local: FeedList, base: Optional[FeedList],
                     dirty: bool, legacy_merged: bool,
                     base_mark: Optional[Tuple[int, str]] = None) -> None:
        path = self._cache_path(pubkey)
        if path is None:
            return
        now = int(self._clock())
        data = {
            "version": _CACHE_VERSION,
            "local": write_payload(local, now),
            "base": write_payload(base, now) if base is not None else None,
            "dirty": dirty,
            "legacyMerged": legacy_merged,
            "baseEvent": list(base_mark) if base_mark else None,
        }
        # write_json logs why when it cannot write; the list itself stays
        # on the relays.
        write_json(path, data, indent=2)

    # -- internals: relays ---------------------------------------------------

    def _with_relays(self, profile: Profile, on_done: Callable[..., None], *,
                     reading: bool = False) -> None:
        """The account's private relays: where the list is written
        (``on_done(relays)``), or with ``reading`` where it is read, which
        also asks the relays a list written before the account's own was
        known went: ``on_done((read, own))``, ``own`` being the relays it
        is written to, the only ones whose answer counts."""
        if not reading:
            ask_private_relays(self._relay_directory, profile, on_done,
                               entitled=self._entitled_relays, reading=False)
            return

        def _own(own: List[str]) -> None:
            ask_private_relays(self._relay_directory, profile,
                               lambda read: on_done((read, own)),
                               entitled=self._entitled_relays, reading=True)

        ask_private_relays(self._relay_directory, profile, _own,
                           entitled=self._entitled_relays, reading=False)

    def _read_list(self, profile: Profile, relays, d_tag: str, on_done) -> None:
        """The newest list under ``d_tag``: ``on_done(result, event)`` with a
        FeedList, None when there is none, _NO_ANSWER when none of the
        account's own relays answered (never taken for "none"), or
        _UNREADABLE (with the reason already reported).

        ``relays`` is ``(read, own)``. Only the account's own lists count,
        each checked (kind, ``d``, author, signature) before the newest is
        chosen, so a relay can neither plant a list nor hide the real one
        behind a newer forged one."""
        read, own = relays
        if self._query is None:
            on_done(None, None)
            return
        counted = {normalize_relay_url(u) or u for u in own}

        def _on_events(events, answered: Set[str]) -> None:
            answered = {normalize_relay_url(u) or u for u in answered}
            if not answered or (counted and not (answered & counted)):
                on_done(_NO_ANSWER, None)
                return
            valid = [e for e in events if _is_list_of(e, profile, d_tag)]
            newest = None
            for event in valid:
                if newest is None or supersedes(int(event["created_at"]), event["id"],
                                                int(newest["created_at"]), newest["id"]):
                    newest = event
            if newest is None or not str(newest.get("content") or "").strip():
                on_done(None, newest)
                return
            self._decrypt(profile, newest,
                          lambda text: on_done(_parse(text), newest),
                          lambda reason: _unreadable(reason, newest))

        def _parse(text: str):
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                _log.warning("the list of sources is not a list")
                self.sync_status.emit(UNREADABLE)
                return _UNREADABLE
            return read_payload(payload)

        def _unreadable(reason: str, event) -> None:
            self.sync_status.emit(reason)
            on_done(_UNREADABLE, event)

        self._query.events(read, [{
            "kinds": [SUBSCRIPTIONS_KIND],
            "authors": [profile.user_pubkey],
            "#d": [d_tag],
            "limit": 1,
        }], _on_events)

    def _decrypt(self, profile: Profile, event: dict, on_text, on_unreadable) -> None:
        """The plaintext of a list, by its ``encrypted`` tag, as STANDUP
        reads it: NIP-44, NIP-04 when the signer offers it, or none."""
        algorithm = next((t[1] for t in event.get("tags", [])
                          if isinstance(t, list) and len(t) >= 2 and t[0] == "encrypted"), "")
        content = str(event.get("content") or "")
        if algorithm not in ("nip44", "nip44_v2", "nip04"):
            on_text(content)  # no encryption (a list from before it was used)
            return

        def failed(reason: str, words: str) -> None:
            """A signer app that did not answer is said as such, whatever
            else ``words`` would advise (CP3 review L9: a timeout read as
            the older encryption)."""
            _log.warning("the list of sources (%s) was not read: %s", algorithm, reason)
            silent = is_signer_silent(reason) or reason == "not connected"
            on_unreadable(SIGNER_SILENT if silent else words)

        def _on_ready(client) -> None:
            if algorithm == "nip04":
                decrypt = getattr(client, "nip04_decrypt_self", None)
                if decrypt is None:
                    on_unreadable(_OLD_ENCRYPTION)
                    return
                # A signer app that refuses NIP-04: what to do is the same
                # as without one.
                decrypt(content, on_success=on_text,
                        on_failure=lambda reason: failed(reason, _OLD_ENCRYPTION))
                return
            client.nip44_decrypt_self(content, on_success=on_text,
                                      on_failure=lambda reason: failed(reason, UNREADABLE))

        self._session_pool.get(profile, on_ready=_on_ready,
                               on_error=lambda reason: failed(reason, SIGNER_SILENT))

    # -- internals: publishing -------------------------------------------------

    def _publish_now(self) -> None:
        profile = self._profile
        if profile is None:
            return
        generation = self._generation
        if self._runs.get(generation):
            # The latest state still ships, right after the publish on
            # its way settles.
            self._publish_after_run = True
            return
        self._runs[generation] = 1
        # What this publish starts from, kept for the case the profile is
        # switched before it is done.
        snapshot = {"base": self._base, "local": self._local.copy(),
                    "legacy": self._legacy_merged, "reads": self._send_read_times,
                    "mark": self._base_mark}

        def current() -> bool:
            return generation == self._generation

        def _finish(status: Optional[str] = "", *, sent: bool = False) -> None:
            """``status`` None leaves the status line as it is."""
            self._runs.pop(generation, None)
            if current():
                if not sent:
                    self._dirty = True
                else:
                    self._retries = 0
                self._save_cache()
                if status is not None:
                    self.sync_status.emit(status)
                again, self._publish_after_run = self._publish_after_run, False
                if self._dirty and self._cancel_timer is None and (sent or again):
                    if again:
                        self._publish_now()
                        return
                    self._schedule_publish()
                elif self._dirty and not sent and status == NOT_SHARED:
                    self._retry_later()
            if not self.is_busy:
                self.settled.emit()

        def _on_read_relays(relays) -> None:
            self._read_list(profile, relays, FEED_LIST_DTAG, _on_remote)

        def _on_remote(remote, event) -> None:
            if remote == _UNREADABLE:
                # Never write over a list that could not be read: it holds
                # the other app's sources. The reason is on the status line.
                _finish(None)
                return
            if remote == _NO_ANSWER:
                # Never write over a list nobody answered for: a newer one
                # may be on the relays. The change waits (dirty, cached).
                _finish(NOT_SHARED)
                return
            base = self._base if current() else snapshot["base"]
            local = self._local if current() else snapshot["local"]
            base_mark = self._base_mark if current() else snapshot["mark"]
            mark = _mark(event)
            if (event is not None and base_mark is not None and mark is not None
                    and not supersedes(mark[0], mark[1], *base_mark)):
                # A copy no newer than the last seen (a slow or fallback
                # relay's): it changes nothing; what is new here goes out.
                remote, event = base, None
            merged = merge(base, local, remote)
            if current():
                changed = merged != self._local
                self._local = merged
                if remote is not None:
                    self._base = remote
                    if event is not None:
                        self._base_mark = mark
                if changed:
                    self.feeds_changed.emit()
            if remote is not None and merged.same_sources(remote) and not snapshot["reads"]:
                # Everything here is on the relays already.
                if current():
                    self._dirty = not self._local.same_sources(remote)
                _finish(sent=True)
                return
            _sign_and_send(merged.copy(), event)

        def _sign_and_send(content: FeedList, remote_event) -> None:
            plaintext = json.dumps(write_payload(content, int(self._clock())),
                                   separators=(",", ":"))

            def _on_ready(client) -> None:
                client.nip44_encrypt_self(
                    plaintext,
                    on_success=lambda ciphertext: _sign(client, ciphertext),
                    on_failure=_failed)

            def _sign(client, ciphertext: str) -> None:
                # After the list it replaces and after the last one seen,
                # even when this computer's clock runs behind.
                known = (self._base_mark if current() else snapshot["mark"]) or (0, "")
                unsigned = build_event(
                    kind=SUBSCRIPTIONS_KIND,
                    content=ciphertext,
                    tags=[["d", FEED_LIST_DTAG], ["client", CLIENT_NAME],
                          ["encrypted", "nip44"]],
                    pubkey_hex=profile.user_pubkey,
                    created_at=max(replacement_created_at(remote_event, self._clock()),
                                   known[0] + 1),
                )
                client.sign_event(unsigned, on_success=_publish, on_failure=_failed)

            def _publish(signed: dict) -> None:
                self._with_relays(profile, lambda relays: self._publisher(
                    relays, signed, on_done=lambda accepted, total: _done(accepted, signed)))

            def _done(accepted: int, signed: dict) -> None:
                if accepted <= 0:
                    _finish(NOT_SHARED)
                    return
                if current():
                    self._base = content
                    self._base_mark = _mark(signed)
                    self._send_read_times = False
                    # Edits made while this was on its way go out next.
                    self._dirty = not self._local.same_sources(content)
                else:
                    self._write_cache(profile.user_pubkey, content, content, False,
                                      snapshot["legacy"], _mark(signed))
                _finish(sent=True)

            self._session_pool.get(profile, on_ready=_on_ready, on_error=_failed)

        def _failed(reason: str) -> None:
            _finish(_("Your list of sources couldn't be saved online ({reason}). Your "
                      "changes are kept on this computer.").format(reason=reason))

        self._with_relays(profile, _on_read_relays, reading=True)

    def _default_publisher(self, relays, signed, *, on_done) -> None:
        """Publish ``signed`` and answer once: as soon as one relay accepted
        it (the list is safe then; a relay that never answers must not hold
        up quitting for the publish timeout), or when all have answered
        and none did."""
        relays = list(relays)
        try:
            job = self._relay_pool.publish(relays, signed)
        except Exception as exc:  # noqa: BLE001, publish must never raise into UI
            _log.warning("subscription publish failed: %s", exc)
            on_done(0, len(relays))
            return
        answered = []

        def accepted(_url: str) -> None:
            if not answered:
                answered.append(True)
                on_done(1, len(relays))

        def all_done(results) -> None:
            if not answered:
                answered.append(True)
                on_done(sum(1 for _, ok, _ in results if ok), len(results))

        job.first_accept.connect(accepted)
        job.all_done.connect(all_done)


def _is_list_of(event, profile: Profile, d_tag: str) -> bool:
    """Whether ``event`` is the account's own list under ``d_tag``: the
    kind, the ``d`` tag, the author and a valid signature."""
    if not isinstance(event, dict) or event.get("kind") != SUBSCRIPTIONS_KIND:
        return False
    if str(event.get("pubkey", "")).lower() != profile.user_pubkey.lower():
        return False
    tag = next((t[1] for t in event.get("tags", [])
                if isinstance(t, list) and len(t) >= 2 and t[0] == "d"), None)
    if tag != d_tag:
        return False
    try:
        int(event.get("created_at"))
    except (TypeError, ValueError):
        return False
    return verify_event(event)
