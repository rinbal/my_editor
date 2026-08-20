# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory Blossom media library + upload orchestration.

This is the layer the UI talks to. It owns:
  - the per-hash file map (dedup across mirror servers)
  - a 30 s freshness window on /list calls so the library, the picker
    and the editor can all call ``fetch()`` on mount without fanning
    out N x M requests
  - the upload pipeline: hash, keep the bytes in the local cache, plan,
    ask each server whether it already has the blob, sign auth and PUT
    /upload only for the servers that do not, then replicate to the rest
    one at a time and merge URLs back into the map
  - the delete pipeline: sign auth, DELETE, remove from map

Signing happens via the existing ``BunkerSessionPool``. The store keeps
no key material of its own. Every signature costs the user a prompt in
their signer app, which is what shapes the whole upload path: the paged
/list walk reuses one token per server, the dedup probe is unsigned so
asking a question is free, replication is sequential rather than a
parallel prompt flood, and an upload failover is capped rather than
exhaustive.

State changes are surfaced through Qt signals so the dialogs can stay
declarative.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set

from PySide6.QtCore import QObject, Signal

import url_safety

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
)
from .errors import ERROR_CODES, friendly_message
from .hashes import blob_url, url_agrees_with_hash
from .plan import get_effective_max_file, plan_upload, UploadPlan
from .settings import BlossomSettings


# How long a fetched library stays "fresh". A second call within this
# window is a no-op (matches STANDUP's 30 s constant).
_FETCH_FRESHNESS_SECONDS = 30.0

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
_RETRYABLE_STATUSES = frozenset({0, 413, 429, 502, 503})

# 401 and 403 are deliberately absent: they usually mean the signer or
# the clock is the problem, so the next server would demand auth too and
# a failover buys a second prompt and a second failure.
_RETRYABLE_CODES = frozenset({ERROR_CODES.REDIRECT_REFUSED})


# ---------------------------------------------------------------------------
# Types and data classes
# ---------------------------------------------------------------------------

