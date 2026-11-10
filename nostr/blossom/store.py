# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory Blossom media library + upload orchestration.

This is the layer the UI talks to. It owns:
  - the per-hash file map (dedup across mirror servers)
  - a 30 s freshness window on /list calls so the library, the picker
    and the editor can all call ``fetch()`` on mount without fanning
    out N x M requests
  - the upload pipeline: hash, keep the bytes in the local cache, plan,
    skip the servers the person's own listing says hold the blob, sign
    auth and PUT /upload to the first of the rest, then replicate to the
    others one at a time and merge URLs back into the map
  - the delete pipeline: sign auth, DELETE, remove from map

Signing happens via the existing ``BunkerSessionPool``. The store keeps
no key material of its own. Every signature costs the user a prompt in
their signer app, which is what shapes the whole upload path: the paged
/list walk reuses one token per server, a file the library already
lists on a server is not sent there again, replication is sequential
rather than a parallel prompt flood, and an upload failover is capped
rather than exhaustive.

State changes are surfaced through Qt signals so the dialogs can stay
declarative.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from PySide6.QtCore import QObject, Signal

import media_files
import url_safety
from i18n import N_, _

from ..bunker import BunkerClient, BunkerSessionPool
from ..profiles import Profile
from . import replicate
from .auth import build_blossom_auth_event
from .client import (
    BlossomClient,
    BlossomError,
    UploadResult,
    extract_server_from_blob_url,
    looks_like_sha256,
    server_origin,
    whole_number,
)
from .errors import ERROR_CODES, friendly_message, signing_failure_code
from .hashes import blob_url, url_agrees_with_hash
from .names import MediaNames
from .plan import get_effective_max_file, lists_publicly, plan_upload, UploadPlan
from .settings import BlossomSettings
from .upload_progress import UploadProgress
from ..imports.workers import run_for_size
from ..standup import crypto as scrypto


# How long a fetched library stays "fresh". A second call within this
# window is a no-op (matches STANDUP's 30 s constant).
_FETCH_FRESHNESS_SECONDS = 30.0
# Files whose first bytes are read at once to tell a submission's
# encrypted file (check_submission_file); the others wait their turn.
MAX_HEADS_AT_ONCE = 4

# BUD-12 cursor pagination. 200 keeps a page comfortably inside the
# client's 8 MiB response cap; 25 pages is 5000 blobs, which is far more
# than any real library and stops a server that ignores the cursor from
# turning a fetch into an endless walk.
_LIST_PAGE_SIZE = 200
_MAX_LIST_PAGES = 25

# Each failover costs another signer prompt, so the walk is short by
# design. A judgement, not a measurement; kept as a constant so it can
# be tuned once there is field data.
_MAX_UPLOAD_ATTEMPTS = 2

# Failures worth trying the next server for. 413 is here because it is
# precisely the case the size planner cannot know about: a server whose
# real cap is below its published one.
_RETRYABLE_STATUSES = frozenset({0, 413, 415, 429, 502, 503})

_log = logging.getLogger(__name__)

# How many uploads' refusing servers are kept for their Details.
_REFUSALS_KEPT = 32

# A server's refusal of the file itself (BUD-02: 413 too large, 415 a
# type it does not take), with the short reason a list of servers says.
_FILE_REFUSALS = {
    ERROR_CODES.SERVER_TOO_LARGE: N_("too large"),
    ERROR_CODES.UNSUPPORTED_TYPE: N_("not a type it takes"),
}

# 401 and 403 are deliberately absent: they usually mean the signer or
# the clock is the problem, so the next server would demand auth too and
# a failover buys a second prompt and a second failure.
_RETRYABLE_CODES = frozenset({ERROR_CODES.REDIRECT_REFUSED, ERROR_CODES.LOOKALIKE_REFUSED})

# A server that refuses a copy for one of these reasons would refuse the
# same bytes uploaded directly: its limits, a payment, the signature, or a
# copy that came back wrong. Any other refusal (no mirror support, the
# source out of its reach) gets a direct upload.
_NO_DIRECT_UPLOAD = frozenset({
    ERROR_CODES.RATE_LIMITED, ERROR_CODES.PAYMENT_REQUIRED, ERROR_CODES.SERVER_TOO_LARGE,
    ERROR_CODES.TOO_LARGE, ERROR_CODES.AUTH_REJECTED, ERROR_CODES.SIGNER_REJECTED,
    ERROR_CODES.SIGNER_SILENT,
    ERROR_CODES.SIGNER_IDENTITY_MISMATCH, ERROR_CODES.HASH_MISMATCH,
    ERROR_CODES.NETWORK_UNAVAILABLE, ERROR_CODES.LOOKALIKE_REFUSED,
})


# The types a submission's encrypted file is served as: it is uploaded as a
# byte stream (contract 7, never the plaintext's type), and a server that
# reads the bytes finds no other type in them.
_SEALED_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def may_be_sealed(mime_type: Optional[str]) -> bool:
    """Whether a file served as ``mime_type`` may be a submission's SMF1
    file: a byte stream, no type, or none that names a kind."""
    mime = (mime_type or "").lower().split(";", 1)[0].strip()
    return mime in _SEALED_TYPES or "/" not in mime


# ---------------------------------------------------------------------------
# Types and data classes
# ---------------------------------------------------------------------------

class BlobCache(Protocol):
    """The app's content-addressed byte cache.

    ``ThumbnailLoader`` is the implementation; it is a UI module and the
    store must not import it, so the object arrives as a constructor
    argument the way every other boundary in this codebase is crossed.
    The write half, and where a blob's bytes are, to tell a submission's
    encrypted file by its first bytes (:meth:`MediaStore.is_submission_file`).
    """

    def put_bytes(self, data: bytes) -> str: ...
    def cache_path(self, sha256: str) -> Path: ...


@dataclass
class MediaFile:
    """One unique blob in the library, deduped by sha256 across servers.

    ``urls`` lists every server that confirmed it has this hash. The
    first entry is the "primary URL" the UI uses for previews and
    inserts; ``url`` mirrors it for callers that just want a single
    string.

    ``width`` and ``height`` are populated lazily by the UI once a
    thumbnail decode produces a QPixmap; ``0`` means "not yet known".

    ``nip94`` holds the BUD-08 key/value pairs an upload or mirror
    response offered, already validated by the client. Captured for
    later use only: they are not published, because a server-supplied
    measurement is not one this app made.
    """

    hash: str
    url: str
    urls: List[dict] = field(default_factory=list)   # [{server, url}, ...]
    mime_type: str = "application/octet-stream"
    size: int = 0
    alt: str = ""
    uploaded_at_ms: int = 0
    width: int = 0
    height: int = 0
    nip94: List[List[str]] = field(default_factory=list)


@dataclass
class ServerListing:
    """What one server said about this account's files in the last fetch.

    ``ok`` False means the server could not be listed; its files from the
    previous fetch are kept (``stale``) rather than vanishing, because a
    server that is briefly down has not deleted anything. ``truncated``
    is True when the walk stopped at the page limit, so ``count`` and
    ``bytes`` are a lower bound and must not be shown as a total.
    """

    origin: str
    ok: bool = True
    count: int = 0
    bytes: int = 0
    truncated: bool = False
    error_code: str = ""
    fetched_at: float = 0.0


@dataclass
class UploadJobState:
    """Per-file state visible to the UI during an upload."""

    name: str
    status: str = "queued"     # queued | signing | uploading | mirroring | done | failed
    error: str = ""
    hash: str = ""
    # The PUT in flight, while the bytes are on their way: what Cancel
    # aborts. None before the signer answered and once a server has it.
    sending: Optional[object] = None
    # How far it has got, as its progress indicator shows it.
    meter: UploadProgress = field(default_factory=UploadProgress)


@dataclass(eq=False)
class _Added:
    """A file the person added to the Media Library: what a retry sends
    again, and the name it is remembered by. Compared by identity, so a
    late answer about an earlier attempt can tell it is stale."""

    label: str
    mime: str
    path: Optional[Path] = None
    body: Optional[bytes] = None
    failed: bool = False
    error: str = ""
    # Which servers refused it and why: a tooltip's, never the sentence's
    # (lessons 36; contract 7 names them).
    detail: str = ""


@dataclass(frozen=True)
class AddedUpload:
    """A file being added to the library, as a window opened later sees
    it: ``state`` is "waiting", "sending", "finishing" or "failed"."""

    name: str
    mime: str
    state: str
    error: str = ""
    path: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# MediaStore
# ---------------------------------------------------------------------------

