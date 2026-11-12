# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Imports, as the running app sees them.

One object owns everything the app does about imports for the active
account. The main window constructs it, tells it when the account
changes and asks it to flush on quit; the Imports window and the drafts
panel only show what it holds and forward what the person does:

- the list of sources shared with EINUNDZWANZIG STANDUP
  (imports/subscriptions.py);
- the account's inbox on this computer (imports/inbox_store.py), kept in
  step with that list and with the drafts the account has;
- the feed checker that runs while the app is open (imports/checker.py);
- the import jobs (imports/jobs.py) and the catalogue of what already
  exists (imports/catalogue.py);
- posts held only while they are shown: a source that is read when it is
  opened (imports/workspace.py, ``Collection``);
- the images the window shows (imports/remote_images.py), fetched
  through the same guard as everything else an import reads;
- the Imports window (nostr/ui/imports_window.py), made when first
  opened.

Nostr stays opt-in: until an account is in use, nothing is bound,
nothing is checked and nothing reaches the network. Leaving the account
(switching, signing out) stops the checker, pauses a running import and
closes the window; answers that arrive for the account left are dropped.

A second MyEditor process on the same account finds the inbox locked (a
lock file next to it). It shows the inbox but checks nothing and starts
no import, so two processes never sign the same import twice.