class BlobCache(Protocol):
    """The app's content-addressed byte cache.

    ``ThumbnailLoader`` is the implementation; it is a UI module and the
    store must not import it, so the object arrives as a constructor
    argument the way every other boundary in this codebase is crossed.
    Only the write half is named here, because writing is all the store
    does with it.
    """

    def put_bytes(self, data: bytes) -> str: ...


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
class UploadJobState:
    """Per-file state visible to the UI during an upload."""

    name: str
    progress: int = 0          # 0..100
    status: str = "queued"     # queued | signing | uploading | mirroring | done | failed
    error: str = ""
    hash: str = ""


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
    # emitted alongside this when every server failed, so the UI can
    # surface a clear message.
    fetch_finished = Signal()
    fetch_error = Signal(str)

    # Upload lifecycle.
    upload_started = Signal(str)                       # name
    upload_progress = Signal(str, int, int)            # name, sent, total
    upload_status = Signal(str, str)                   # name, status
    upload_finished = Signal(str, object)              # name, MediaFile
    upload_failed = Signal(str, str)                   # name, reason
    # Same failure, as a stable code. Emitted alongside ``upload_failed``
    # so a subscriber can branch on the code instead of string-matching
    # the reason. ``upload_failed`` stays: it is what the asset manager
    # reads today.
    upload_failed_code = Signal(str, str)              # name, code

    # Reroute toast trigger. Fired once per upload, after a server has
    # confirmed the blob, when that server is not the configured
    # primary.
    upload_rerouted = Signal(str, str, str)            # name, from_host, to_host

    # A mirror did not take the copy. Non-fatal: the primary has the
    # file, so the upload still succeeds and the document is untouched.
    mirror_failed = Signal(str, str, str)              # name, host, code

    # Delete lifecycle.
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

        self._files: Dict[str, MediaFile] = {}
        self._last_fetch_at: float = 0.0
        self._fetch_in_flight: bool = False
        self._mirror_by_default: bool = True
        # Hashes this process committed itself during this session. Their
        # library records were written from a server's own confirmation
        # rather than from a /list response of unknown age, which is what
        # makes them safe to dedup against without a probe.
        self._committed: Set[str] = set()

        # Track active upload jobs by display name so the UI can render
        # multiple parallel uploads. (Phase 1 we expect one at a time
        # but the shape supports more.)
        self._uploads: Dict[str, UploadJobState] = {}

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

    def file_list(
        self,
        *,
        filter_type: str = "all",
        sort_by: str = "newest",
    ) -> List[MediaFile]:
        """Filtered + sorted snapshot, matching STANDUP's options.

        ``filter_type`` ∈ {'all','image','video','audio'}; everything
        else falls under 'all'.
        ``sort_by`` ∈ {'newest','oldest','largest','smallest'}.
        """
        result = list(self._files.values())
        prefix = {"image": "image/", "video": "video/", "audio": "audio/"}.get(filter_type)
        if prefix:
            result = [f for f in result if (f.mime_type or "").startswith(prefix)]
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
        freshness window are no-ops unless ``force`` is set.

        Each server is walked with BUD-12 cursor pagination, reusing one
        signed list token for all of its pages: BUD-11 scopes a list
        token to a server and gives it no ``x`` tag, so an N-page walk
        still costs a single signer prompt.

        Quietly no-ops when no profile is active. The UI is expected to
        show its empty or connect-signer state in that case.
        """
        profile = self._profile_provider()
        if profile is None:
            return
        if self._fetch_in_flight:
            return
        if not force and (time.monotonic() - self._last_fetch_at) < _FETCH_FRESHNESS_SECONDS:
            return

        servers = self._target_servers()
        if not servers:
            return

        self._fetch_in_flight = True
        self.fetch_started.emit()

        merged: Dict[str, MediaFile] = {}
        remaining = {"count": len(servers), "errors": 0}
        # What the library held when the walk began. Anything committed
        # into ``self._files`` after this point is an upload that
        # finished mid-fetch, and a /list response is silent about
        # those: it is authoritative about what the server had, not
        # about what this process did while the request was in flight.
        started_with: Set[str] = set(self._files)

        def finish_one() -> None:
            remaining["count"] -= 1
            if remaining["count"] > 0:
                return
            self._fetch_in_flight = False
            self._last_fetch_at = time.monotonic()
            if remaining["errors"] >= len(servers):
                self.fetch_error.emit(
                    "Could not reach any Blossom server. Check your network or server list."
                )
            else:
                for sha, media in self._files.items():
                    if sha not in merged and sha not in started_with:
                        merged[sha] = media
                self._files = merged
                self.library_changed.emit()
            self.fetch_finished.emit()

        def attempt_server(server: str, *, retry_without_auth: bool = False) -> None:
            origin = server_origin(server)

            def do_list(auth_event: Optional[dict]) -> None:
                seen: Set[str] = set()
                request_page(origin, auth_event, cursor=None, page=1, seen=seen,
                             retry_without_auth=retry_without_auth,
                             server=server)

            if retry_without_auth:
                # Fallback path: skip the bunker round-trip entirely.
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
                    server, origin, BlossomError(reason), False
                ),
            )

        def request_page(
            origin: str,
            auth_event: Optional[dict],
            *,
            cursor: Optional[str],
            page: int,
            seen: Set[str],
            retry_without_auth: bool,
            server: str,
        ) -> None:
            self._client.list_for_pubkey(
                origin,
                profile.user_pubkey,
                auth_event,
                on_success=lambda items: handle_page(
                    server, origin, auth_event, items,
                    cursor=cursor, page=page, seen=seen,
                    retry_without_auth=retry_without_auth,
                ),
                on_failure=lambda err: handle_list_error(
                    server, origin, err, retry_without_auth
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
            retry_without_auth: bool,
        ) -> None:
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
                merge_entry(origin, sha, entry)

            # Stop conditions. Together they mean a server that ignores
            # the cursor and re-sends the same page forever is caught on
            # page two: it merges once and the walk ends, with no error
            # and no loop.
            if not items or len(items) < _LIST_PAGE_SIZE:
                finish_one()
                return
            if novel == 0 or echoed_cursor or not last_sha:
                finish_one()
                return
            if page >= _MAX_LIST_PAGES:
                finish_one()
                return
            request_page(origin, auth_event, cursor=last_sha, page=page + 1,
                         seen=seen, retry_without_auth=retry_without_auth,
                         server=server)

        def merge_entry(origin: str, sha: str, entry: dict) -> None:
            url = entry.get("url") or f"{origin}/{sha}"
            size = int(entry.get("size") or 0)
            mime = str(entry.get("type") or "application/octet-stream")
            uploaded = int(entry.get("uploaded") or entry.get("created") or 0)
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

        def handle_list_error(server: str, origin: str, err: BlossomError, already_retried: bool) -> None:
            # Match STANDUP: on 401/403 the server is telling us auth was
            # required but rejected; some operators reject mid-flight
            # because of a clock skew between the signer and the server.
            # Retry once without auth, since many servers serve /list
            # publicly and BUD-11 marks the token optional there.
            if not already_retried and err.status in (401, 403):
                attempt_server(server, retry_without_auth=True)
                return
            remaining["errors"] += 1
            finish_one()

        for server in servers:
            attempt_server(server)

    def clear(self) -> None:
        """Drop the library entirely. Used on profile switch / sign-out."""
        self._files = {}
        self._last_fetch_at = 0.0
        self._committed.clear()
        self.library_changed.emit()

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> None:
        """Read the file from disk and dispatch through the upload
        pipeline. Errors surface via ``upload_failed``.

        Display name is the file's basename so multiple parallel uploads
        of the same file would collide. Phase 1 we don't expect that;
        if it becomes a real case we'll switch to a per-job UUID.
        """
        profile = self._profile_provider()
        path = Path(file_path)
        name = path.name or "upload"
        if profile is None:
            self.upload_failed.emit(name, "Connect a Nostr signer first.")
            return
        try:
            body = path.read_bytes()
        except OSError as exc:
            self.upload_failed.emit(name, f"Could not read file: {exc}")
            return
        if not body:
            self.upload_failed.emit(name, "File is empty.")
            return

        mime, _ = mimetypes.guess_type(str(path))
        self.upload_bytes(body, name=name, mime_type=mime or "application/octet-stream")

    def upload_bytes(
        self,
        body: bytes,
        *,
        name: str,
        mime_type: str = "application/octet-stream",
    ) -> None:
        """Upload an in-memory buffer. Used by the drop handler when the
        bytes come from a QMimeData payload rather than a file path.

        The bytes are put in the local cache first, so nothing that
        happens next decides whether they can still be displayed.

        Nothing is signed until the store knows which servers actually
        need the bytes. Every eligible server that the library cannot
        vouch for is asked ``HEAD /<sha256>`` first, unsigned and in
        parallel, and the ones that already hold the blob are dropped
        from the plan. A file that is already everywhere finishes here:
        no signature, no PUT, no prompt.

        When the first eligible server fails in a way another server
        might not, the upload walks to the next one. It only ever walks
        the servers the planner accepted, so a server skipped for size
        can never be reached by a retry.
        """
        profile = self._profile_provider()
        if profile is None:
            self.upload_failed.emit(name, "Connect a Nostr signer first.")
            return
        if not body:
            self.upload_failed.emit(name, "Nothing to upload.")
            return

        servers = self._target_servers()
        if not servers:
            self.upload_failed.emit(name, "No Blossom servers configured.")
            return

        # The size plan runs before any dedup probe, so an oversized file
        # is refused without a single request leaving the process.
        plan: UploadPlan = plan_upload(len(body), servers)
        if plan.primary is None:
            # The number the user needs is the largest cap any configured
            # server publishes, not the size of their own file.
            limit_mb = max(1, max(get_effective_max_file(s) for s in servers) // (1024 * 1024))
            self.upload_failed.emit(
                name,
                f"File is too large for any configured server ({limit_mb} MiB). "
                "Try a smaller file or add a server that accepts it.",
            )
            return

        sha = hashlib.sha256(body).hexdigest()
        self._seed_cache(body)
        state = UploadJobState(name=name, status="queued", hash=sha)
        self._uploads[name] = state
        self.upload_started.emit(name)
        self.upload_status.emit(name, state.status)

        configured_primary = servers[0]
        eligible = list(plan.eligible)

        self._resolve_coverage(
            sha,
            eligible,
            on_ready=lambda present: self._upload_missing(
                name=name,
                body=body,
                mime_type=mime_type,
                profile=profile,
                sha=sha,
                eligible=eligible,
                present=present,
                configured_primary=configured_primary,
            ),
        )

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

    def _seed_cache(self, body: bytes) -> None:
        """Keep the bytes locally before they are sent anywhere.

        Without this the app downloads its own upload back from a server
        the moment it wants to show it, which is a round trip for bytes
        that were in memory a second earlier and simply fails when the
        machine is offline. The cache is content addressed, so seeding it
        here is the same write the download would have made.

        It happens before the network rather than after a successful
        upload on purpose: a failed upload the user retries still has
        something to show, and the local copy is what makes a Blossom
        server a place a file is kept rather than the only place.

        A cache that cannot be written is not an upload failure. The
        bytes are still going to a server, so the upload carries on and
        the display falls back to fetching them.
        """
        if self._blob_cache is None:
            return
        try:
            self._blob_cache.put_bytes(body)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Dedup: find out who already has the blob before signing anything
    # ------------------------------------------------------------------

    def _resolve_coverage(
        self,
        sha: str,
        eligible: List[str],
        *,
        on_ready: Callable[[List[str]], None],
    ) -> None:
        """Report which of ``eligible`` already hold ``sha``.

        Two sources, in trust order. The library map answers for free,
        but only when the record can be believed: one this process wrote
        itself this session, or one from a ``/list`` inside the freshness
        window. ``self._files`` is filled by ``/list`` alone, which only
        runs when the media library is opened, so in a typical session it
        is empty or arbitrarily old and a stale hit would be the reason
        an upload silently did not happen.

        Everything the map cannot vouch for is asked directly. The probe
        carries no authorization, so it costs no prompt, and an
        inconclusive answer counts as absent: dedup may never be the
        reason a blob failed to reach a server.
        """
        present: List[str] = []
        if self._record_is_trustworthy(sha):
            present = [s for s in eligible
                       if s in servers_holding(self._files.get(sha), eligible)]
        to_probe = [s for s in eligible if s not in present]
        if not to_probe:
            on_ready(present)
            return

        remaining = {"count": len(to_probe)}

        def one_done() -> None:
            remaining["count"] -= 1
            if remaining["count"] > 0:
                return
            on_ready([s for s in eligible if s in present])

        for server in to_probe:
            def found(target=server) -> None:
                present.append(target)
                one_done()

            self._client.head_blob(
                server_origin(server), sha,
                on_present=found,
                on_absent=one_done,
            )

    def _record_is_trustworthy(self, sha: str) -> bool:
        if sha in self._committed:
            return True
        return (time.monotonic() - self._last_fetch_at) < _FETCH_FRESHNESS_SECONDS

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
            )
            return

        # Servers this upload already tried and lost. They are not
        # offered the blob a second time as a mirror target: the request
        # would be new but the problem would be the same one.
        exhausted: List[str] = []

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

            job = self._uploads.get(name)
            if job is not None and job.status != "signing":
                job.status = "signing"
                self.upload_status.emit(name, job.status)

            def on_signed(auth_event: dict) -> None:
                job = self._uploads.get(name)
                if job is not None:
                    job.status = "uploading"
                    self.upload_status.emit(name, job.status)
                self._client.upload(
                    origin,
                    body,
                    mime_type,
                    auth_event,
                    on_success=lambda result: self._on_primary_uploaded(
                        name=name,
                        body=body,
                        profile=profile,
                        primary_result=result,
                        mirror_servers=mirrors,
                        configured_primary=configured_primary,
                    ),
                    on_failure=lambda err: on_attempt_failed(index, err),
                    on_progress=lambda sent, total: self._on_progress(name, sent, total),
                    sha256=sha,
                )

            self._sign_with_bunker(
                profile,
                unsigned,
                on_signed=on_signed,
                # A signer failure is not a server failure: the next
                # server would ask the same signer, so failing over just
                # buys a second prompt and a second refusal.
                on_failure=lambda reason: self._fail_upload(
                    name, _format_err(reason), code=ERROR_CODES.SIGNER_REJECTED
                ),
            )

        def on_attempt_failed(index: int, err: BlossomError) -> None:
            exhausted.append(missing[index])
            next_index = index + 1
            if (
                next_index < _MAX_UPLOAD_ATTEMPTS
                and next_index < len(missing)
                and _is_retryable(err)
            ):
                attempt(next_index)
                return
            self._fail_upload(name, _format_err(err), code=_error_code(err))

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
        state.progress = int(sent * 100 / total) if total > 0 else 0
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
    ) -> None:
        """Copy a blob one server already holds onto the rest, in turn.

        One server at a time on purpose. Each copy is a signature, and a
        parallel fan-out is a stack of approval popups the user has to
        clear one by one anyway, which is the flood the importer's loop
        was written to avoid.

        A mirror that refuses is reported and the walk carries on. The
        blob is already on a server, so failing the whole upload over a
        replication problem would take an image out of a document to
        punish a server the user does not control.
        """
        if not mirror_servers:
            self._commit_upload(name, media, configured_primary=configured_primary)
            return

        state = self._uploads.get(name)
        if state is not None:
            state.status = "mirroring"
            self.upload_status.emit(name, state.status)

        failures: List[tuple] = []

        def step(index: int) -> None:
            if index >= len(mirror_servers):
                for host, code in failures:
                    self.mirror_failed.emit(name, host, code)
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

            replicate.mirror_to_server(
                session_pool=self._session_pool,
                profile=profile,
                client=self._client,
                server=origin,
                source_url=source_url,
                sha256=media.hash,
                on_success=on_ok,
                on_failure=on_err,
            )

        step(0)

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
        # upload of the same bytes can trust it without a probe.
        self._committed.add(media.hash)
        state = self._uploads.pop(name, None)
        if state is not None:
            state.status = "done"
            state.progress = 100

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

    def _fail_upload(self, name: str, reason: str, *, code: str = "") -> None:
        state = self._uploads.pop(name, None)
        if state is not None:
            state.status = "failed"
            state.error = reason
        self.upload_status.emit(name, "failed")
        self.upload_failed.emit(name, reason)
        self.upload_failed_code.emit(name, code or ERROR_CODES.UPLOAD_FAILED)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_file(self, file_hash: str) -> None:
        """Delete a blob from every server it lives on.

        Files are deduped by sha256 across servers, so the same hash can
        be on N hosts; we issue a parallel DELETE to each, then commit
        the local removal once all of them have reported back. A delete
        is "successful" if at least one server confirmed it (or the
        whole list returned 404, meaning the blob is already gone). We only
        surface an error to the UI when every server actively rejected.
        """
        profile = self._profile_provider()
        if profile is None:
            self.delete_failed.emit(file_hash, "Connect a Nostr signer first.")
            return
        media = self._files.get(file_hash)
        if media is None:
            return

        # Build the unique set of servers to target. Source of truth is
        # the file's own ``urls`` (whatever ``/list`` told us), with the
        # blob URL as a fallback for files we have only one record of.
        target_servers: list[str] = []
        seen: set[str] = set()
        for entry in media.urls:
            origin = extract_server_from_blob_url(entry.get("url", ""))
            if origin and origin not in seen:
                target_servers.append(origin)
                seen.add(origin)
        if not target_servers:
            fallback = extract_server_from_blob_url(media.url)
            if fallback:
                target_servers.append(fallback)
            else:
                target_servers.append(self._settings.primary)

        # Track per-server outcomes so the UI gets a meaningful summary.
        remaining = {"count": len(target_servers)}
        successes: list[str] = []
        failures: list[tuple[str, str]] = []

        def finish_one() -> None:
            remaining["count"] -= 1
            if remaining["count"] > 0:
                return
            # Always drop the local record: the user said "remove this".
            self._files.pop(file_hash, None)
            if successes:
                self.file_deleted.emit(file_hash)
            else:
                summary = "; ".join(f"{_hostname(host)}: {msg}" for host, msg in failures)
                self.delete_failed.emit(
                    file_hash,
                    f"No server accepted the delete ({summary}). Removed locally.",
                )
            self.library_changed.emit()

        for origin in target_servers:
            self._delete_one_server(
                profile=profile,
                file_hash=file_hash,
                origin=origin,
                on_ok=lambda o=origin: (successes.append(o), finish_one()),
                on_err=lambda msg, o=origin: (failures.append((o, msg)), finish_one()),
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
        was asking for)."""
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
                    on_err(_format_err(err))

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
            on_failure=on_err,
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
                on_failure=lambda reason: on_failure(
                    f"signer rejected the Blossom auth event: {reason}"
                ),
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


def _origin_or_empty(value: str) -> str:
    try:
        return server_origin(value)
    except ValueError:
        return ""


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
    return ERROR_CODES.SIGNER_REJECTED


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


def _format_err(err) -> str:
    """User-facing copy for a failure.

    A :class:`BlossomError` becomes mapped copy plus the operator's own
    ``X-Reason``, never a Qt transport string and never protocol jargon.
    Anything else is a signer message and passes through byte-identical:
    the asset manager still classifies those by prefix, so rewording
    them here would silently reclassify every signer failure.
    """
    if isinstance(err, BlossomError):
        return friendly_message(
            err.code or ERROR_CODES.UPLOAD_FAILED, detail=err.detail
        )
    return str(err)