class MediaStore(QObject):
    """Library state + upload/delete orchestration. One instance per
    main window."""

    # Library map changed (added / removed / replaced after fetch).
    library_changed = Signal()

    # Background fetch started (UI can show a spinner).
    fetch_started = Signal()
    # All servers responded (or all failed). ``fetch_error`` is also
    # emitted alongside this when every server failed, with the code of
    # why (the signer, a refusal, or no connection), so the UI can say
    # the real reason.
    fetch_finished = Signal()
    fetch_error = Signal(str)                          # code

    # Upload lifecycle.
    upload_started = Signal(str)                       # name
    upload_progress = Signal(str, int, int)            # name, sent, total
    upload_status = Signal(str, str)                   # name, status
    upload_finished = Signal(str, object)              # name, MediaFile
    # Every failure is both: the plain sentence to show, then its stable
    # code, so a subscriber branches on the code and never on the words.
    upload_failed = Signal(str, str)                   # name, reason
    upload_failed_code = Signal(str, str)              # name, code

    # Reroute toast trigger. Fired once per upload, after a server has
    # confirmed the blob, when that server is not the configured
    # primary.
    upload_rerouted = Signal(str, str, str)            # name, from_host, to_host

    # The person cancelled an upload while its bytes were on their way.
    # Nothing else is reported for it.
    upload_cancelled = Signal(str)                     # name

    # A mirror did not take the copy. Non-fatal: the primary has the
    # file, so the upload still succeeds and the document is untouched.
    mirror_failed = Signal(str, str, str)              # name, host, code
    # A server was left out of an upload before it started, because the
    # file would not fit in the space left there. Non-fatal as well.
    server_skipped = Signal(str, str, str)             # name, host, reason

    # Per-server listing results changed (see ``server_listings``).
    listings_changed = Signal()

    # Delete lifecycle.
    # Whether a listed file is a submission's encrypted file was read
    # from its first bytes, or could not be (submission_state).
    submission_checked = Signal(str)                   # file_hash
    file_deleted = Signal(str)                         # file_hash
    delete_failed = Signal(str, str)                   # file_hash, reason

    def __init__(
        self,
        *,
        session_pool: BunkerSessionPool,
        profile_provider: Callable[[], Optional[Profile]],
        settings: Optional[BlossomSettings] = None,
        client: Optional[BlossomClient] = None,
        blob_cache: Optional[BlobCache] = None,
        entitled_servers: Optional[Callable[[], Sequence[str]]] = None,
        server_quota: Optional[Callable[[str], Optional[int]]] = None,
        names: Optional[MediaNames] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session_pool = session_pool
        self._profile_provider = profile_provider
        self._settings = settings or BlossomSettings()
        self._client = client or BlossomClient(parent=self)
        # Where uploaded bytes are kept so displaying them never needs
        # the network. None means no cache, which is what every caller
        # had before this seam existed.
        self._blob_cache = blob_cache
        # Servers this account may use beyond the ones it configured, such
        # as one that comes with an association membership. Resolved on
        # each call because entitlement can change while the app is open.
        # This module knows nothing about what grants it.
        self._entitled_servers = entitled_servers
        # Space this account has on a server (bytes) when one is known,
        # such as the per-member allowance on an association's server.
        # Used to show usage and to leave a full server out of an upload.
        self._server_quota = server_quota
        # The names files had when they were added here (servers keep
        # none). Without a book, this session's names only.
        self._names = names
        self._session_names: Dict[str, str] = {}

        self._files: Dict[str, MediaFile] = {}
        self._listings: Dict[str, ServerListing] = {}
        # The servers the last fetch walked. A different set (membership
        # resolved, a server added) makes the library stale at once.
        self._last_targets: tuple = ()
        self._last_fetch_at: float = 0.0
        self._fetch_in_flight: bool = False
        # Every fetch carries the generation it started under. ``clear``
        # moves it on, so a walk still running for the account being left
        # can never land its files in the next account's library.
        self._fetch_generation: int = 0
        # The servers the running walk covers, and whether somebody asked
        # for a fetch while it ran. A server added mid-walk (a membership
        # that resolved) is listed once the walk ends rather than dropped.
        self._inflight_targets: tuple = ()
        self._fetch_requested: bool = False
        self._mirror_by_default: bool = True
        # Hashes this process committed itself during this session. Their
        # library records were written from a server's own confirmation
        # rather than from a /list response of unknown age, which is what
        # makes them safe to dedup against without listing again.
        self._committed: Set[str] = set()
        # Submissions' encrypted files: uploaded for one this session, and
        # what the cache's first bytes said (is_submission_file).
        self._submission_files: Set[str] = set()
        self._sealed_known: Dict[str, bool] = {}
        # First bytes being read from a server, or waiting for their turn
        # (MAX_HEADS_AT_ONCE are read at once), and those no server gave.
        self._heads_asked: Set[str] = set()
        self._heads_waiting: List[str] = []
        self._heads_running = 0
        self._heads_failed: Set[str] = set()
        # What stops each read on its way, by file: a new listing or
        # another account aborts them (RG-M2r3 10).
        self._heads_reading: Dict[str, Callable[[], None]] = {}

        # Upload jobs by name, from upload_started until they end. The
        # asset manager names its own (an asset key); a file added to the
        # library gets a name of its own that no other job has.
        self._uploads: Dict[str, UploadJobState] = {}
        # Files added to the library, by job name, from the moment they
        # are added until they land, are cancelled, or a failure is
        # dismissed: what Retry sends again.
        self._added: Dict[str, _Added] = {}
        # Which servers refused the last try of an upload not added to
        # the library (a submission's picture), and why: what its Details
        # name (contract 7), never its sentence. The newest few only.
        self._refused: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    @property
    def files(self) -> Dict[str, MediaFile]:
        """Live view of the library. Callers must not mutate the dict
        directly; use ``upload`` / ``delete_file`` so signals fire."""
        return self._files

    @property
    def settings(self) -> BlossomSettings:
        return self._settings

    def target_servers(self) -> List[str]:
        """Every server the library reads and writes, configured ones first."""
        return self._target_servers()

    def server_listings(self) -> Dict[str, ServerListing]:
        """What each server said in the last fetch, by origin."""
        return dict(self._listings)

    def bytes_on(self, origin: str) -> int:
        """Bytes this account stores on ``origin``, from the library.

        Counts every file the library knows to be there, including ones
        uploaded during this session, so the number moves as files are
        added or deleted without waiting for the next fetch.
        """
        target = url_safety.origin_of(origin)
        return sum(f.size for f in self._files.values()
                   if any(url_safety.origin_of(u.get("server", "")) == target
                          for u in f.urls))

    def name_of(self, sha256: str) -> str:
        """The name the file had when it was added here, or ""."""
        sha = (sha256 or "").lower()
        if self._names is not None:
            name = self._names.get(sha)
            if name:
                return name
        return self._session_names.get(sha, "")

    def is_submission_file(self, sha256: str) -> bool:
        """Whether this file is known to be a submission's encrypted file
        (SMF1, contract 7): :meth:`submission_state` is True. Such a file
        is never offered to insert, and deleting it takes it from the
        editorial team."""
        return self.submission_state(sha256) is True

    def submission_state(self, sha256: str) -> Optional[bool]:
        """Whether this file is a submission's encrypted file: uploaded
        for one in this session, or its first bytes (cached here, or read
        from its server by :meth:`check_submission_file`) start like one.
        None while that cannot be told: a listed file of a type a sealed
        file is served as (a byte stream, or none) whose first bytes are
        not here, as on another computer (RG-M2r 5). It is neither
        inserted nor deleted as a plain file until it is told."""
        sha = (sha256 or "").lower()
        if sha in self._submission_files:
            return True
        known = self._sealed_known.get(sha)
        if known is not None:
            return known
        head = self._cached_head(sha)
        if head is not None:
            # A content address never changes its bytes: the answer is kept.
            self._sealed_known[sha] = not scrypto.media_header_problem(head)
            return self._sealed_known[sha]
        media = self._files.get(sha)
        if media is None or not may_be_sealed(media.mime_type):
            return False
        return None

    def submission_check_failed(self, sha256: str) -> bool:
        """No server gave this file's first bytes the last time they were
        asked for (:meth:`check_submission_file` asks again)."""
        return (sha256 or "").lower() in self._heads_failed

    def check_submission_file(self, sha256: str, *, again: bool = False) -> None:
        """Read the first bytes of a file whose :meth:`submission_state`
        is None from its servers, one after the other, with a ranged GET
        of :data:`~nostr.standup.crypto.MEDIA_HEADER_BYTES` bytes; then
        :attr:`submission_checked`. Once at a time per file, and at most
        :data:`MAX_HEADS_AT_ONCE` files at once (a library listed from
        another computer may hold a thousand: RG-M2r2a 4); one that no
        server gave is asked for again only ``again`` (Try Again)."""
        sha = (sha256 or "").lower()
        media = self._files.get(sha)
        if media is None or sha in self._heads_asked or self.submission_state(sha) is not None:
            return
        if sha in self._heads_failed and not again:
            return
        self._heads_asked.add(sha)
        self._heads_failed.discard(sha)
        self._heads_waiting.append(sha)
        self._next_heads()

    def _next_heads(self) -> None:
        while self._heads_running < MAX_HEADS_AT_ONCE and self._heads_waiting:
            sha = self._heads_waiting.pop(0)
            media = self._files.get(sha)
            if media is None:
                self._heads_asked.discard(sha)
                continue
            self._heads_running += 1
            urls = [u.get("url", "") for u in media.urls if u.get("url")] or [media.url]
            self._read_head_of(sha, urls, self._fetch_generation)

    def _read_head_of(self, sha: str, urls: List[str], generation: int) -> None:
        def done(sealed: Optional[bool]) -> None:
            if generation != self._fetch_generation:
                return                      # another listing: read anew for it
            self._heads_running -= 1
            self._heads_asked.discard(sha)
            self._heads_reading.pop(sha, None)
            if sealed is None:
                self._heads_failed.add(sha)
            else:
                self._sealed_known[sha] = sealed
            self.submission_checked.emit(sha)
            self._next_heads()

        def ask(left: List[str]) -> None:
            if not left:
                done(None)
                return
            stop = self._client.read_head(
                left[0], scrypto.MEDIA_HEADER_BYTES,
                lambda head: done(not scrypto.media_header_problem(head)),
                lambda _error: ask(left[1:]) if generation == self._fetch_generation else None)
            if stop is not None:            # None: it answered at once (the next one is held)
                self._heads_reading[sha] = stop

        ask(urls)

    def _drop_head_reads(self) -> None:
        """A new listing, or another account: the head reads on their way
        answer for nothing now: each is stopped (its reply aborted, so no
        socket stays open for it), and what waits is asked anew when shown."""
        reading, self._heads_reading = self._heads_reading, {}
        for stop in reading.values():
            stop()
        self._heads_asked.clear()
        self._heads_waiting.clear()
        self._heads_running = 0

    def _cached_head(self, sha: str) -> Optional[bytes]:
        if self._blob_cache is None:
            return None
        try:
            with self._blob_cache.cache_path(sha).open("rb") as cached:
                return cached.read(scrypto.MEDIA_HEADER_BYTES)
        except OSError:
            return None                     # not cached

    def added_here(self, sha256: str) -> bool:
        """Whether this file was added to the library on this computer
        (it then has the name it was added with)."""
        return bool(self.name_of(sha256))

    def upload_job(self, name: str) -> Optional[UploadJobState]:
        """The running job of that name, or None."""
        return self._uploads.get(name)

    def added_uploads(self) -> List[AddedUpload]:
        """The files added to the library that have not landed yet, in
        the order they were added: still on their way, or failed."""
        out = []
        for name, added in self._added.items():
            job = self._uploads.get(name)
            if added.failed:
                state = "failed"
            elif job is None or job.status in ("queued", "signing"):
                state = "waiting" if job is None else "sending"
            else:
                state = "finishing" if job.status == "mirroring" else "sending"
            out.append(AddedUpload(name=name, mime=added.mime, state=state,
                                   error=added.error, detail=added.detail,
                                   path=str(added.path) if added.path else ""))
        return out

    def can_retry(self, name: str) -> bool:
        """Whether that added file failed and can be sent again."""
        added = self._added.get(name)
        return added is not None and added.failed

    def quota_for(self, origin: str) -> Optional[int]:
        """The space this account has on ``origin``, when one is known."""
        if self._server_quota is None:
            return None
        try:
            quota = self._server_quota(url_safety.origin_of(origin))
        except Exception:  # noqa: BLE001, a quota lookup must never break uploads
            return None
        return quota if isinstance(quota, int) and quota > 0 else None

    def file_list(
        self,
        *,
        filter_type: str = "all",
        sort_by: str = "newest",
    ) -> List[MediaFile]:
        """Filtered + sorted snapshot, matching STANDUP's options.

        ``filter_type`` ∈ {'all','image','video','audio','file'}; everything
        else falls under 'all'.
        ``sort_by`` ∈ {'newest','oldest','largest','smallest'}.
        """
        result = list(self._files.values())
        if filter_type in ("image", "video", "audio", "file"):
            result = [f for f in result if media_files.library_kind(f.mime_type) == filter_type]
        if sort_by == "oldest":
            result.sort(key=lambda f: f.uploaded_at_ms)
        elif sort_by == "largest":
            result.sort(key=lambda f: f.size, reverse=True)
        elif sort_by == "smallest":
            result.sort(key=lambda f: f.size)
        else:
            result.sort(key=lambda f: f.uploaded_at_ms, reverse=True)
        return result

    # ------------------------------------------------------------------
    # Library: fetch / clear
    # ------------------------------------------------------------------

    def fetch(self, *, force: bool = False) -> None:
        """Repopulate the library from every configured server, deduped
        by sha256. Concurrent calls are coalesced; calls within the 30 s
        freshness window are no-ops unless ``force`` is set or the set of
        servers changed since the last fetch (a membership that resolved
        adds one, and its files belong in the library at once).

        A call that arrives while a walk is running is remembered, not
        dropped: when the walk ends and the servers it covered are no
        longer the servers the library reads, it walks again.

        Each server is walked with BUD-12 cursor pagination, reusing one
        signed list token for all of its pages: BUD-11 scopes a list
        token to a server and gives it no ``x`` tag, so an N-page walk
        still costs a single signer prompt. A server known to list
        publicly is asked without a token first, which costs no prompt
        at all; a 401 or 403 then falls back to signing, once.

        A server that cannot be listed keeps the files the library last
        saw there, marked by its ``ServerListing`` as not ok: a server
        that is briefly down has not deleted anything.

        Quietly no-ops when no profile is active. The UI is expected to
        show its empty or connect-signer state in that case.
        """
        profile = self._profile_provider()
        if profile is None:
            return
        if self._fetch_in_flight:
            self._fetch_requested = True
            return
        servers = self._target_servers()
        targets = tuple(url_safety.origin_of(s) for s in servers)
        fresh = (time.monotonic() - self._last_fetch_at) < _FETCH_FRESHNESS_SECONDS
        if not force and fresh and targets == self._last_targets:
            return
        if not servers:
            return

        self._fetch_generation += 1
        self._drop_head_reads()
        generation = self._fetch_generation
        pubkey = (profile.user_pubkey or "").lower()
        self._fetch_in_flight = True
        self._fetch_requested = False
        self._inflight_targets = targets
        self.fetch_started.emit()

        merged: Dict[str, MediaFile] = {}
        remaining = {"count": len(servers), "errors": 0}
        listings: Dict[str, ServerListing] = {
            url_safety.origin_of(s): ServerListing(origin=url_safety.origin_of(s))
            for s in servers
        }
        # What the library held when the walk began. Anything committed
        # into ``self._files`` after this point is an upload that
        # finished mid-fetch, and a /list response is silent about
        # those: it is authoritative about what the server had, not
        # about what this process did while the request was in flight.
        started_with: Set[str] = set(self._files)
        previous: Dict[str, MediaFile] = dict(self._files)

        def current() -> bool:
            """This walk still speaks for the library: nothing cleared it,
            and the account it lists is still the active one."""
            if generation != self._fetch_generation:
                return False
            active = self._profile_provider()
            return active is not None and (active.user_pubkey or "").lower() == pubkey

        def finish_one() -> None:
            if not current():
                return
            remaining["count"] -= 1
            if remaining["count"] > 0:
                return
            self._fetch_in_flight = False
            self._inflight_targets = ()
            self._last_fetch_at = time.monotonic()
            self._last_targets = targets
            now = time.time()
            for listing in listings.values():
                listing.fetched_at = now
            self._listings = listings
            if remaining["errors"] >= len(servers):
                self.fetch_error.emit(_listing_failure_code(listings.values()))
            if remaining["errors"] < len(servers) or previous:
                failed = {o for o, l in listings.items() if not l.ok}
                carry_over(failed)
                for sha, media in self._files.items():
                    if sha not in merged and sha not in started_with:
                        merged[sha] = media
                order_urls(merged)
                self._files = merged
                self.library_changed.emit()
            self.listings_changed.emit()
            self.fetch_finished.emit()
            # Asked again while this walk ran: walk again only when the
            # servers changed underneath it. Anything else was answered.
            requested, self._fetch_requested = self._fetch_requested, False
            if requested and self._current_targets() != targets:
                self.fetch()

        def carry_over(failed_origins: Set[str]) -> None:
            """Keep what unreachable servers held last time."""
            if not failed_origins:
                return
            for sha, old in previous.items():
                kept = [dict(u) for u in old.urls
                        if url_safety.origin_of(u.get("server", "")) in failed_origins]
                if not kept:
                    continue
                if sha in merged:
                    have = {url_safety.origin_of(u.get("server", "")) for u in merged[sha].urls}
                    merged[sha].urls.extend(u for u in kept
                                            if url_safety.origin_of(u["server"]) not in have)
                else:
                    merged[sha] = MediaFile(
                        hash=old.hash, url=kept[0]["url"], urls=kept,
                        mime_type=old.mime_type, size=old.size, alt=old.alt,
                        uploaded_at_ms=old.uploaded_at_ms, width=old.width,
                        height=old.height, nip94=list(old.nip94))

        def order_urls(files: Dict[str, MediaFile]) -> None:
            """Copies in server order, the configured primary first, so the
            URL the UI copies and inserts does not depend on which server
            happened to answer first."""
            rank = {origin: i for i, origin in enumerate(targets)}
            for media in files.values():
                media.urls.sort(key=lambda u: rank.get(
                    url_safety.origin_of(u.get("server", "")), len(rank)))
                if media.urls:
                    media.url = str(media.urls[0].get("url") or media.url)

        def attempt_server(server: str, *, signed: bool, retried: bool) -> None:
            """List ``server`` one way. ``retried`` is True for the second
            way of asking, after the first was refused; it is the only
            retry a server gets in one fetch."""
            origin = server_origin(server)

            def do_list(auth_event: Optional[dict]) -> None:
                if not current():
                    return
                seen: Set[str] = set()
                request_page(origin, auth_event, cursor=None, page=1, seen=seen,
                             largest=0, signed=signed, retried=retried, server=server)

            if not signed:
                # No bunker round-trip: a public listing, or the fallback
                # after a signed request was refused.
                do_list(None)
                return

            unsigned = build_blossom_auth_event(
                "list", server=origin, pubkey_hex=profile.user_pubkey
            )
            self._sign_with_bunker(
                profile,
                unsigned,
                on_signed=do_list,
                on_failure=lambda reason: handle_list_error(
                    server, origin,
                    BlossomError(str(reason), code=signing_failure_code(reason)),
                    page=1, signed=True, retried=retried,
                ),
            )

        def request_page(
            origin: str,
            auth_event: Optional[dict],
            *,
            cursor: Optional[str],
            page: int,
            seen: Set[str],
            largest: int,
            signed: bool,
            retried: bool,
            server: str,
        ) -> None:
            self._client.list_for_pubkey(
                origin,
                profile.user_pubkey,
                auth_event,
                on_success=lambda items: handle_page(
                    server, origin, auth_event, items, cursor=cursor, page=page,
                    seen=seen, largest=largest, signed=signed, retried=retried,
                ),
                on_failure=lambda err: handle_list_error(
                    server, origin, err, page=page, signed=signed, retried=retried,
                ),
                cursor=cursor,
                limit=_LIST_PAGE_SIZE,
            )

        def handle_page(
            server: str,
            origin: str,
            auth_event: Optional[dict],
            items: list,
            *,
            cursor: Optional[str],
            page: int,
            seen: Set[str],
            largest: int,
            signed: bool,
            retried: bool,
        ) -> None:
            if not current():
                return
            listing = listings[url_safety.origin_of(origin)]
            novel = 0
            last_sha = ""
            echoed_cursor = False
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                sha = (entry.get("sha256") or "").lower()
                if not looks_like_sha256(sha):
                    continue
                last_sha = sha
                if cursor and sha == cursor:
                    echoed_cursor = True
                if sha not in seen:
                    seen.add(sha)
                    novel += 1
                    listing.count += 1
                    listing.bytes += _entry_size(entry)
                merge_entry(origin, sha, entry)

            # Stop conditions. Together they mean a server that ignores
            # the cursor and re-sends the same page forever is caught on
            # page two: it merges once and the walk ends, with no error
            # and no loop. A page shorter than asked for is not the end
            # by itself: BUD-12's ``limit`` is a maximum, and a server
            # may page smaller. It is the end when the server has already
            # sent a longer page; otherwise one more request (same token,
            # no prompt) tells, and an empty answer ends the walk.
            if not items or novel == 0 or echoed_cursor or not last_sha:
                finish_one()
                return
            if len(items) < largest:
                finish_one()
                return
            if page >= _MAX_LIST_PAGES:
                listing.truncated = True
                finish_one()
                return
            request_page(origin, auth_event, cursor=last_sha, page=page + 1,
                         seen=seen, largest=max(largest, len(items)),
                         signed=signed, retried=retried, server=server)

        def merge_entry(origin: str, sha: str, entry: dict) -> None:
            url = entry.get("url") or f"{origin}/{sha}"
            size = _entry_size(entry)
            mime = str(entry.get("type") or "application/octet-stream")
            uploaded = whole_number(entry.get("uploaded") or entry.get("created"))
            uploaded_ms = uploaded * 1000 if uploaded else int(time.time() * 1000)

            if sha in merged:
                existing = merged[sha]
                if not any(u.get("server") == origin for u in existing.urls):
                    existing.urls.append({"server": origin, "url": str(url)})
            else:
                merged[sha] = MediaFile(
                    hash=sha,
                    url=str(url),
                    urls=[{"server": origin, "url": str(url)}],
                    mime_type=mime,
                    size=size,
                    uploaded_at_ms=uploaded_ms,
                )

        def handle_list_error(server: str, origin: str, err: BlossomError, *,
                              page: int, signed: bool, retried: bool) -> None:
            if not current():
                return
            # A 401/403 on the first page means the other way of asking may
            # work: a signed request some operators reject mid-flight (clock
            # skew between signer and server) can succeed unsigned, since
            # BUD-11 marks the token optional for /list; an unsigned request
            # to a server that turned out to want one is retried signed.
            # Once per server per fetch: a server that refuses both ways is
            # a failed listing, not a loop.
            if not retried and page == 1 and getattr(err, "status", None) in (401, 403):
                if signed:
                    attempt_server(server, signed=False, retried=True)
                    return
                if lists_publicly(server):
                    attempt_server(server, signed=True, retried=True)
                    return
            listing = listings[url_safety.origin_of(origin)]
            if page > 1:
                # The first page was listed and is kept; the rest may be
                # missing (a server refusing the cursor, or busy for the
                # second request), which is not a failed listing.
                listing.truncated = True
                finish_one()
                return
            listing.ok = False
            listing.error_code = _error_code(err)
            remaining["errors"] += 1
            finish_one()

        for server in servers:
            attempt_server(server, signed=not lists_publicly(server), retried=False)

    def refetch_if_targets_changed(self) -> bool:
        """Walk the library again only when the servers it reads changed.

        For callers that hear about a possible change (a membership that
        resolved or lapsed) without knowing whether it moved anything, so
        a membership check that changed nothing costs no signer prompt.
        A library that has not been fetched for this account stays that
        way: listing costs prompts, and only opening the library asks for
        them. While a walk runs the request is remembered, and the walk
        repeats itself when it ends if the servers differ. Returns True
        when a fetch started.
        """
        if self._fetch_in_flight:
            if self._current_targets() != self._inflight_targets:
                self._fetch_requested = True
            return False
        if not self._last_targets:
            return False
        if self._current_targets() == self._last_targets:
            return False
        self.fetch()
        return self._fetch_in_flight

    def clear(self) -> None:
        """Drop the library entirely. Used on profile switch / sign-out.

        A walk still running is abandoned: its answers are for the
        account being left, and the next fetch starts a walk of its own
        instead of being folded into that one.
        """
        self._stop_uploads()
        self._fetch_generation += 1
        self._fetch_in_flight = False
        self._fetch_requested = False
        self._inflight_targets = ()
        self._files = {}
        self._listings = {}
        self._last_fetch_at = 0.0
        self._last_targets = ()
        self._committed.clear()
        self._submission_files.clear()
        self._sealed_known.clear()
        self._drop_head_reads()
        self._heads_failed.clear()
        self.library_changed.emit()
        self.listings_changed.emit()

    def _stop_uploads(self) -> None:
        """Every upload of the account being left stops: what is on its
        way is aborted, a signer, a hash or a read answering later goes
        nowhere (the job is gone), and each is reported cancelled."""
        jobs, self._uploads = self._uploads, {}
        added, self._added = self._added, {}
        self._refused = {}
        for job in jobs.values():
            if job.sending is not None:
                job.sending.abort()
        # Files being read, and added files that failed, go too: a retry
        # would send them as the next account.
        for name in dict.fromkeys([*jobs, *added]):
            self.upload_cancelled.emit(name)

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> str:
        """Add the file at ``file_path`` to the library: read it, then
        upload it as :meth:`upload_bytes` does. Returns the job's name,
        the file's name made unique among the jobs in flight ("photo
        (2).png"), which every signal about it carries. Errors surface
        via ``upload_failed``, and the job can then be retried.
        """
        path = Path(file_path)
        for name, added in self._added.items():
            if added.failed and added.path == path:
                # The same file again after it failed: its job, retried.
                self.retry_upload(name)
                return name
        mime = media_files.type_of(path.name) or "application/octet-stream"
        added = _Added(label=path.name or "upload", mime=mime, path=path)
        name = self._name_for(added.label)
        self._added[name] = added
        self._read_added(name, added)
        return name

    def add_bytes(self, body: bytes, *, label: str, mime_type: str) -> str:
        """Add bytes that are no file yet (a pasted image) to the
        library, under ``label``. Returns the job's name, as
        :meth:`upload_file` does."""
        added = _Added(label=label, mime=mime_type, body=bytes(body))
        name = self._name_for(label)
        self._added[name] = added
        self._dispatch(added.body, name, mime_type, added)
        return name

    def retry_upload(self, name: str) -> None:
        """Send an added file that failed again, under the same name."""
        added = self._added.get(name)
        if added is None or not added.failed:
            return
        added.failed, added.error = False, ""
        if added.path is not None:
            self._read_added(name, added)
        else:
            self._dispatch(added.body or b"", name, added.mime, added)

    def dismiss_upload(self, name: str) -> None:
        """Forget an added file that failed; its name is free again."""
        added = self._added.get(name)
        if added is not None and added.failed:
            del self._added[name]

    def cancel_upload(self, name: str) -> bool:
        """Stop an upload while its bytes are on their way: the request
        is aborted, a signer that answers later is ignored, and
        ``upload_cancelled`` is all that is reported. Refused (False)
        once a server holds the file: the copies on the other servers
        then finish in the background."""
        job = self._uploads.get(name)
        if job is not None and job.status not in ("queued", "signing", "uploading"):
            return False
        added = self._added.get(name)
        if job is None and (added is None or added.failed):
            return False
        self._uploads.pop(name, None)
        self._added.pop(name, None)
        if job is not None and job.sending is not None:
            job.sending.abort()
        self.upload_cancelled.emit(name)
        return True

    def _name_for(self, label: str) -> str:
        """``label``, or "label (2)" and so on when a job has it."""
        stem, ext = os.path.splitext(label)
        name, number = label, 2
        while name in self._added or name in self._uploads:
            name = f"{stem} ({number}){ext}"
            number += 1
        return name

    def _read_added(self, name: str, added: _Added) -> None:
        try:
            size = added.path.stat().st_size
        except OSError:
            self._refuse(name, ERROR_CODES.FILE_UNREADABLE)
            return

        def read(read_back) -> None:
            if self._added.get(name) is not added:
                return                       # cancelled while it was read
            body, _sha = read_back
            if not body:
                self._refuse(name, ERROR_CODES.FILE_UNREADABLE)
                return
            self._dispatch(body, name, added.mime, added)

        # A large file is read (and kept in the cache) off the UI thread.
        run_for_size(size, lambda: self._read_and_keep(added.path), read,
                     lambda _exc: self._refuse(name, ERROR_CODES.FILE_UNREADABLE),
                     parent=self)

    def _read_and_keep(self, path: Path) -> tuple:
        """``(bytes, sha256)`` of the file at ``path``, kept in the cache
        too. Safe on a worker thread: files only."""
        body = path.read_bytes()
        return body, self._hash_and_keep(body) if body else ""

    def _hash_and_keep(self, body: bytes) -> str:
        """The sha256 of ``body``, kept in the local cache before it is sent
        anywhere (the cache's own write hashes it, once). Safe on a worker
        thread.

        Without this the app downloads its own upload back from a server
        the moment it wants to show it, and simply fails offline. It
        happens before the network on purpose: a failed upload the user
        retries still has something to show. A cache that cannot be
        written is not an upload failure: the bytes still go to a server."""
        if self._blob_cache is not None:
            try:
                return self._blob_cache.put_bytes(body)
            except OSError:
                pass
        return hashlib.sha256(body).hexdigest()

    def upload_bytes(
        self,
        body: bytes,
        *,
        name: str,
        mime_type: str = "application/octet-stream",
        force: bool = False,
        submission: bool = False,
    ) -> None:
        """Upload an in-memory buffer. Used by the drop handler when the
        bytes come from a QMimeData payload rather than a file path.

        The bytes are put in the local cache first, so nothing that
        happens next decides whether they can still be displayed.

        Servers the person's own, fresh listing says already hold the
        blob are left out (see ``_servers_holding``). A file already on
        every server finishes here: no signature, no PUT, no prompt.

        When the first eligible server fails in a way another server
        might not, the upload walks to the next one. It only ever walks
        the servers the planner accepted, so a server skipped for size
        can never be reached by a retry.

        ``force`` sends the bytes to every eligible server whatever the
        library says it holds: for a file a server said it is missing
        (a STANDUP receipt's ``blobs_missing``), where the library's word
        would stand in for the server's. ``submission`` marks the file as
        a submission's encrypted file (:meth:`is_submission_file`).
        """
        self._dispatch(body, name, mime_type, None, force=force, submission=submission)

    def _dispatch(self, body: bytes, name: str, mime_type: str,
                  added: Optional[_Added], *, force: bool = False,
                  submission: bool = False) -> None:
        profile = self._profile_provider()
        if profile is None:
            self._refuse(name, ERROR_CODES.NO_SIGNER)
            return
        if not body:
            self._refuse(name, ERROR_CODES.FILE_UNREADABLE)
            return

        servers = self._target_servers()
        if not servers:
            self._refuse(name, ERROR_CODES.NO_SERVER)
            return

        # The size plan runs first, so an oversized file is refused
        # without a single request leaving the process.
        plan: UploadPlan = plan_upload(len(body), servers)
        if plan.primary is None:
            # The number the person needs is the largest file any of
            # their servers takes, not the size of their own file.
            limit = max(get_effective_max_file(s) for s in servers)
            self._refuse(name, ERROR_CODES.FILE_TOO_LARGE, _(
                "This file is larger than your media servers take ({size})."
            ).format(size=media_files.format_size(limit)))
            return

        # The job exists from here on, before the hash: Cancel and
        # clear() reach it while a large file is hashed off the UI
        # thread, and nothing goes on for a job that is gone.
        state = UploadJobState(name=name, status="queued")
        self._uploads[name] = state
        run_for_size(len(body), lambda: self._hash_and_keep(body),
                     lambda sha: self._upload_hashed(state, body, mime_type, profile, sha,
                                                     servers, plan, force, submission),
                     parent=self)

    def _upload_hashed(self, state: UploadJobState, body: bytes, mime_type: str, profile,
                       sha: str, servers, plan: UploadPlan, force: bool,
                       submission: bool) -> None:
        name = state.name
        if self._uploads.get(name) is not state:
            return                           # cancelled while it was hashed, or left
        if submission:
            self._submission_files.add(sha)
        eligible = [server for server in plan.eligible
                    if self._has_room(server, sha, len(body), name)]
        if not eligible:
            self._fail_upload(name, ERROR_CODES.QUOTA_FULL)
            return
        state.hash = sha
        state.meter.expect(len(eligible) - 1)
        self.upload_started.emit(name)
        self.upload_status.emit(name, state.status)
        if self._uploads.get(name) is not state:
            return                           # cancelled as it started

        self._upload_missing(
            name=name,
            body=body,
            mime_type=mime_type,
            profile=profile,
            sha=sha,
            eligible=eligible,
            present=[] if force else self._servers_holding(sha, eligible),
            configured_primary=servers[0],
        )

    def _has_room(self, server: str, sha: str, size: int, name: str) -> bool:
        """Whether a file of ``size`` bytes still fits on ``server``.

        Only servers with a known allowance are checked, and only when
        the last fetch listed that server successfully: usage counted
        from a listing that failed (or never ran) is a guess, and a
        server is never left out on a guess. It then decides for itself.
        A file the library already knows to be there costs nothing more.
        A server that is full is left out of this upload with a note,
        never the reason the whole upload fails while another server has
        room.
        """
        quota = self.quota_for(server)
        if quota is None:
            return True
        origin = url_safety.origin_of(server)
        listing = self._listings.get(origin)
        if listing is None or not listing.ok:
            return True
        existing = self._files.get(sha)
        if existing is not None and any(
                url_safety.origin_of(u.get("server", "")) == origin for u in existing.urls):
            return True
        if self.bytes_on(origin) + size <= quota:
            return True
        _log.info("%s skipped %s: it is full", name, _hostname(server))
        self.server_skipped.emit(name, _hostname(server), "full")
        return False

    def _current_targets(self) -> tuple:
        """The origins a fetch started now would walk, in order."""
        return tuple(url_safety.origin_of(s) for s in self._target_servers())

    def _target_servers(self) -> List[str]:
        """Configured servers, followed by any this account is entitled to.

        Entitled servers are appended, never promoted. A benefit must not
        quietly become the primary and start receiving a user's uploads
        ahead of the server they chose themselves. Appending also means
        losing the entitlement costs nothing already stored elsewhere.

        Used for retrieval as well as upload, so media already sitting on
        an entitled server is listed rather than looking lost.
        """
        servers = list(self._settings.configured_servers)
        if self._entitled_servers is None:
            return servers
        try:
            extra = list(self._entitled_servers() or ())
        except Exception:  # noqa: BLE001, an entitlement lookup must never
            return servers  # take the media library down with it
        seen = {url_safety.origin_of(s) for s in servers}
        for url in extra:
            origin = url_safety.origin_of(url)
            if origin and origin not in seen and url_safety.is_safe_media_url(origin):
                seen.add(origin)
                servers.append(origin)
        return servers

    # ------------------------------------------------------------------
    # Dedup: servers the person's own library says already hold the blob
    # ------------------------------------------------------------------

    def _servers_holding(self, sha: str, eligible: List[str]) -> List[str]:
        """Which of ``eligible`` already hold ``sha`` for this person.

        Only the library map answers, and only when its record can be
        believed: one this process wrote itself this session, or one from
        a ``/list`` inside the freshness window. A listing is the person's
        own files, so a hit there means the file is theirs on that server.

        A server is never skipped because it merely has the bytes (an
        unsigned ``HEAD``): someone else may have uploaded them, and then
        the file is not the person's there, not in their listing, not
        theirs to delete, and gone when the other owner deletes it. Such
        a server gets the upload, which BUD-02 answers with a cheap 200.
        """
        if sha not in self._committed and \
                (time.monotonic() - self._last_fetch_at) >= _FETCH_FRESHNESS_SECONDS:
            return []
        held = servers_holding(self._files.get(sha), eligible)
        return [s for s in eligible if s in held]

    def _upload_missing(
        self,
        *,
        name: str,
        body: bytes,
        mime_type: str,
        profile: Profile,
        sha: str,
        eligible: List[str],
        present: List[str],
        configured_primary: str,
    ) -> None:
        """Put the blob on every eligible server that does not have it."""
        missing = [s for s in eligible if s not in present]
        if present and not missing:
            self._commit_covered(name, sha, present, body, mime_type)
            return
        if present:
            # A server that just confirmed the hash is a better source
            # than this process: BUD-04 exists so the bytes travel once.
            media = self._covered_media(sha, present, body, mime_type)
            self._replicate(
                name=name,
                profile=profile,
                media=media,
                source_url=media.url,
                mirror_servers=missing,
                body=body,
            )
            return

        # Servers this upload already tried and lost. They are not
        # offered the blob a second time as a mirror target: the request
        # would be new but the problem would be the same one. Once the
        # file lands elsewhere they are reported as copies not kept.
        exhausted: List[str] = []
        missed: List[tuple] = []
        # This upload's job. Every answer below asks whether it is still
        # the job of that name: after a Cancel it is not, and a signer or
        # a server answering late sends and reports nothing.
        job = self._uploads.get(name)

        def live() -> bool:
            return self._uploads.get(name) is job

        def attempt(index: int) -> None:
            target = missing[index]
            origin = server_origin(target)
            mirrors = [s for s in missing if s != target and s not in exhausted]

            # A fresh token per attempt, scoped to this server. Reusing
            # the previous one would send a credential to a host it does
            # not name, which the client refuses anyway.
            unsigned = build_blossom_auth_event(
                "upload",
                file_hash=sha,
                server=origin,
                pubkey_hex=profile.user_pubkey,
            )

            if job is not None and job.status != "signing":
                job.status = "signing"
                job.meter.signing(remote=_signs_remotely(profile))
                self.upload_status.emit(name, job.status)

            def on_signed(auth_event: dict) -> None:
                if not live():
                    return
                if job is not None:
                    job.status = "uploading"
                    self.upload_status.emit(name, job.status)
                sending = self._client.upload(
                    origin,
                    body,
                    mime_type,
                    auth_event,
                    on_success=lambda result: on_sent(result, mirrors),
                    on_failure=lambda err: on_attempt_failed(index, err),
                    on_progress=lambda sent, total: self._on_progress(name, sent, total),
                    sha256=sha,
                )
                if job is not None and live():
                    job.sending = sending

            self._sign_with_bunker(
                profile,
                unsigned,
                on_signed=on_signed,
                # A signer failure is not a server failure: the next
                # server would ask the same signer, so failing over just
                # buys a second prompt and a second refusal.
                on_failure=lambda reason: live() and self._fail_upload(
                    name, signing_failure_code(reason)),
            )

        def on_sent(result: UploadResult, mirrors: List[str]) -> None:
            if not live():
                return
            if job is not None:
                job.sending = None
            self._on_primary_uploaded(
                name=name,
                body=body,
                profile=profile,
                primary_result=result,
                mirror_servers=mirrors,
                configured_primary=configured_primary,
                missed=missed,
            )

        def on_attempt_failed(index: int, err: BlossomError) -> None:
            if not live():
                return
            if job is not None:
                job.sending = None
            exhausted.append(missing[index])
            missed.append((_hostname(missing[index]), _error_code(err)))
            next_index = index + 1
            # A server that refused the file itself (413, 415) says
            # nothing of the others: every one is tried (BUD-02).
            attempts = len(missing) if _refused_file(err) else _MAX_UPLOAD_ATTEMPTS
            if next_index < min(attempts, len(missing)) and _is_retryable(err):
                if job is not None:
                    job.meter.retrying()
                attempt(next_index)
                return
            sentence, detail = (_refusals(missed, err) if _refused_file(err)
                                else (_format_err(err), ""))
            self._fail_upload(name, _error_code(err), sentence, detail=detail)

        attempt(0)

    def _covered_media(
        self,
        sha: str,
        holders: List[str],
        body: bytes,
        mime_type: str,
    ) -> MediaFile:
        """A library record for a blob that servers already hold.

        ``holders`` is never empty: both callers reach here only after a
        server confirmed the hash.

        Addresses come from the existing record when it names one for
        that origin and that address still agrees with the hash;
        otherwise from BUD-01's canonical ``<origin>/<sha256>``, which is
        served from the root of every Blossom domain.
        """
        known = self._files.get(sha)
        addresses: Dict[str, str] = {}
        for entry in (getattr(known, "urls", None) or []):
            if not isinstance(entry, dict):
                continue
            origin = _origin_or_empty(entry.get("server") or entry.get("url") or "")
            url = str(entry.get("url") or "")
            if origin and url and url_agrees_with_hash(url, sha):
                addresses.setdefault(origin, url)

        urls = []
        for server in holders:
            origin = server_origin(server)
            urls.append({"server": origin,
                         "url": addresses.get(origin) or blob_url(origin, sha)})
        return MediaFile(
            hash=sha,
            url=urls[0]["url"],
            urls=urls,
            mime_type=getattr(known, "mime_type", "") or mime_type
            or "application/octet-stream",
            size=len(body),
            alt=getattr(known, "alt", "") or "",
            uploaded_at_ms=getattr(known, "uploaded_at_ms", 0)
            or int(time.time() * 1000),
            nip94=list(getattr(known, "nip94", None) or []),
        )

    def _commit_covered(
        self,
        name: str,
        sha: str,
        holders: List[str],
        body: bytes,
        mime_type: str,
    ) -> None:
        """Finish an upload that turned out to be unnecessary.

        The blob is already on every server the plan chose, so there is
        nothing to send and nothing to sign. It still commits and still
        emits ``upload_finished``: the caller asked for the file to be on
        the user's servers, and it is.

        No reroute is announced. That toast tells the user where their
        upload went, and this path uploaded nothing.
        """
        self._commit_upload(name, self._covered_media(sha, holders, body, mime_type))

    def _on_progress(self, name: str, sent: int, total: int) -> None:
        state = self._uploads.get(name)
        if state is None:
            return
        state.meter.sending(sent, total)
        self.upload_progress.emit(name, int(sent), int(total))

    def _on_primary_uploaded(
        self,
        *,
        name: str,
        body: bytes,
        profile: Profile,
        primary_result: UploadResult,
        mirror_servers: List[str],
        configured_primary: str = "",
        missed: Sequence[tuple] = (),
    ) -> None:
        sha = primary_result["hash"]
        primary_server = primary_result["server"]
        mime = primary_result["mime_type"]
        size = primary_result["size"] or len(body)
        primary_url = primary_result["url"]

        media = MediaFile(
            hash=sha,
            url=primary_url,
            urls=[{"server": primary_server, "url": primary_url}],
            mime_type=mime,
            size=size,
            uploaded_at_ms=int(time.time() * 1000),
            nip94=list(primary_result.get("nip94") or []),
        )

        self._replicate(
            name=name,
            profile=profile,
            media=media,
            source_url=primary_url,
            mirror_servers=mirror_servers,
            configured_primary=configured_primary,
            body=body,
            missed=missed,
        )

    def _replicate(
        self,
        *,
        name: str,
        profile: Profile,
        media: MediaFile,
        source_url: str,
        mirror_servers: List[str],
        configured_primary: str = "",
        body: Optional[bytes] = None,
        missed: Sequence[tuple] = (),
    ) -> None:
        """Copy a blob one server already holds onto the rest, in turn.

        One server at a time on purpose. Each copy is a signature, and a
        parallel fan-out is a stack of approval popups the user has to
        clear one by one anyway, which is the flood the importer's loop
        was written to avoid.

        A server that cannot copy it (no mirror support, or it cannot
        reach the source) gets the bytes uploaded directly when they are
        at hand. A server that refuses for its own reasons (a limit, a
        payment, the signature) is reported and the walk carries on. The
        blob is already on a server, so failing the whole upload over a
        replication problem would take an image out of a document to
        punish a server the user does not control.
        """
        # (host, code) of every server that ends up without a copy: the
        # ones the upload itself could not reach, then refused mirrors.
        failures: List[tuple] = list(missed)
        if not mirror_servers:
            for host, code in failures:
                self._copy_missing(name, host, code)
            self._commit_upload(name, media, configured_primary=configured_primary)
            return

        # The first server has it: it is in the library from now on, so
        # its name is remembered now, not after the copies (quitting
        # stops those, and it would come back named by its day).
        self._remember_name(name, media.hash)
        state = self._uploads.get(name)
        if state is not None:
            state.status = "mirroring"
            state.meter.copying(len(mirror_servers))
            self.upload_status.emit(name, state.status)

        def live() -> bool:
            return self._uploads.get(name) is state

        def step(index: int) -> None:
            if not live():
                return                       # its account was left: no more copies
            if state is not None and index > 0:
                state.meter.copied()
            if index >= len(mirror_servers):
                for host, code in failures:
                    self._copy_missing(name, host, code)
                self._commit_upload(
                    name, media, configured_primary=configured_primary
                )
                return
            origin = server_origin(mirror_servers[index])

            def on_ok(result: UploadResult, o=origin, i=index) -> None:
                media.urls.append({"server": o, "url": result["url"]})
                step(i + 1)

            def on_err(err: BlossomError, o=origin, i=index) -> None:
                failures.append((_hostname(o), _error_code(err)))
                step(i + 1)

            def on_mirror_err(err: BlossomError, o=origin, i=index) -> None:
                if body is None or _error_code(err) in _NO_DIRECT_UPLOAD:
                    on_err(err, o, i)
                    return
                replicate.upload_to_server(
                    session_pool=self._session_pool,
                    profile=profile,
                    client=self._client,
                    server=o,
                    body=body,
                    mime=media.mime_type,
                    sha256=media.hash,
                    on_success=lambda result, o=o, i=i: on_ok(result, o, i),
                    on_failure=lambda err2, o=o, i=i: on_err(err2, o, i),
                    live=live,
                )

            replicate.mirror_to_server(
                session_pool=self._session_pool,
                profile=profile,
                client=self._client,
                server=origin,
                source_url=source_url,
                sha256=media.hash,
                on_success=on_ok,
                on_failure=on_mirror_err,
                live=live,
            )

        step(0)

    def _remember_name(self, name: str, sha: str) -> None:
        """Remember the file added as job ``name`` by the name it was
        added with (an editor upload's job is not a file name)."""
        added = self._added.get(name)
        if added is not None:
            self._session_names[sha] = added.label
            if self._names is not None:
                self._names.remember(sha, added.label)

    def _commit_upload(
        self,
        name: str,
        media: MediaFile,
        *,
        configured_primary: str = "",
    ) -> None:
        # Dedupe with anything already in the library: if the same hash
        # was already there, prefer the new URL list, which is freshest.
        self._files[media.hash] = media
        # Written from confirmations this process collected, so the next
        # upload of the same bytes can trust it without listing again.
        self._committed.add(media.hash)
        self._remember_name(name, media.hash)
        self._added.pop(name, None)
        state = self._uploads.pop(name, None)
        if state is not None:
            state.status = "done"
            state.meter.done()

        # Reroute is reported only now, against the server that actually
        # confirmed the blob. Announcing it at plan time claimed a
        # destination that the upload could still fail to reach.
        if configured_primary:
            from_host = _hostname(configured_primary)
            to_host = _hostname(media.urls[0].get("server", "")) if media.urls else ""
            if from_host and to_host and from_host != to_host:
                self.upload_rerouted.emit(name, from_host, to_host)

        self.upload_status.emit(name, "done")
        self.upload_finished.emit(name, media)
        self.library_changed.emit()

    def _fail_upload(self, name: str, code: str, reason: str = "", *,
                     detail: str = "") -> None:
        state = self._uploads.pop(name, None)
        if state is not None:
            state.status = "failed"
            state.error = reason or friendly_message(code)
        self.upload_status.emit(name, "failed")
        self._refuse(name, code, reason, detail=detail)

    def _copy_missing(self, name: str, host: str, code: str) -> None:
        """A server kept no copy of a file the others have: the upload
        worked, so this is logged and told, never said (lessons 36)."""
        _log.info("%s has no copy on %s: %s", name, host, code)
        self.mirror_failed.emit(name, host, code)

    def refusal_detail(self, name: str) -> str:
        """Which servers refused the last try of an upload and why, one
        per line, for a tooltip or Details; "" when none did."""
        added = self._added.get(name)
        return added.detail if added is not None else self._refused.get(name, "")

    def _refuse(self, name: str, code: str, reason: str = "", *, detail: str = "") -> None:
        """Report one upload's failure: the sentence, then its code. An
        added file stays, to be retried or dismissed."""
        if detail:
            _log.info("media servers refused %s: %s", name, detail.replace("\n", "; "))
        added = self._added.get(name)
        self._refused.pop(name, None)
        if added is not None:
            added.failed = True
            added.error = reason or friendly_message(code)
            added.detail = detail
        elif detail:
            self._refused[name] = detail
            while len(self._refused) > _REFUSALS_KEPT:
                del self._refused[next(iter(self._refused))]
        self.upload_failed.emit(name, reason or friendly_message(code))
        self.upload_failed_code.emit(name, code)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_file(self, file_hash: str, *, servers: Sequence[str] = (),
                    best_effort: Sequence[str] = ()) -> None:
        """Delete a blob from every server it lives on.

        Files are deduped by sha256 across servers, so the same hash can
        be on N hosts; a DELETE goes to each in parallel. A server that
        confirms (or answers 404, already gone) is dropped from the
        file's record. Only when every server confirmed is the file gone
        and ``file_deleted`` emitted; otherwise the file stays in the
        library with the servers that still hold it, and
        ``delete_failed`` carries the code of what stopped it. A file is
        never reported gone while a server still serves it: it would come
        back on the next listing, and the person would have believed it
        deleted.

        ``servers`` names where else the file was put (a public copy's
        record): it is deleted there too, listed or not.

        ``best_effort`` names servers that may hold it unrecorded (the ones
        in use, for a public copy): asked too, but one that neither lists
        it nor is named in ``servers`` never had it as far as anyone
        knows, so its failure (unreachable today, or refusing to delete
        what it doesn't have) doesn't keep the file.
        """
        profile = self._profile_provider()
        if profile is None:
            self.delete_failed.emit(file_hash, ERROR_CODES.NO_SIGNER)
            return
        # The servers to ask: wherever the listing shows the file, and
        # wherever ``servers`` says it was put (a public copy's record:
        # a server since removed from the list, another device's upload).
        media = self._files.get(file_hash)
        urls = [dict(u) for u in media.urls] if media is not None else []
        listed = {extract_server_from_blob_url(u.get("url", "")) for u in urls}
        for origin in dict.fromkeys(_origin_or_empty(s) for s in servers):
            if origin and origin not in listed:
                urls.append({"server": origin, "url": blob_url(origin, file_hash)})
                listed.add(origin)
        unknown: set = set()
        for origin in dict.fromkeys(_origin_or_empty(s) for s in best_effort):
            if origin and origin not in listed:
                urls.append({"server": origin, "url": blob_url(origin, file_hash)})
                listed.add(origin)
                unknown.add(origin)
        if not urls:
            if media is None:
                return
            urls = [{"server": "", "url": media.url}]
        if media is None:
            media = MediaFile(hash=file_hash, url=urls[0]["url"], urls=urls)

        target_servers: list[str] = []
        for entry in urls:
            origin = extract_server_from_blob_url(entry.get("url", ""))
            if origin and origin not in target_servers:
                target_servers.append(origin)
        if not target_servers:
            target_servers.append(self._settings.primary)

        remaining = {"count": len(target_servers)}
        failures: list[tuple[str, str]] = []

        def failed_one(origin: str, code: str) -> None:
            if origin not in unknown:
                failures.append((origin, code))
            finish_one()

        def finish_one() -> None:
            remaining["count"] -= 1
            if remaining["count"] > 0:
                return
            if not failures:
                self._files.pop(file_hash, None)
                self.file_deleted.emit(file_hash)
            elif file_hash not in self._files:
                self.delete_failed.emit(file_hash, _delete_failure_code(failures))
            else:
                kept = {origin for origin, _code in failures}
                media.urls = [u for u in urls
                              if extract_server_from_blob_url(u.get("url", "")) in kept]
                if media.urls:
                    media.url = str(media.urls[0].get("url") or media.url)
                self.delete_failed.emit(file_hash, _delete_failure_code(failures))
            self.library_changed.emit()

        for origin in target_servers:
            self._delete_one_server(
                profile=profile,
                file_hash=file_hash,
                origin=origin,
                on_ok=finish_one,
                on_err=lambda code, o=origin: failed_one(o, code),
            )

    def _delete_one_server(
        self,
        *,
        profile: Profile,
        file_hash: str,
        origin: str,
        on_ok: Callable[[], None],
        on_err: Callable[[str], None],
    ) -> None:
        """Sign and send a single DELETE. 404 is treated as success
        (the file isn't on that server anymore, which is what the user
        was asking for). ``on_err`` gets the failure's code."""
        unsigned = build_blossom_auth_event(
            "delete",
            file_hash=file_hash,
            server=origin,
            pubkey_hex=profile.user_pubkey,
        )

        def after_sign(auth_event: dict) -> None:
            def on_ok_or_404(err: BlossomError) -> None:
                if err.status == 404:
                    on_ok()
                else:
                    on_err(_error_code(err))

            self._client.delete(
                origin,
                file_hash,
                auth_event,
                on_success=on_ok,
                on_failure=on_ok_or_404,
            )

        self._sign_with_bunker(
            profile,
            unsigned,
            on_signed=after_sign,
            on_failure=lambda reason: on_err(signing_failure_code(reason)),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sign_with_bunker(
        self,
        profile: Profile,
        unsigned_event: dict,
        *,
        on_signed: Callable[[dict], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Resolve the bunker session for ``profile`` and ask it to sign.

        Matches the publisher's flow: a single shared ``BunkerSessionPool``
        coalesces parallel sign requests for the same profile, so the
        first auth event in a fetch warms the channel for the rest.
        """

        def on_ready(client: BunkerClient) -> None:
            client.sign_event(
                unsigned_event,
                on_success=on_signed,
                on_failure=on_failure,
            )

        self._session_pool.get(profile, on_ready=on_ready, on_error=on_failure)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def servers_holding(media, planned: Sequence[str]) -> Set[str]:
    """Which of ``planned`` a library record says already hold the blob.

    Pure, so the dedup decision can be read and tested without a store,
    a transport or a signer. Matching is by origin, because a record's
    ``server`` field and a configured server are two spellings of the
    same address: one came back from ``/list`` or an upload response, the
    other was typed by the user.

    Whether the answer may be acted on without asking the servers is a
    separate question, and a caller's to make: this function only reports
    what the record claims.
    """
    if media is None:
        return set()
    held = set()
    for entry in (getattr(media, "urls", None) or []):
        if not isinstance(entry, dict):
            continue
        origin = _origin_or_empty(entry.get("server") or entry.get("url") or "")
        if origin:
            held.add(origin)
    return {s for s in planned if _origin_or_empty(s) in held}


def _entry_size(entry: dict) -> int:
    """A /list entry's size in bytes; 0 when it is missing or nonsense."""
    return whole_number(entry.get("size"))


def _origin_or_empty(value: str) -> str:
    try:
        return server_origin(value)
    except ValueError:
        return ""


def _signs_remotely(profile) -> bool:
    """Whether ``profile`` signs in a signer app (NIP-46), so a signature
    is something to wait for."""
    return getattr(profile, "signer", "local") != "local"


def _hostname(url: str) -> str:
    try:
        return server_origin(url).split("://", 1)[1]
    except ValueError:
        return url


def _error_code(err) -> str:
    """Stable code for a failure, whatever shape it arrived in."""
    if isinstance(err, BlossomError):
        return err.code or ERROR_CODES.UPLOAD_FAILED
    # Everything else reaching here came from the signing step.
    return signing_failure_code(err)


def _listing_failure_code(listings) -> str:
    """Why no server could be listed, as one code: the signer, a server
    refusing the account, a server out of reach, or a server that
    answered but did not list (busy, or without /list, which BUD-12
    makes optional): not the connection's fault, so not said to be."""
    codes = {listing.error_code for listing in listings}
    for wanted in (ERROR_CODES.SIGNER_REJECTED, ERROR_CODES.SIGNER_SILENT,
                   ERROR_CODES.AUTH_REJECTED):
        if wanted in codes:
            return wanted
    if codes <= {ERROR_CODES.NETWORK_UNAVAILABLE}:
        return ERROR_CODES.NETWORK_UNAVAILABLE
    return ERROR_CODES.LIST_FAILED


def _delete_failure_code(failures) -> str:
    """The one code a delete that some server did not take is reported
    with: the signer first (nothing was asked of the servers), then an
    unreachable server (try again later), then whatever a server said."""
    codes = [code for _origin, code in failures]
    for wanted in (ERROR_CODES.SIGNER_REJECTED, ERROR_CODES.SIGNER_SILENT,
                   ERROR_CODES.NETWORK_UNAVAILABLE):
        if wanted in codes:
            return wanted
    return codes[0] if codes else ERROR_CODES.UPLOAD_FAILED


def _refused_file(err) -> bool:
    return isinstance(err, BlossomError) and err.status in (413, 415)


def _refusals(missed: Sequence[tuple], err: BlossomError) -> Tuple[str, str]:
    """``(sentence, detail)`` for a file every server refused, never the
    plaintext uploaded instead: the one server's remedy, or that none
    took it; which servers and why is ``detail`` (contract 7 names them,
    behind a tooltip: lessons 36)."""
    detail = "\n".join(f"{host}: {_(_FILE_REFUSALS[code])}" if code in _FILE_REFUSALS
                       else host for host, code in missed)
    if len(missed) == 1:
        return _format_err(err), detail
    return _("No media server took the file: add another one in Media Servers."), detail


def _is_retryable(err) -> bool:
    """Whether another server is worth trying for this failure."""
    if not isinstance(err, BlossomError):
        return False
    if err.code in _RETRYABLE_CODES:
        return True
    if err.code in (
        ERROR_CODES.HOST_MISMATCH,
        ERROR_CODES.HASH_MISMATCH,
        ERROR_CODES.AUTH_REJECTED,
        ERROR_CODES.PAYMENT_REQUIRED,
    ):
        return False
    return err.status in _RETRYABLE_STATUSES


def _format_err(err: BlossomError) -> str:
    """The plain sentence for a server's or the transport's failure: the
    code's copy plus the operator's own ``X-Reason``, never a Qt
    transport string."""
    return friendly_message(err.code or ERROR_CODES.UPLOAD_FAILED, detail=err.detail)