The inbox is a convenience kept on this computer, so it never stops the
app: a damaged file is set aside (renamed, never deleted) and a new one
started, and an inbox that cannot be opened at all (a folder that cannot
be written to) leaves imports unbound with ``problem`` saying why.
"""

from __future__ import annotations

import atexit
import errno
import logging
import os
import sqlite3
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import shiboken6
from PySide6.QtCore import QLockFile, QObject, QTimer, Signal

import url_safety
import user_dirs
from i18n import _

from .imports import snapshots, workers
from .imports.catalogue import ExistingCatalogue
from .imports.checker import FeedChecker, checks_automatically
from .imports.constants import IDENTIFIER_PREFIX
from .imports.errors import LOCAL_NETWORK_MESSAGE, SourceError, failure_words
from .imports.feed_list import source_key
from .imports.fulltext import extract_readable_content
from .imports.page_meta import fill_from_page, read_page_meta
from .imports.page_text import page_wanted, what_the_page_adds
from .imports.sources.ghost import needs_site_address
from .rss.parser import FeedItem
from .imports.intake import FEED, is_whole_source, kind_of, post_at, same_page
from .imports.sources.opml import OpmlFeed, write_opml
from .imports.subscriptions import SYNCING
from .imports.fetch import SourceFetcher
from .imports.inbox_store import (
    DRAFTED,
    NEW,
    OLDER,
    ROW_FAILED,
    ROW_PENDING,
    SKIPPED,
    Conflict,
    Counts,
    InboxStore,
    Job,
    SourceRow,
    View,
    database_path,
)
from .imports.intake import read_export
from .imports.jobs import ImportRunner, unfinished
from .imports.pipeline import ImportItemsJob
from .imports.registry import ResolveInput, resolve_source
from .imports.remote_images import RemoteImages
from .imports.sources.nostr import RelayQueryAdapter
from .imports.sources.opml import is_opml, parse_opml
from .imports.subscriptions import FeedSubscriptionStore
from .imports.workspace import Collection, Post, post_from_row
from .outbox import relays_from
from .preview import Article
from .publisher import DraftPublishJob
from .rss.normalize import item_to_article

_log = logging.getLogger(__name__)

ACCOUNT_CHANGED = _("The account changed: resume when you're signed in to that account "
                    "again.")
QUIT_PAUSED = _("MyEditor was closed during the import: resume it to go on.")
ELSEWHERE = _("Imports are checked in another MyEditor window. This one only shows them.")
ARTICLE_FAILED = _("Couldn't show this post.")
# Why imports cannot be kept here, in plain words, each with what to do
# (CP2 review N1: a damaged inbox was told to check that the user folder
# can be written to). The error itself goes to the log. The list of
# sources is not in the inbox, so it is kept whatever happens to it.
CANNOT_WRITE = _("MyEditor can't write to the folder where it keeps imports ({folder}). Check "
                 "that you can write to it, then choose Nostr > Imports again. Your sources are "
                 "kept.")
DISK_FULL = _("The disk is full, so MyEditor can't keep imports on this computer. Free some "
              "space, then choose Nostr > Imports again. Your sources are kept.")
IN_USE = _("Another program is using the imports kept on this computer. Choose Nostr > Imports "
           "again in a moment.")
DAMAGED_ELSEWHERE = _("The file that keeps imports on this computer is damaged, and MyEditor is "
                      "open a second time. Quit the other MyEditor, then choose Nostr > Imports "
                      "again.")
DAMAGED_HELD = _("The file that keeps imports on this computer is damaged, and another program "
                 "is using it, so MyEditor can't set it aside. Quit programs that might use "
                 "it, such as a backup or sync app, then choose Nostr > Imports again.")
CANNOT_OPEN = _("MyEditor can't open the imports kept on this computer. Choose Nostr > Imports "
                "again to try once more. Your sources are kept.")
SET_ASIDE = _("The file that keeps imports on this computer was damaged, so MyEditor set it "
              "aside and started a new one. Your sources are kept; posts you had skipped show "
              "again.")
NO_SOURCES_IN_LIST = _("No sources were found in that list. Is it a subscription export "
                       "from a feed reader?")
NOTHING_FOUND = _("Nothing to import was found at that address.")
PARTIAL_POST = _("This post is longer than MyEditor keeps here. Open Original reads all of "
                 "it, and a draft made of it gets the whole post from its page.")
READING_PAGE = _("Reading the whole post from its page…")
PAGE_UNREAD = _("Couldn't read the whole post from its page. Creating a draft tries again; "
                "if that fails too, the draft gets the text shown here.")
# Pages read for the article view, kept for posts opened again.
_PAGES_KEPT = 40
TRIMMED_POST = _("MyEditor no longer keeps the text of this post here. Open Original reads "
                 "it, and a draft made of it gets the post from its page.")
IMAGES_UNREADABLE = _("Couldn't read the images of these posts.")


class ImportsController(QObject):
    """Imports for the active account.

    Signals:
      bound_changed(bool)        an account's inbox was opened or closed
      sources_changed()          sources, their counts or their health changed
      posts_changed()            what a list shows may have changed
      inbox_count_changed(int)   posts waiting in the Inbox
      activity_changed()         an import job started, moved or ended
      new_posts(str, int)        a check put posts in the Inbox (source, count)
      import_finished(str)       an import went through all its posts (its id;
                                 some may have failed)
      icon_ready(str)            a site's icon is known now (C12)
    """

    bound_changed = Signal(bool)
    sources_changed = Signal()
    posts_changed = Signal()
    inbox_count_changed = Signal(int)
    activity_changed = Signal()
    new_posts = Signal(str, int)
    import_finished = Signal(str)
    sync_changed = Signal()
    icon_ready = Signal(str)

    def __init__(self, *, relay_pool, relay_directory, session_pool, draft_store=None,
                 entitled_relays: Optional[Callable[[], Sequence[str]]] = None,
                 blossom_primary: Callable[[], str] = lambda: "",
                 config_dir: Optional[Path] = None,
                 subscription_store: Optional[FeedSubscriptionStore] = None,
                 fetcher=None, images: Optional[RemoteImages] = None, icons=None,
                 item_job_factory=None, resend_factory=None,
                 checker_factory=None, catalogue_factory=None,
                 run_blocking=None, window_factory=None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        self._draft_store = draft_store
        self._entitled_relays = entitled_relays
        self._blossom_primary = blossom_primary
        self._config_dir = Path(config_dir) if config_dir else user_dirs.config_dir()
        self._fetcher = fetcher if fetcher is not None else SourceFetcher(self)
        # What the pages of posts opened in the article view added (page_text),
        # so a post opened again is not read again.
        self._pages_read: "OrderedDict[tuple, object]" = OrderedDict()
        self._run_blocking = run_blocking or (
            lambda fn, ok, err: workers.run_blocking(fn, ok, err, parent=self))
        self._item_job_factory = item_job_factory or self._make_item_job
        self._resend_factory = resend_factory or self._make_resend
        self._checker_factory = checker_factory or (
            lambda inbox: FeedChecker(store=inbox, fetcher=self._fetcher, parent=self))
        self._catalogue_factory = catalogue_factory or (
            lambda inbox: ExistingCatalogue(
                relay_pool=relay_pool, relay_directory=relay_directory,
                draft_store=draft_store, ledger=inbox.ledger_states,
                entitled_relays=entitled_relays, parent=self))
        self._window_factory = window_factory
        self._nostr_query = (RelayQueryAdapter(relay_pool, parent=self)
                             if relay_pool is not None else None)

        self.subscriptions = subscription_store or FeedSubscriptionStore(
            session_pool=session_pool, relay_pool=relay_pool,
            relay_directory=relay_directory, entitled_relays=entitled_relays,
            parent=self)
        self.subscriptions.feeds_changed.connect(self._sync_sources)
        # What the list of sources says about being read and shared, for
        # the window (CP2 review N3); a read under way says nothing.
        self.sync_message = ""
        self.subscriptions.sync_status.connect(self._on_sync_status)
        self.images = images or RemoteImages(parent=self)
        # Site icons (C12): none unless the app gives a cache, so nothing
        # is fetched for a controller a test makes.
        self.icons = icons
        if icons is not None:
            icons.ready.connect(self.icon_ready)
        self._sites: Dict[str, str] = {}
        self.sources_changed.connect(self._forget_sites)

        self._profile = None
        self.inbox: Optional[InboxStore] = None
        self.catalogue = None
        self.runner: Optional[ImportRunner] = None
        self.checker = None
        self._lock: Optional[QLockFile] = None
        self._exit_hook: Optional[Callable[[], None]] = None
        self.read_only = False
        # Read only, the other process's changes are looked for and its
        # lock tried every few seconds (CP1 review L7).
        self._watch = QTimer(self)
        self._watch.setInterval(self.WATCH_MS)
        self._watch.timeout.connect(self._watch_other)
        self._seen_version = None
        # Posts let go of their text when they have been kept long enough
        # (Q-9): at binding, then a few times a day.
        self._trim_timer = QTimer(self)
        self._trim_timer.setInterval(self.TRIM_MS)
        self._trim_timer.timeout.connect(self._trim_text)
        # Why imports could not be bound (a folder that cannot be written
        # to), and a one-time word for the window (a damaged file set aside).
        self.problem = ""
        self.notice = ""
        self._collections: Dict[str, Collection] = {}
        self._opened = 0                    # files and links opened, for their ids
        self._window = None
        self._reconcile_pending = False
        # Bumped whenever the account changes: an answer for the account
        # left behind is dropped.
        self._generation = 0

        if draft_store is not None:
            for signal in (draft_store.record_added, draft_store.record_changed,
                           draft_store.record_removed):
                signal.connect(self._schedule_reconcile)
            draft_store.loading_state_changed.connect(
                lambda loading: None if loading else self._schedule_reconcile())

    # ------------------------------------------------------------------ #
    # The account                                                          #
    # ------------------------------------------------------------------ #

    @property
    def profile(self):
        return self._profile

    @property
    def bound(self) -> bool:
        return self.inbox is not None

    def account_changed(self, profile) -> None:
        """The account in use changed; None when Nostr is not in use."""
        if (profile is not None and self._profile is not None
                and profile.user_pubkey.lower() == self._profile.user_pubkey.lower()):
            # The same account, signing another way now (a signer app
            # paired, or its key restored here): everything that signs or
            # decrypts for it follows, or the list of sources would keep
            # asking the signer it no longer has.
            self._profile = profile
            self.subscriptions.update_profile(profile)
            if self.runner is not None:
                self.runner.set_profile(profile)
            return
        self._unbind(ACCOUNT_CHANGED)
        self._generation += 1
        self._profile = profile
        if self.sync_message:
            self.sync_message = ""      # the previous account's
            self.sync_changed.emit()
        self.subscriptions.bind_profile(profile)
        if profile is not None:
            self._bind()

    @property
    def can_read_sources_again(self) -> bool:
        """The list of sources could not be read: Try Again reads it now."""
        return self.subscriptions.read_failed

    def read_sources_again(self) -> None:
        self.subscriptions.retry_read()

    def _on_sync_status(self, text: str) -> None:
        if text == SYNCING or text == self.sync_message:
            return
        if text:
            _log.warning("list of sources: %s", text)
        self.sync_message = text
        self.sync_changed.emit()

    def flush(self) -> None:
        """Quitting: send pending changes to the list of sources, pause a
        running import (it resumes where it was), stop checking, close
        the window."""
        self.subscriptions.flush()
        if self.runner is not None and self.runner.busy():
            self.runner.halt(QUIT_PAUSED)
        if self.checker is not None:
            self.checker.stop()
        self.close_window()

    def shutdown(self) -> None:
        """Quitting, after :meth:`flush` and the wait for the list of
        sources: close the inbox and give its lock back, so the next start
        (or another MyEditor) finds it free."""
        self._unbind(QUIT_PAUSED)

    def _bind(self) -> None:
        self.problem = self.notice = ""
        path = database_path(self._config_dir, self._profile.user_pubkey)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._cannot_bind(exc, path)
            return
        # A lock this process kept after the inbox could not be opened is
        # still its own: trying again (``retry``) goes on with it.
        if (self._lock is None or not self._lock.isLocked()) and not self._take_lock(path):
            return
        self._open(path)

    def _take_lock(self, path: Path) -> bool:
        """Take the inbox's lock, or learn that another MyEditor keeps it
        (``read_only``). False when no lock can be made at all."""
        self._lock = QLockFile(str(path.with_suffix(".lock")))
        # Held for the whole session: stale only when its process is gone,
        # so a lock left by a crash is taken over at the next start.
        self._lock.setStaleLockTime(0)
        self.read_only = not self._lock.tryLock(0)
        if self.read_only and self._lock.error() == QLockFile.LockError.PermissionError:
            # Not another process: the lock file cannot be made at all.
            self._lock = None
            self.read_only = self.subscriptions.read_only = False
            self._cannot_bind(PermissionError(errno.EACCES, "the lock file cannot be made",
                                              str(path.with_suffix(".lock"))), path)
            return False
        # The list of sources follows the lock: while another MyEditor keeps
        # it, that one writes the list, also when this one then cannot open
        # the inbox (CP3 review M1, and N1 of its fixes).
        self.subscriptions.read_only = self.read_only
        if self.read_only:
            _log.info("imports inbox in use by another process; showing it read-only")
        else:
            self._hold_lock()
        return True

    def _hold_lock(self) -> None:
        """The lock is this process's for the session. Given back at quit
        (shutdown); a process that ends without closing its window still
        gives it back before Python tears down: left to the lock's
        destructor, it went after other exit handlers, when its folder
        could already be gone."""
        self._exit_hook = _release_at_exit(weakref.ref(self))
        atexit.register(self._exit_hook)

    def _open(self, path: Path) -> None:
        """Open the inbox at ``path`` as the lock decided (``read_only``),
        a damaged one set aside once."""
        try:
            self._bind_inbox(path)
        except (sqlite3.Error, OSError) as exc:
            if not _is_damage(exc):
                self._give_up(exc, path)
                return
            if self.read_only:
                # The other process's file is left alone.
                self._give_up(exc, path, problem=DAMAGED_ELSEWHERE)
                return
            # Damaged: set aside (never deleted) and start a new inbox, once.
            self._drop_inbox()
            try:
                self._set_aside(path, exc)
            except OSError as held:
                self._give_up(held, path, problem=DAMAGED_HELD)
                return
            try:
                self.notice = SET_ASIDE
                self._bind_inbox(path)
            except (sqlite3.Error, OSError) as again:
                self._give_up(again, path)
                return
        if self.read_only:
            self._seen_version = self._data_version()
            self._watch.start()
        else:
            self._trim_timer.start()
        self.bound_changed.emit(True)
        self._announce()

    WATCH_MS = 3000
    TRIM_MS = 6 * 3600 * 1000

    def _trim_text(self) -> None:
        if self.inbox is None or self.read_only:
            self._trim_timer.stop()
            return
        try:
            trimmed = self.inbox.trim_text()
        except sqlite3.Error:
            _log.warning("could not let go of the text of old posts", exc_info=True)
            return
        if trimmed:
            self._announce()

    def _data_version(self):
        try:
            return self.inbox.data_version() if self.inbox is not None else None
        except sqlite3.Error:
            _log.warning("could not tell whether the other MyEditor changed the imports",
                         exc_info=True)
            return None

    def _watch_other(self) -> None:
        """Read only: show what the process that checks has changed, and
        keep the inbox when that process has quit (CP1 review L7: this one
        never refreshed, and stayed read only until it was restarted)."""
        if self.inbox is None or not self.read_only or self._lock is None:
            self._watch.stop()
            return
        if self._lock.tryLock(0):
            self._take_over()
            return
        version = self._data_version()
        if version is not None and version != self._seen_version:
            self._seen_version = version
            self._announce()

    def _take_over(self) -> None:
        """The other MyEditor quit: this one checks and imports now, with
        the lock it just took. The window stays open."""
        _log.info("the other MyEditor quit; this one keeps the imports inbox now")
        self._watch.stop()
        path = database_path(self._config_dir, self._profile.user_pubkey)
        self._drop_inbox()
        self.read_only = self.subscriptions.read_only = False
        self._hold_lock()
        # The list as the other MyEditor left it: syncing the inbox with
        # the one read when this one started would remove the sources it
        # followed since, and their posts with them.
        self.subscriptions.reload()
        self._open(path)

    def _bind_inbox(self, path: Path) -> None:
        """Everything binding does with the database, so a damaged file
        raises here and nowhere later in the binding (CP2 review N1: only
        the open was guarded, and a file with damaged pages raised out of
        the next step, and out of MainWindow.__init__ with it)."""
        self.inbox = InboxStore(path)
        self.inbox.check()
        if not self.read_only:
            self.inbox.recover_jobs()
            self.inbox.prune_jobs()
            self.inbox.trim_text()
        self.catalogue = self._catalogue_factory(self.inbox)
        self.runner = ImportRunner(
            store=self.inbox, catalogue=self.catalogue, profile=self._profile,
            item_job_factory=self._item_job_factory, resend_factory=self._resend_factory,
            parent=self)
        self.runner.job_changed.connect(lambda _id: self.activity_changed.emit())
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.draft_created.connect(self._on_draft_created)
        self.checker = self._checker_factory(self.inbox)
        self.checker.source_checked.connect(self._on_source_checked)
        self.checker.checking.connect(lambda *_args: self.sources_changed.emit())
        self._sync_sources(announce=False)
        self._reconcile_from_drafts()
        self.counts()       # the lists' numbers read once, here, where it is guarded
        if not self.read_only:
            self.checker.start()

    def _drop_inbox(self) -> None:
        """Undo what a binding that failed had made, the database closed."""
        if self.checker is not None:
            self.checker.stop()
        for owned in (self.checker, self.runner, self.catalogue):
            if isinstance(owned, QObject):
                owned.deleteLater()
        if self.inbox is not None:
            try:
                self.inbox.close()
            except sqlite3.Error:
                _log.warning("could not close the imports inbox", exc_info=True)
        self.inbox = self.catalogue = self.runner = self.checker = None

    def _set_aside(self, path: Path, exc: BaseException) -> None:
        """Rename a damaged inbox (and its journal) so a new one starts; the
        connection is closed already (_drop_inbox), as Windows requires."""
        aside = path.with_name(f"{path.name}.damaged-{int(time.time())}")
        _log.warning("imports inbox %s is damaged (%s); set aside as %s",
                     path, exc, aside.name)
        for suffix in ("", "-wal", "-shm"):
            part = Path(f"{path}{suffix}")
            if part.exists():
                part.replace(Path(f"{aside}{suffix}"))

    def _give_up(self, exc: BaseException, path: Path, *, problem: str = "") -> None:
        self._watch.stop()
        self._trim_timer.stop()
        self._drop_inbox()
        if self.read_only:
            self._release_lock()    # only tried: nothing of it is this one's
        # A lock this process holds stays its own until it unbinds: given
        # back here, a second MyEditor took it and wrote the list too, while
        # this one still did (S1 of the CP3 fixes review).
        self._cannot_bind(exc, path, problem=problem)

    def _cannot_bind(self, exc: BaseException, path: Path, *, problem: str = "") -> None:
        _log.warning("imports cannot be kept on this computer: %s", exc)
        self.inbox = None
        self.read_only = False
        self.notice = ""
        self.problem = problem or plain_problem(exc, path.parent)
        self.bound_changed.emit(False)

    def retry(self) -> bool:
        """Try once more to open the inbox after it could not be opened
        (Nostr > Imports, once the folder can be written to again or the
        program holding the file has quit). True when it is open now."""
        if self._profile is not None and self.inbox is None and self.problem:
            self._bind()
        return self.bound

    def _release_lock(self) -> None:
        if self._exit_hook is not None:
            atexit.unregister(self._exit_hook)
            self._exit_hook = None
        if self._lock is not None and not self.read_only:
            self._lock.unlock()
        self._lock = None

    def _unbind(self, reason: str) -> None:
        self.problem = self.notice = ""
        self.subscriptions.read_only = False    # the next binding asks the lock again
        self._watch.stop()
        self._trim_timer.stop()
        if self.inbox is None:
            self._release_lock()    # one kept after the inbox could not be opened
            return
        self.close_window()
        if self.runner is not None and self.runner.busy():
            self.runner.halt(reason)
        self.checker.stop()
        for owned in (self.checker, self.runner, self.catalogue):
            if isinstance(owned, QObject):
                owned.deleteLater()
        self.inbox.close()
        self.inbox = self.catalogue = self.runner = self.checker = None
        self._release_lock()
        self.read_only = False
        self._collections.clear()
        self.bound_changed.emit(False)
        self._announce()

    def _announce(self) -> None:
        self.sources_changed.emit()
        self.posts_changed.emit()
        self.activity_changed.emit()
        self.inbox_count_changed.emit(self.counts().inbox)

    # ------------------------------------------------------------------ #
    # Sources                                                              #
    # ------------------------------------------------------------------ #

    def _sync_sources(self, *, announce: bool = True) -> None:
        if self.inbox is None:
            return
        if self.read_only:
            # The process that checks keeps the inbox in step.
            if announce:
                self._announce()
            return
        # Until the list is known (this computer's copy read, or the
        # relays answered), a source missing from it is not removed: a
        # lost copy must never empty the inbox and its skips.
        self.inbox.sync_sources(
            ((source_key(feed.url), feed.url, feed.title, checks_automatically(feed.url))
             for feed in self.subscriptions.feeds),
            remove_missing=self.subscriptions.known)
        for collection_id in [cid for cid, c in self._collections.items()
                              if c.kind == "manual"
                              and not self.subscriptions.has_feed(c.source_url)]:
            del self._collections[collection_id]
        if announce:
            self._announce()

    def sources(self) -> List[SourceRow]:
        return self.inbox.sources() if self.inbox is not None else []

    def source(self, key: str) -> Optional[SourceRow]:
        return self.inbox.source(key) if self.inbox is not None else None

    def site_icon(self, source_key: str = "", url: str = ""):
        """The site icon of a source (or of the site ``url`` is on) when it
        is known; the first ask looks it up in the background, and
        ``icon_ready`` says when it is there. None without one."""
        if self.icons is None:
            return None
        if source_key:
            if not self._sites and self.inbox is not None:
                self._sites = {s.key: s.site_url or s.url for s in self.inbox.sources()}
            url = self._sites.get(source_key) or url
        return self.icons.icon(url) if url else None

    def _forget_sites(self) -> None:
        self._sites = {}

    def website_of(self, key: str) -> str:
        """A source's website: the one its feed names, else its address's
        start page (review L4: not the feed's own address)."""
        source = self.source(key)
        if source is None:
            return ""
        if source.site_url:
            return source.site_url
        return url_safety.origin_of(source.url) or source.url

    def sources_as_opml(self) -> str:
        """Every source as an OPML file (Export Sources): what other feed
        readers import, and EINUNDZWANZIG STANDUP too. Sources that are
        not feeds go along, so the file brings back the whole list."""
        feeds = []
        for source in self.sources():
            site = self.website_of(source.key)
            feeds.append(OpmlFeed(
                xml_url=source.url, title=source.display_title,
                html_url=site if url_safety.is_safe_external_url(site) else None))
        return write_opml(feeds, title=_("MyEditor Sources"))

    def settings_of(self, key: str) -> Optional["SourceSettings"]:
        """What Source Settings shows for ``key``."""
        source = self.source(key)
        if source is None:
            return None
        followed = self.subscriptions.get(source.url)
        copy_images, full_text = self._source_defaults(key)
        return SourceSettings(
            key=key, url=source.url, name=followed.title if followed else source.title,
            own_title=source.feed_title, website=self.website_of(key),
            automatic=source.automatic, checking=not source.paused,
            copy_images=copy_images, full_text=full_text)

    def save_settings(self, key: str, *, name: str, checking: bool, copy_images: bool,
                      full_text: bool) -> None:
        """Keep what Source Settings says. The name and the defaults for new
        drafts go with the list of sources (the other devices, and
        EINUNDZWANZIG STANDUP, use them too); whether the source is
        checked automatically stays on this computer."""
        source = self.source(key)
        if source is None or self.read_only:
            return
        self.subscriptions.update_title(source.url, name.strip())
        self.subscriptions.set_source_options(source.url, rehost_images=copy_images,
                                              fetch_full_text=full_text)
        if source.automatic and source.paused == checking:
            self.inbox.set_paused(key, not checking)
            self._announce()

    def check_now(self, key: str) -> bool:
        """The person asked to check a source now (once a minute at most)."""
        source = self.source(key)
        if source is None or self.read_only:
            return False
        if not source.automatic:
            self.read_source(source.url)
            return True
        started = self.checker.check_now(key)
        self.sources_changed.emit()
        return started

    def is_checking(self, key: str) -> bool:
        if self.checker is not None and self.checker.is_checking(key):
            return True
        collection = self._collections.get(_manual_id(key))
        return collection is not None and collection.loading

    def _on_source_checked(self, key: str, new: int) -> None:
        self._announce()
        if new:
            self._reconcile_new_posts(key)
            self.new_posts.emit(key, new)

    # ------------------------------------------------------------------ #
    # Lists                                                                #
    # ------------------------------------------------------------------ #

    def counts(self) -> Counts:
        return self.inbox.counts() if self.inbox is not None else Counts()

    def page(self, view: View, *, query: str = "", after: Optional[Post] = None) -> List[Post]:
        """One page of a stored list, after the post ``after``."""
        if self.inbox is None:
            return []
        cursor = None
        if after is not None:
            cursor = InboxStore.cursor_of(after.found_at, after.published_at,
                                          after.source_key, after.d_tag)
        return [post_from_row(row)
                for row in self.inbox.page(view, query=query, after=cursor)]

    def item(self, post: Post):
        """The full post (its body too) behind a list row."""
        if post.collection:
            collection = self._collections.get(post.collection)
            return collection.item(post.d_tag) if collection else None
        if self.inbox is None:
            return None
        return self.inbox.item(post.source_key, post.d_tag)

    def collection(self, collection_id: str) -> Optional[Collection]:
        return self._collections.get(collection_id)

    def collection_of(self, key: str) -> Optional[Collection]:
        """The posts read of a source that is read when it is opened."""
        return self._collections.get(_manual_id(key))

    def read_source(self, url: str) -> Optional[Collection]:
        """Read a source that is not checked on its own (a Nostr author, a
        sitemap, a GitHub folder): now, while it is shown."""
        if self.inbox is None:
            return None
        key = source_key(url)
        collection = self._collections.get(_manual_id(key))
        if collection is None:
            collection = Collection(id=_manual_id(key), kind="manual", label=url,
                                    source_url=url, source_key=key)
            self._collections[collection.id] = collection
        source = self.inbox.source(key)
        if source is not None:
            collection.label = source.display_title
        collection.loading, collection.error = True, ""
        generation = self._generation
        self.posts_changed.emit()
        self.sources_changed.emit()

        def done(result) -> None:
            if generation != self._generation or self._collections.get(
                    collection.id) is not collection:
                return
            collection.loading = False
            collection.items = list(result.feed.items)
            # Remembered with the list, and sent with its next real change:
            # reading a source never asks the signer by itself.
            self.subscriptions.mark_fetched(url)
            if not _title_known(source) and result.feed.title:
                collection.label = result.feed.title
                if not self.read_only:      # the other process keeps the inbox
                    self.inbox.set_site(key, feed_title=result.feed.title,
                                        site_url=result.feed.link or "")
            self._look_up_states(collection)
            self.sources_changed.emit()

        def failed(error) -> None:
            if generation != self._generation or self._collections.get(
                    collection.id) is not collection:
                return
            collection.loading = False
            collection.error = failure_words(error)
            self.posts_changed.emit()
            self.sources_changed.emit()

        resolve_source(ResolveInput(url=url), fetcher=self._fetcher, on_success=done,
                       on_failure=failed, run_blocking=self._run_blocking,
                       nostr_query=self._nostr_query, relay_directory=self._relay_directory)
        return collection

    def _look_up_states(self, collection: Collection) -> None:
        """Which of a collection's posts are imported already."""
        tags = [p.d_tag for p in collection.posts()]
        for d_tag in tags:
            local = self.catalogue.known_locally(d_tag) if self.catalogue else None
            if local:
                collection.states[d_tag] = local
        self.posts_changed.emit()
        if not tags or self.catalogue is None:
            return
        generation = self._generation

        def found(existing) -> None:
            if generation != self._generation:
                return
            collection.states.update(existing.states)
            self.posts_changed.emit()

        # Unavailable, the posts simply show no state; Create checks again
        # and fails closed. Said in the log, never silent (review L8).
        self.catalogue.look_up(self._profile, tags, on_ready=found,
                               on_unavailable=lambda reason: _log.warning(
                                   "could not ask which posts of %s exist already: %s",
                                   collection.label[:80], reason))

    # ------------------------------------------------------------------ #
    # Following, files and links                                           #
    # ------------------------------------------------------------------ #

    def look_up(self, address: str, *,
                on_done: Callable[[Optional[object], str], None]) -> None:
        """Read what an address holds, for a sheet: ``on_done(result, "")``
        or ``on_done(None, words for the person)``."""
        self._resolve(ResolveInput(url=address), on_done)

    def look_up_paste(self, text: str, address: str, *,
                      on_done: Callable[[Optional[object], str], None]) -> None:
        """Read pasted feed text; ``address`` makes its relative links whole."""
        self._resolve(ResolveInput(url=address.strip() or None, pasted_body=text), on_done)

    def _resolve(self, request: ResolveInput, on_done) -> None:
        generation = self._generation

        def answer(result, error: str) -> None:
            if generation == self._generation:
                on_done(result, error)

        resolve_source(
            request, fetcher=self._fetcher,
            on_success=lambda result: answer(result, ""),
            on_failure=lambda error: answer(None, failure_words(error)),
            run_blocking=self._run_blocking, nostr_query=self._nostr_query,
            relay_directory=self._relay_directory)

    def read_list(self, url: str, *,
                  on_done: Callable[[Optional[object], str], None]) -> None:
        """Read a list of sources (OPML) at ``url``: ``on_done(document,
        "")`` or ``on_done(None, words for the person)``."""
        generation = self._generation

        def read(text: str) -> None:
            if generation != self._generation:
                return
            document = parse_opml(text) if is_opml(text) else None
            if document is None or not document.feeds:
                on_done(None, NO_SOURCES_IN_LIST)
            else:
                on_done(document, "")

        def failed(error) -> None:
            if generation == self._generation:
                on_done(None, failure_words(error))

        self._fetcher.fetch(url, on_success=read, on_failure=failed)

    def is_followed(self, url: str) -> bool:
        return self.subscriptions.has_feed(url)

    def why_not_followed(self, url: str) -> str:
        """Why ``url`` cannot be followed, in the words the person reads:
        an address on this computer or its network (Q-6), else none."""
        return LOCAL_NETWORK_MESSAGE if not _public(url) else ""

    def follow(self, url: str, title: str = "", *, result=None) -> str:
        """Follow ``url``. With the ``result`` its lookup gave, its posts are
        there at once: a feed's as a baseline (Older Posts, as the first
        check would make them), another source's as read now. Returns the
        source's key, or "" when it could not be followed."""
        if not _public(url) or self.read_only:
            return ""
        added = self.subscriptions.add_feed(url, title)
        key = source_key(url)
        if not added.get("added") and not added.get("duplicate"):
            return ""
        if added.get("added") and result is not None and self.inbox is not None:
            source = self.inbox.source(key)
            if source is not None and source.automatic and not self.read_only:
                self.inbox.ingest(
                    key, list(result.feed.items), final_url=result.url,
                    feed_title=result.feed.title or "", site_url=result.feed.link or "")
                self._reconcile_new_posts(key, state=OLDER)
            elif source is not None and not source.automatic:
                collection = Collection(id=_manual_id(key), kind="manual",
                                        label=source.display_title or result.feed.title or url,
                                        source_url=url, source_key=key,
                                        items=list(result.feed.items))
                self._collections[collection.id] = collection
                self._look_up_states(collection)
            self._announce()
        return key

    def unfollow(self, key: str) -> bool:
        """Stop following a source. Its posts leave the inbox; the drafts
        made from them, and the record of what was imported, stay."""
        source = self.source(key)
        if source is None or self.read_only:
            return False
        return self.subscriptions.remove_feed(source.url)

    def follow_all(self, document) -> Tuple[int, int, int]:
        """Follow every source of a list (OPML): how many were followed
        now, were followed already, and could not be followed (not a
        source, or an address on this computer or its network)."""
        followed = known = refused = 0
        public = [feed for feed in document.feeds if _public(feed.xml_url)]
        refused = len(document.feeds) - len(public)
        # One change of the list for all of them (CP2 review L-d: one sync
        # of the whole list per source, 3.8 s for 400 on the UI thread).
        for added in self.subscriptions.add_feeds(
                [(feed.xml_url, feed.title) for feed in public]):
            if added.get("added"):
                followed += 1
            elif added.get("duplicate"):
                known += 1
            else:
                refused += 1
        return followed, known, refused

    def open_link(self, address: str, *,
                  on_done: Callable[[Optional[Collection], str], None]) -> None:
        """Import a Link: the posts at ``address``, held to import once.
        An article's page is one post (CP2 review N5: the site's whole
        feed opened, every post checked): the post of the site's feed
        whose page it is, else the page itself, read as one post. A feed,
        a site's start page or a thread opens whole."""
        generation = self._generation

        def found(result, error: str) -> None:
            if generation != self._generation:
                return
            if result is None or not result.feed.items:
                on_done(None, error or NOTHING_FOUND)
                return
            items = list(result.feed.items)
            article = (kind_of(address) == FEED and not is_whole_source(address)
                       and not same_page(result.url or "", address))
            if not article:
                on_done(self.open_posts(kind="link", label=result.feed.title or address,
                                        items=items, source_url=result.url or address), "")
                return
            post = post_at(items, address)
            if post is not None:
                on_done(self.open_posts(kind="link", label=post.title or address,
                                        items=[post], source_url=address), "")
                return
            self._read_page(address, generation, on_done)

        self.look_up(address, on_done=found)

    def _read_page(self, address: str, generation: int,
                   on_done: Callable[[Optional[Collection], str], None]) -> None:
        """A page that is in no feed, read as one post (its main text, as
        Fetch the Full Article reads one)."""
        def read(html: str) -> None:
            self._run_blocking(
                lambda: (extract_readable_content(html, url=address),
                         read_page_meta(html, url=address)),
                lambda found: made(*found),
                failed)

        def made(content, page) -> None:
            if generation != self._generation:
                return
            if content is None:
                on_done(None, NOTHING_FOUND)
                return
            # The details the page states (cover, description, date, tags,
            # author), as a feed would have given them.
            post = fill_from_page(
                FeedItem(guid=address, title=address, link=address,
                         summary=None, content_html=content.html, published_at=None,
                         categories=(), image=None, author=None, title_from_url=True),
                page, page_title=content.title)
            on_done(self.open_posts(kind="link", label=post.title, items=[post],
                                    source_url=address), "")

        def failed(reason) -> None:
            # A page that could not be fetched says why; one that could not
            # be read has nothing to import, what went wrong in the log.
            if generation == self._generation:
                if isinstance(reason, SourceError):
                    on_done(None, failure_words(reason))
                else:
                    _log.info("the page at an address could not be read: %s", str(reason)[:200])
                    on_done(None, NOTHING_FOUND)

        self._fetcher.fetch(address, on_success=read, on_failure=failed)

    def open_posts(self, *, kind: str, label: str, items, source_url: str = "") -> Collection:
        """Posts of a file or a link, held while shown, to import once."""
        self._opened += 1
        collection = Collection(id=f"{kind}-{self._opened}", kind=kind, label=label,
                                source_url=source_url, items=list(items))
        self._collections[collection.id] = collection
        self._look_up_states(collection)
        self.sources_changed.emit()
        return collection

    def read_file(self, path: str, *,
                  on_done: Callable[[Optional[Collection], Optional[object], str], None],
                  site_url: Optional[str] = None,
                  on_site_needed: Optional[Callable[[], None]] = None,
                  ) -> None:
        """Read an export file: ``on_done(collection, None, "")`` with its
        posts, ``on_done(None, opml_document, "")`` for a list of sources,
        or ``on_done(None, None, words for the person)``.

        An export that writes its blog's address as a placeholder (Ghost's
        ``__GHOST_URL__``) needs the real one for its pictures and links:
        without ``site_url``, ``on_site_needed()`` is called instead, and
        the file is read again with the address the person gives ("" for
        none)."""
        generation = self._generation

        def read(export) -> None:
            if generation != self._generation:
                return
            if export.sources is not None:
                on_done(None, export.sources, "")
            elif export.items:
                on_done(self.open_posts(kind="file", label=export.label, items=export.items,
                                        source_url=export.label), None, "")
            elif site_url is None and on_site_needed is not None \
                    and needs_site_address(export.text):
                on_site_needed()
            else:
                self.look_up_paste(export.text, site_url or "", on_done=lambda result, error: (
                    on_done(self.open_posts(kind="file", label=export.label,
                                            items=result.feed.items, source_url=export.label),
                            None, "") if result is not None else on_done(None, None, error)))

        def failed(exc) -> None:
            # read_export says what to do as a ValueError's words; anything
            # else is said by the one helper (lessons 36).
            if generation == self._generation:
                on_done(None, None, str(exc) if type(exc) is ValueError
                        else failure_words(exc))

        self._run_blocking(lambda: read_export(path), read, failed)

    def close_collection(self, collection_id: str) -> None:
        """Take a file or a link off the list (its drafts stay)."""
        if self._collections.pop(collection_id, None) is not None:
            self.sources_changed.emit()

    def collections(self, *kinds: str) -> List[Collection]:
        return [c for c in self._collections.values() if not kinds or c.kind in kinds]

    # ------------------------------------------------------------------ #
    # What is imported                                                     #
    # ------------------------------------------------------------------ #

    def _schedule_reconcile(self, *_args) -> None:
        if self._reconcile_pending or self.inbox is None:
            return
        self._reconcile_pending = True
        QTimer.singleShot(0, self, self._reconcile_from_drafts)

    def _reconcile_from_drafts(self) -> None:
        """Posts with a draft are Imported, also posts drafted elsewhere."""
        self._reconcile_pending = False
        store = self._draft_store
        if self.inbox is None or store is None or self._profile is None or self.read_only:
            return
        if store.profile_pubkey != self._profile.user_pubkey.lower():
            return
        drafted = [record.identifier for record in store.all()]
        changed = self.inbox.reconcile(drafted=drafted, removed=store.deleted_identifiers())
        for collection in self._collections.values():
            for d_tag in drafted:
                collection.states.setdefault(d_tag, DRAFTED)
        if changed:
            self._announce()

    def reconcile_inbox(self) -> None:
        """Ask the relays which Inbox posts were imported by another app."""
        if self.inbox is not None:
            self._reconcile_tags(self.inbox.d_tags((NEW, SKIPPED))[:500])

    def _reconcile_new_posts(self, key: str, state: str = NEW) -> None:
        tags = [p.d_tag for p in self.inbox.page(View("source", key, state), limit=500)]
        self._reconcile_tags(tags)

    def _reconcile_tags(self, tags: List[str]) -> None:
        if not tags or self.catalogue is None or self.read_only:
            return
        inbox, generation = self.inbox, self._generation

        def found(existing) -> None:
            if generation != self._generation or self.inbox is not inbox:
                return
            by_state: Dict[str, list] = {}
            for d_tag, state in existing.states.items():
                by_state.setdefault(state, []).append(d_tag)
            if by_state and inbox.reconcile(**by_state):
                self._announce()

        self.catalogue.look_up(
            self._profile, tags, on_ready=found,
            on_unavailable=lambda reason: _log.warning(
                "could not ask which posts exist already: %s", reason))

    # ------------------------------------------------------------------ #
    # One post, as it will be published                                    #
    # ------------------------------------------------------------------ #

    def prepare_article(self, post: Post, *, on_ready: Callable[[str, Article], None],
                        on_failed: Callable[[str], None]) -> None:
        """The post as the article an import of it publishes: its Markdown
        and the details around it, made off the UI thread.

        When the import would read the post's page (its source gave a
        teaser or a link to it; page_text.py), the page is read here too
        and the article shown again with what the page added, so what is
        shown is what the draft gets. ``on_ready`` is then called twice:
        first with the source's text and a line saying the page is read."""
        item = self.item(post)
        if item is None:
            on_failed(ARTICLE_FAILED)
            return
        author = getattr(self._profile, "display_name", "") or ""
        generation = self._generation
        full_text = self._source_defaults(post.source_key)[1]

        def show(template, *, reading: bool = False, whole: bool = False,
                 unread: bool = False) -> None:
            if generation != self._generation:
                return
            content = template.content
            if reading:
                content = "*" + READING_PAGE + "*\n\n" + content
            elif unread:
                content = "*" + PAGE_UNREAD + "*\n\n" + content
            elif post.trimmed and not whole:
                # Nothing of the body is left: the word first, where the
                # body would be, then where the post is.
                content = "*" + TRIMMED_POST + "*\n\n" + content
            elif item.partial and not whole:
                content += "\n\n*" + PARTIAL_POST + "*"
            on_ready(content, Article(
                title=template.title, summary=template.summary,
                image=template.image or snapshots.cover(item), tags=template.hashtags,
                author=author, published_at=template.published_at or 0))

        def made(template) -> None:
            if generation != self._generation:
                return
            wants_text, wants_details = page_wanted(item, template, fetch_full_text=full_text)
            if not (wants_text or wants_details):
                show(template)
                return
            show(template, reading=wants_text)
            self._add_from_page(item, template, wants_text, generation,
                                on_done=lambda added: shown_with(template, added, wants_text))

        def shown_with(template, added, wants_text: bool) -> None:
            if added is None:
                # The page could not be read (blocked, empty, gone) or added
                # nothing: the reader is told when its text was wanted.
                show(template, unread=wants_text and not item.partial and not post.trimmed)
                return
            page_item, body = added
            remade = item_to_article(replace(page_item, content_markdown=body),
                                     identifier_prefix=IDENTIFIER_PREFIX)
            show(replace(remade, slug=template.slug), whole=body != template.body)

        def failed(exc) -> None:
            _log.warning("could not prepare an imported post: %s", exc)
            if generation == self._generation:
                on_failed(ARTICLE_FAILED)

        self._run_blocking(lambda: item_to_article(item, identifier_prefix=IDENTIFIER_PREFIX),
                           made, failed)

    def _add_from_page(self, item: FeedItem, template, wants_text: bool, generation: int, *,
                       on_done: Callable[[Optional[tuple]], None]) -> None:
        """Read ``item``'s page and answer what it adds (page_text), or
        None; a page read before for this view is not read again. A page
        that gave nothing is asked again next time: a site that turned
        the reader away (too many requests) may answer then."""
        key = (item.guid, item.link, wants_text)
        if key in self._pages_read:
            self._pages_read.move_to_end(key)
            on_done(self._pages_read[key])
            return

        def keep(added) -> None:
            if added is not None:
                self._pages_read[key] = added
                while len(self._pages_read) > _PAGES_KEPT:
                    self._pages_read.popitem(last=False)
            if generation == self._generation:
                on_done(added)

        def read(html: str) -> None:
            self._run_blocking(
                lambda: what_the_page_adds(item, template.body, html, wants_text=wants_text),
                keep, lambda exc: keep(None))

        self._fetcher.fetch(item.link, on_success=read,
                            on_failure=lambda _error: on_done(None)
                            if generation == self._generation else None)

    # ------------------------------------------------------------------ #
    # Creating drafts                                                      #
    # ------------------------------------------------------------------ #

    def defaults_for(self, posts: Sequence[Post]) -> Tuple[Optional[bool], Optional[bool]]:
        """What the sources of ``posts`` choose for new drafts: copy images,
        fetch the full article. Each is None when the sources differ.

        Asked on every change of the checks, so each source is read once,
        from the list of sources in memory (CP2 review N2: two database
        queries a checked post made one key press 312 ms at 2,000 checked
        posts)."""
        sources = {(post.source_key, post.source_url) for post in posts}
        found = {self._defaults_of(key, url) for key, url in sources} or {(True, True)}
        copy = {pair[0] for pair in found}
        full = {pair[1] for pair in found}
        return (copy.pop() if len(copy) == 1 else None,
                full.pop() if len(full) == 1 else None)

    def _defaults_of(self, key: str, url: str) -> Tuple[bool, bool]:
        """A followed source's defaults for new drafts; a post of no followed
        source (a file, a link) gets both."""
        if not key or not url or not self.subscriptions.has_feed(url):
            return True, True
        options = self.subscriptions.options_for(url)
        return options.rehost_images, options.fetch_full_text

    def _source_defaults(self, key: str) -> Tuple[bool, bool]:
        source = self.source(key) if key else None
        if source is None:
            return True, True
        options = self.subscriptions.options_for(source.url)
        return options.rehost_images, options.fetch_full_text

    def signer_prompts(self, posts: Sequence[Post], *, copy_images: bool) -> int:
        """At most how often a signer app asks while ``posts`` become drafts:
        twice a draft (it encrypts the draft, then signs it: CP2 review N7,
        once was half the truth), and once an image copied, the cover
        included. 0 when the key is kept here."""
        if self._profile is None or getattr(self._profile, "signer", "remote") == "local":
            return 0
        images = sum(post.image_count + (1 if post.image else 0) for post in posts)
        return 2 * len(posts) + (images if copy_images else 0)

    def can_create(self) -> bool:
        """Whether drafts can be created now (one import at a time)."""
        return self.runner is not None and not self.read_only and not self.runner.busy()

    def create_drafts(self, posts: Sequence[Post], *, label: str,
                      choices: Optional[dict] = None, skip_image_urls=()) -> str:
        """Start making drafts of ``posts``. ``choices`` are the person's for
        this run (rehost_images, fetch_full_text); where it says nothing,
        each source's defaults apply. Returns the import's id, or "" when
        none could start."""
        if not self.can_create():
            return ""
        pairs, by_source, collections = [], {}, set()
        for post in posts:
            item = self.item(post)
            if item is None or not post.d_tag:
                continue
            pairs.append((item, post.source_key))
            collections.add(post.collection)
            if post.source_key and post.source_key not in by_source:
                copy, full = self._source_defaults(post.source_key)
                by_source[post.source_key] = {"rehost_images": copy, "fetch_full_text": full}
        if not pairs:
            return ""
        collection = (self._collections.get(next(iter(collections)))
                      if len(collections) == 1 else None)
        options = {"by_source": by_source, **(choices or {})}
        if skip_image_urls:
            options["skip_image_urls"] = sorted(skip_image_urls)
        job = self.runner.create(
            label=label, source_type=collection.kind if collection else "inbox",
            posts=pairs, source_url=collection.source_url if collection else "",
            options=options)
        self.runner.run(job.id)
        self.activity_changed.emit()
        self.posts_changed.emit()
        return job.id

    def images_of(self, posts: Sequence[Post], *,
                  on_ready: Callable[[List[str]], None],
                  on_failed: Optional[Callable[[str], None]] = None) -> None:
        """Every image an import of ``posts`` would copy, for the review
        (found off the UI thread: it converts each post). A failure says
        so (review L8: it read as "no image to copy")."""
        items = [item for item in map(self.item, posts) if item is not None]
        generation = self._generation

        def found(images) -> None:
            if generation == self._generation:
                on_ready(list(images))

        def failed(exc) -> None:
            _log.warning("could not find the images of %d posts: %s", len(items), exc)
            if generation == self._generation and on_failed is not None:
                on_failed(IMAGES_UNREADABLE)

        self._run_blocking(lambda: snapshots.images_to_copy(items), found, failed)

    # ------------------------------------------------------------------ #
    # Imports                                                              #
    # ------------------------------------------------------------------ #

    def _on_job_finished(self, job_id: str) -> None:
        self._announce()
        job = self.inbox.job(job_id) if self.inbox is not None else None
        if job is not None and job.status in ("completed", "partial"):
            self.import_finished.emit(job_id)

    # ------------------------------------------------------------------ #
    # Progress                                                             #
    # ------------------------------------------------------------------ #

    def activity(self) -> Optional[Job]:
        """The import to show: the one running, else the newest one that
        still has posts to do (paused, or some failed)."""
        if self.runner is None:
            return None
        if self.runner.running is not None:
            return self.runner.running
        waiting = unfinished(self.inbox.jobs(statuses=UNFINISHED, payloads=False))
        return waiting[0] if waiting else None

    def job(self, job_id: str) -> Optional[Job]:
        if self.runner is not None and self.runner.running is not None \
                and self.runner.running.id == job_id:
            return self.runner.running
        return self.inbox.job(job_id) if self.inbox is not None else None

    def posts_in_progress(self) -> Tuple[Set[str], Set[str]]:
        """The posts an unfinished import still holds (they cannot be
        chosen again), and those that failed in one (they can)."""
        busy: Set[str] = set()
        failed: Set[str] = set()
        if self.inbox is None:
            return busy, failed
        jobs = unfinished(self.inbox.jobs(statuses=UNFINISHED, payloads=False))
        running = self.runner.running if self.runner is not None else None
        if running is not None:
            jobs = [running] + [job for job in jobs if job.id != running.id]
        for job in jobs:
            for row in job.rows:
                if row.status == ROW_PENDING:
                    busy.add(row.d_tag)
                elif row.status == ROW_FAILED:
                    failed.add(row.d_tag)
        return busy, failed - busy

    def pause_import(self) -> None:
        """Pause after the post being made."""
        if self.runner is not None:
            self.runner.pause()

    def resume_import(self, job_id: str) -> bool:
        """Go on with an import, or try its failed posts again."""
        if self.runner is None or self.read_only:
            return False
        started = self.runner.run(job_id)
        self._announce()
        return started

    def stop_import(self, job_id: str) -> None:
        """Stop an import for good; the drafts made stay."""
        if self.runner is not None:
            self.runner.stop_paused(job_id)
            self._announce()

    # ------------------------------------------------------------------ #
    # Skipping                                                             #
    # ------------------------------------------------------------------ #

    def skip(self, posts: Sequence[Post]) -> List[Tuple[str, str, int]]:
        """Set posts aside on this computer. Returns what moved, for Undo:
        (source, post, its new revision) each."""
        moved = []
        if self.inbox is None or self.read_only:
            return moved
        for post in posts:
            if not post.skippable:
                continue
            try:
                revision = self.inbox.skip(post.source_key, post.d_tag, post.revision)
            except Conflict:
                continue        # imported or changed meanwhile: leave it
            moved.append((post.source_key, post.d_tag, revision))
        if moved:
            self._announce()
        return moved

    def restore(self, posts: Sequence[Post]) -> List[Tuple[str, str, int]]:
        """Bring skipped posts back where they were."""
        return self._restore([(post.source_key, post.d_tag, post.revision)
                              for post in posts if post.skipped])

    def undo_skip(self, moved: Sequence[Tuple[str, str, int]]) -> int:
        """Undo a skip: what :meth:`skip` returned goes back. Returns how
        many posts did (one changed since stays as it is)."""
        return len(self._restore(moved))

    def _restore(self, moves) -> List[Tuple[str, str, int]]:
        restored = []
        if self.inbox is None or self.read_only:
            return restored
        for source_key_, d_tag, revision in moves:
            try:
                restored.append((source_key_, d_tag,
                                 self.inbox.restore(source_key_, d_tag, revision)))
            except Conflict:
                continue
        if restored:
            self._announce()
        return restored

    def _on_draft_created(self, d_tag: str) -> None:
        """A post became a draft: it is Imported everywhere it is shown."""
        for collection in self._collections.values():
            if collection.item(d_tag) is not None:
                collection.states[d_tag] = DRAFTED
        self._announce()

    def _make_item_job(self, *, items, feed_url, fetch_full_text, rehost_images,
                       skip_image_urls, is_imported, parent):
        store = self._draft_store
        return ImportItemsJob(
            items=items, feed_url=feed_url, profile=self._profile,
            relay_pool=self._relay_pool, relay_directory=self._relay_directory,
            session_pool=self._session_pool,
            # One fetcher for every post, not a network manager each.
            fetcher=self._fetcher,
            identifier_exists=(lambda d: d in store) if store is not None else None,
            is_imported=is_imported, fetch_full_text=fetch_full_text,
            rehost_images=rehost_images,
            blossom_server=self._blossom_primary() if rehost_images else "",
            skip_image_urls=skip_image_urls,
            entitled_relays=relays_from(self._entitled_relays), parent=parent)

    def _make_resend(self, *, identifier, parent):
        return DraftPublishJob(
            relay_pool=self._relay_pool, relay_directory=self._relay_directory,
            session_pool=self._session_pool, profile=self._profile, inner_event=None,
            identifier=identifier, entitled_relays=relays_from(self._entitled_relays),
            parent=parent)

    # ------------------------------------------------------------------ #
    # The window                                                           #
    # ------------------------------------------------------------------ #

    def window(self):
        window = self._window
        return window if window is not None and shiboken6.isValid(window) else None

    def open_window(self) -> None:
        """Nostr > Imports: show the window (made on first use)."""
        if self.inbox is None or self._window_factory is None:
            return
        window = self.window()
        if window is None:
            window = self._window = self._window_factory(self)
            self.reconcile_inbox()
        window.show()
        window.raise_()
        window.activateWindow()

    def close_window(self) -> None:
        window = self.window()
        self._window = None
        if window is not None:
            window.close()
            window.deleteLater()


@dataclass(frozen=True)
class SourceSettings:
    """One source as Source Settings shows it."""

    key: str
    url: str
    name: str               # the name the person gave it ("" = the source's own)
    own_title: str          # what the source calls itself
    website: str
    automatic: bool         # a feed this app checks while it runs
    checking: bool          # automatic checks are on
    copy_images: bool       # defaults for new drafts (synced)
    full_text: bool


def plain_problem(exc: BaseException, folder: Path) -> str:
    """What to tell the person when the inbox cannot be opened: the cause
    in plain words and what to do about it."""
    name = getattr(exc, "sqlite_errorname", "") or ""
    number = getattr(exc, "errno", None)
    if name.startswith("SQLITE_FULL") or number in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return DISK_FULL
    if (name.startswith(("SQLITE_READONLY", "SQLITE_CANTOPEN", "SQLITE_PERM"))
            or isinstance(exc, PermissionError) or number in (errno.EACCES, errno.EPERM,
                                                              errno.EROFS)):
        return CANNOT_WRITE.format(folder=os.fspath(folder))
    if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return IN_USE
    return CANNOT_OPEN


def _is_damage(exc: BaseException) -> bool:
    """Whether a database error means the file is damaged (not a database
    any more, unreadable pages, missing tables), as opposed to one that
    cannot be opened or written (the folder, the disk, another process)."""
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")):
        return True
    return type(exc) is sqlite3.DatabaseError and not name   # InboxStore.check


# The statuses of imports that still have posts to do (jobs.unfinished).
UNFINISHED = ("paused", "running", "pausing", "stopping", "partial")


def _release_at_exit(controller_ref) -> Callable[[], None]:
    """An exit handler that gives the inbox's lock back if the controller
    still holds it (one per lock taken, unregistered when it is given back)."""
    def release() -> None:
        controller = controller_ref()
        if controller is not None and shiboken6.isValid(controller):
            controller._release_lock()
    return release


def _public(url: str) -> bool:
    """Whether a web address may be followed: never one on this computer or
    its network (the owner's decision Q-6). A Nostr address is judged by
    the readers."""
    if not url.strip().lower().startswith(("http://", "https://")):
        return True
    return url_safety.is_safe_mirror_source(url.strip())


def _manual_id(key: str) -> str:
    return "m-" + key


def _title_known(source: Optional[SourceRow]) -> bool:
    return source is not None and bool(source.title or source.feed_title)
