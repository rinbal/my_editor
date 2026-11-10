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

Reading, checking, merging and writing the event itself is done by
nostr/encrypted_lists.py, which every encrypted list of this kind
shares; this store keeps what is particular to the sources: the cache,
the debounce, the retries, read times and the older list.
"""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal

from atomic_file import write_json
from i18n import _

from ..bunker import BunkerSessionPool, is_signer_silent
from ..drafts import supersedes
from ..encrypted_lists import EncryptedList
from ..list_relays import ListQuery, event_mark, publish_once
from ..list_results import TOO_LONG, ListRead, ListWords, PublishStatus, ReadStatus
from ..outbox import RelayDirectory
from ..profiles import Profile
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

_CACHE_DIR = Path.home() / ".config" / "my_editor"
_CACHE_VERSION = 2

# What the list's status says (sync_status), in plain words: the window
# shows it (CP2 review N3: nothing heard these since the Feeds page went).
# Each one sentence, its cause and what to do or what MyEditor does
# (lessons 36): the window's one quiet line.
SYNCING = _("Reading your list of sources\u2026")      # under way: not shown
NOT_READ = _("Your list of sources couldn't be read online, so MyEditor shows the copy kept "
             "on this computer and tries again on its own.")
NOT_SHARED = _("Your latest changes to your sources are kept on this computer until the list "
               "can be saved online: MyEditor tries again on its own.")
# Why a list could not be read goes to the log, never into these words:
# it is English, and seldom something a person can act on (CP3 review L5).
UNREADABLE = _("Your list of sources couldn't be read: MyEditor shows the copy kept on this "
               "computer and tries again on its own.")
SIGNER_SILENT = _("Your signer app didn't answer, so your list of sources couldn't be read: "
                  "open it, and MyEditor tries again on its own.")
# A list written here that could not go out, by its cause (PublishResult):
# what was changed stays here and goes out with the next change.
TOO_LONG_TO_SAVE = _("Your list of sources is too long to be saved online: your changes stay "
                     "on this computer until you remove some sources.")
SIGNER_DID_NOT_SAVE = _("Your signer app didn't sign your list of sources: your changes stay "
                        "on this computer and go out with your next change.")
# Pauses before an unshared change is sent again, or a list that could
# not be read is read again, then every half hour.
RETRY_DELAYS_MS = (2 * 60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000)


_OLD_ENCRYPTION = _(
    "Your list of sources was saved with an older kind of encryption: open it once in the app "
    "that saved it, then choose Try Again.")
# Longer than the connection to the signer app carries (contract 12.5's
# caps; this list is one event): a connection made with the code goes over
# relays that carry it.
_TOO_LONG_FOR_CONNECTION = _(
    "Your list of sources is longer than the connection to your signer app carries: connect "
    "your signer app again, then choose Try Again.")


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
        self._cache_dir = Path(cache_dir) if cache_dir else _CACHE_DIR
        self._query = query if query is not None else (
            ListQuery(relay_pool, parent=self) if relay_pool is not None else None)
        self._publisher = publisher or self._default_publisher
        self._scheduler = scheduler or self._default_scheduler
        self._clock = clock or (lambda: int(_time.time()))
        self._debounce_ms = debounce_ms
        # The shared list, and this app's older one (only ever read): one
        # event each, in the format they have always had (not paged).
        lists = dict(
            decode=read_payload, encode=write_payload, merge=merge,
            words=ListWords(unreadable=UNREADABLE, signer_silent=SIGNER_SILENT,
                            old_encryption=_OLD_ENCRYPTION,
                            too_long_for_connection=_TOO_LONG_FOR_CONNECTION),
            session_pool=session_pool, relay_directory=relay_directory, query=self._query,
            publisher=self._publisher, clock=self._clock, entitled_relays=entitled_relays,
            kind=SUBSCRIPTIONS_KIND, on_status=self.sync_status.emit, parent=self)
        self._list = EncryptedList(d_tag=FEED_LIST_DTAG, name="of sources", **lists)
        self._legacy_list = EncryptedList(d_tag=LEGACY_FEED_LIST_DTAG,
                                          name="of sources (MyEditor's older one)", **lists)

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
        # The list could not be read (no answer, or not opened now): the
        # window offers Try Again. Reads again quietly after a pause that
        # grows, while waiting may help (lessons 36).
        self.read_failed = False
        self._rereads = 0
        self._cancel_reread: Optional[Callable[[], None]] = None
        # The next publish also sends read times (a manual source opened).
        self._send_read_times = False
        self._legacy_merged = False
        self._cancel_timer: Optional[Callable[[], None]] = None
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
        self._cancel_pending_reread()
        self._generation += 1
        self._profile = profile
        self._local, self._base, self._base_mark = FeedList(), None, None
        self._dirty = self._send_read_times = self._legacy_merged = False
        self._retries = self._rereads = 0
        self.read_failed = False
        self._known = False
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
        return self._cancel_timer is not None or self._list.busy

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
        self._cancel_pending_reread()      # asked now: this read is the one
        generation = self._generation
        self.sync_status.emit(SYNCING)

        def _on_relays(relays) -> None:
            if generation != self._generation:
                return
            self._list.read(profile, relays, lambda shared: _on_shared(relays, shared))

        def _on_shared(relays, shared: ListRead) -> None:
            if generation != self._generation:
                return
            if shared.status is ReadStatus.NO_ANSWER:
                # Read again quietly; until then the list is not known and
                # nothing is taken for missing.
                self.read_failed = True
                self.sync_status.emit(NOT_READ)
                self._reread_later()
                return
            if not shared.readable:
                # Its words are on the status line already. Waiting helps a
                # signer app that did not answer or failed; one that said
                # no, or an older list, waits for the person (Try Again).
                self.read_failed = True
                if shared.words in (SIGNER_SILENT, UNREADABLE):
                    self._reread_later()
            else:
                # Read: the line goes with the read of the older list below.
                self.read_failed, self._rereads = False, 0
            if isinstance(shared.value, FeedList):
                self._adopt(shared.value, shared.event)
            if shared.readable:
                # Answered: a change that waits here goes out now.
                self._send_waiting()
            if self._legacy_merged:
                if shared.readable:
                    self.sync_status.emit("")
                    self._mark_known()
                return
            self._legacy_list.read(profile, relays, lambda legacy: _on_legacy(legacy, shared))

        def _on_legacy(legacy: ListRead, shared: ListRead) -> None:
            if generation != self._generation:
                return
            if not legacy.readable:
                return  # tried again on the next launch, never marked done
            if isinstance(legacy.value, FeedList):
                self._merge_legacy(legacy.value)
            self._legacy_merged = True
            self._save_cache()
            if shared.readable:
                self.sync_status.emit("")
                self._mark_known()

        self._list.relays(profile, _on_relays, reading=True)

    # -- internals: merging --------------------------------------------------

    def _newer_than_base(self, event: Optional[dict]) -> bool:
        """Whether a list read from the relays is newer than the last one
        seen (NIP-01: the later time, then the lower id)."""
        mark = event_mark(event)
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
            self._base_mark = event_mark(event)
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
        if (self._dirty and not self._list.busy
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

    def retry_read(self) -> None:
        """Try Again: read the list now, and from the shortest pause again
        should it still not work."""
        self._rereads = 0
        self.refresh()

    def _reread_later(self) -> None:
        delay = RETRY_DELAYS_MS[min(self._rereads, len(RETRY_DELAYS_MS) - 1)]
        self._rereads += 1
        generation = self._generation

        def fired() -> None:
            self._cancel_reread = None
            if generation == self._generation:
                self.refresh()

        self._cancel_pending_reread()
        self._cancel_reread = self._scheduler(delay, fired)

    def _cancel_pending_reread(self) -> None:
        cancel, self._cancel_reread = self._cancel_reread, None
        if cancel is not None:
            cancel()

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

    # -- internals: publishing -------------------------------------------------

    def _publish_now(self) -> None:
        profile = self._profile
        if profile is None:
            return
        generation = self._generation
        # One write runs at a time per account (EncryptedList): asked
        # while one is on its way, this one goes right after it, with the
        # latest state. What this publish starts from, kept for the case the profile is
        # switched before it is done.
        snapshot = {"base": self._base, "local": self._local.copy(),
                    "legacy": self._legacy_merged, "reads": self._send_read_times,
                    "mark": self._base_mark}

        def current() -> bool:
            return generation == self._generation

        def _finish(status: Optional[str] = "", *, sent: bool = False) -> None:
            """``status`` None leaves the status line as it is."""
            if current():
                if not sent:
                    self._dirty = True
                else:
                    self._retries = 0
                self._save_cache()
                if status is not None:
                    self.sync_status.emit(status)
                # A write still on its way carries what changed since.
                going = self._list.is_writing(profile)
                if self._dirty and self._cancel_timer is None and sent and not going:
                    self._schedule_publish()
                elif self._dirty and not sent and status == NOT_SHARED and not going:
                    self._retry_later()
            if not self.is_busy:
                self.settled.emit()

        def _state():
            """What this publish merges: the store's state, or, after a
            switch to another profile, what it started from."""
            if current():
                return self._base, self._local, self._base_mark
            return snapshot["base"], snapshot["local"], snapshot["mark"]

        def _on_merged(merged: FeedList, remote: Optional[FeedList], event) -> None:
            if current():
                changed = merged != self._local
                self._local = merged
                if remote is not None:
                    self._base = remote
                    if event is not None:
                        self._base_mark = event_mark(event)
                if changed:
                    self.feeds_changed.emit()

        def _up_to_date(merged: FeedList, remote: FeedList) -> bool:
            # Everything here is on the relays already (read times travel
            # only along with a real change, or when asked to).
            return merged.same_sources(remote) and not snapshot["reads"]

        def _on_done(result) -> None:
            if result.status in (PublishStatus.UNREADABLE, PublishStatus.BLOCKED):
                # Never write over a list that could not be read, or not
                # opened now: it holds the other app's sources. The reason
                # is on the status line.
                _finish(None)
            elif result.status in (PublishStatus.NO_ANSWER, PublishStatus.NOT_ACCEPTED):
                # Nobody answered for the list (a newer one may be on the
                # relays), or no relay took it: the change waits (dirty,
                # cached).
                _finish(NOT_SHARED)
            elif result.status is PublishStatus.UP_TO_DATE:
                if current():
                    self._dirty = not self._local.same_sources(result.remote)
                _finish(sent=True)
            elif result.status is PublishStatus.FAILED:
                # Said by its cause, what was said only in the log (lessons
                # 36). A signer app that did not answer may yet: tried again
                # quietly, as for no relay taking it.
                _log.info("the list of sources was not saved online: %s", result.reason[:200])
                if result.cause == TOO_LONG:
                    _finish(TOO_LONG_TO_SAVE)
                elif is_signer_silent(result.reason):
                    _finish(NOT_SHARED)
                else:
                    _finish(SIGNER_DID_NOT_SAVE)
            else:
                _sent(result.value, result.signed)

        def _sent(content: FeedList, signed: dict) -> None:
            if current():
                self._base = content
                self._base_mark = event_mark(signed)
                self._send_read_times = False
                # Edits made while this was on its way go out next.
                self._dirty = not self._local.same_sources(content)
            else:
                self._write_cache(profile.user_pubkey, content, content, False,
                                  snapshot["legacy"], event_mark(signed))
            _finish(sent=True)

        self._list.publish(profile, state=_state, on_merged=_on_merged,
                           up_to_date=_up_to_date, on_done=_on_done)

    def _default_publisher(self, relays, signed, *, on_done, every_relay: bool = False) -> None:
        """Publish ``signed`` and answer once (list_relays.publish_once)."""
        publish_once(self._relay_pool, relays, signed, on_done=on_done, every_relay=every_relay)
