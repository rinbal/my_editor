# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory Blossom media library + upload orchestration.

This is the layer the UI talks to. It owns:
  - the per-hash file map (dedup across mirror servers)
  - a 30 s freshness window on /list calls so the library, the picker
    and the editor can all call ``fetch()`` on mount without fanning
    out N×M requests
  - the upload pipeline: hash → plan → sign auth → PUT /upload → sign
    auth for each mirror → PUT /mirror → merge URLs back into the map
  - the delete pipeline: sign auth → DELETE → remove from map

Signing happens via the existing ``BunkerSessionPool``. The store keeps
no key material of its own.

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
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from ..bunker import BunkerClient, BunkerSessionPool
from ..profiles import Profile
from .auth import build_blossom_auth_event
from .client import (
    BlossomClient,
    BlossomError,
    UploadResult,
    extract_server_from_blob_url,
    looks_like_sha256,
    server_origin,
)
from .plan import plan_upload, UploadPlan
from .settings import BlossomSettings


# How long a fetched library stays "fresh". A second call within this
# window is a no-op (matches STANDUP's 30 s constant).
_FETCH_FRESHNESS_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MediaFile:
    """One unique blob in the library, deduped by sha256 across servers.

    ``urls`` lists every server that confirmed it has this hash. The
    first entry is the "primary URL" the UI uses for previews and
    inserts; ``url`` mirrors it for callers that just want a single
    string.

    ``width`` and ``height`` are populated lazily by the UI once a
    thumbnail decode produces a QPixmap; ``0`` means "not yet known".
    """

    hash: str
    url: str
    urls: List[dict] = field(default_factory=list)   # [{server, url}, …]
    mime_type: str = "application/octet-stream"
    size: int = 0
    alt: str = ""
    uploaded_at_ms: int = 0
    width: int = 0
    height: int = 0


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

    # Reroute toast trigger — fired once per upload when the configured
    # primary couldn't take the file and a mirror absorbed it instead.
    upload_rerouted = Signal(str, str, str)            # name, from_host, to_host

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
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session_pool = session_pool
        self._profile_provider = profile_provider
        self._settings = settings or BlossomSettings()
        self._client = client or BlossomClient(parent=self)

        self._files: Dict[str, MediaFile] = {}
        self._last_fetch_at: float = 0.0
        self._fetch_in_flight: bool = False
        self._mirror_by_default: bool = True

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

        Quietly no-ops when no profile is active — the UI is expected to
        show its empty/connect-signer state in that case.
        """
        profile = self._profile_provider()
        if profile is None:
            return
        if self._fetch_in_flight:
            return
        if not force and (time.monotonic() - self._last_fetch_at) < _FETCH_FRESHNESS_SECONDS:
            return

        servers = list(self._settings.configured_servers)
        if not servers:
            return

        self._fetch_in_flight = True
        self.fetch_started.emit()

        # Per-fetch accumulator: server index → list[server_response_items]
        merged: Dict[str, MediaFile] = {}
        remaining = {"count": len(servers), "errors": 0}

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
                self._files = merged
                self.library_changed.emit()
            self.fetch_finished.emit()

        def attempt_server(server: str, *, retry_without_auth: bool = False) -> None:
            origin = server_origin(server)

            def do_list(auth_event: Optional[dict]) -> None:
                self._client.list_for_pubkey(
                    origin,
                    profile.user_pubkey,
                    auth_event,
                    on_success=lambda items: handle_list(server, origin, items),
                    on_failure=lambda err: handle_list_error(server, origin, err, retry_without_auth),
                )

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

        def handle_list(server: str, origin: str, items: list) -> None:
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                sha = (entry.get("sha256") or "").lower()
                if not looks_like_sha256(sha):
                    continue
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
            finish_one()

        def handle_list_error(server: str, origin: str, err: BlossomError, already_retried: bool) -> None:
            # Match STANDUP: on 401/403 the server is telling us auth was
            # required but rejected; some operators reject mid-flight
            # because of a clock skew between the signer and the server.
            # Retry once without auth — many servers serve /list publicly.
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
        self.library_changed.emit()

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str) -> None:
        """Read the file from disk and dispatch through the upload
        pipeline. Errors surface via ``upload_failed``.

        Display name is the file's basename so multiple parallel uploads
        of the same file would collide — Phase 1 we don't expect that;
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
        bytes come from a QMimeData payload rather than a file path."""
        profile = self._profile_provider()
        if profile is None:
            self.upload_failed.emit(name, "Connect a Nostr signer first.")
            return
        if not body:
            self.upload_failed.emit(name, "Nothing to upload.")
            return

        servers = list(self._settings.configured_servers)
        if not servers:
            self.upload_failed.emit(name, "No Blossom servers configured.")
            return

        plan: UploadPlan = plan_upload(len(body), servers)
        if plan.primary is None:
            limit_mb = max(1, max(len(body) for _ in [1]) // (1024 * 1024))
            self.upload_failed.emit(
                name,
                f"File is too large for any configured server ({limit_mb} MiB). "
                "Try a smaller file or add a server that accepts it.",
            )
            return

        sha = hashlib.sha256(body).hexdigest()
        state = UploadJobState(name=name, status="signing", hash=sha)
        self._uploads[name] = state
        self.upload_started.emit(name)
        self.upload_status.emit(name, state.status)

        if plan.rerouted:
            from_host = _hostname(servers[0])
            to_host = _hostname(plan.primary)
            if from_host and to_host:
                self.upload_rerouted.emit(name, from_host, to_host)

        primary = plan.primary
        mirrors = [s for s in plan.eligible if s != primary]
        primary_origin = server_origin(primary)

        unsigned = build_blossom_auth_event(
            "upload",
            file_hash=sha,
            server=primary_origin,
            pubkey_hex=profile.user_pubkey,
        )

        def on_signed(auth_event: dict) -> None:
            state.status = "uploading"
            self.upload_status.emit(name, state.status)
            self._client.upload(
                primary_origin,
                body,
                mime_type,
                auth_event,
                on_success=lambda result: self._on_primary_uploaded(
                    name=name,
                    body=body,
                    profile=profile,
                    primary_result=result,
                    mirror_servers=mirrors,
                ),
                on_failure=lambda err: self._fail_upload(name, _format_err(err)),
                on_progress=lambda sent, total: self._on_progress(name, sent, total),
            )

        self._sign_with_bunker(
            profile,
            unsigned,
            on_signed=on_signed,
            on_failure=lambda reason: self._fail_upload(name, _format_err(reason)),
        )

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
        )

        if not mirror_servers:
            self._commit_upload(name, media)
            return

        state = self._uploads.get(name)
        if state is not None:
            state.status = "mirroring"
            self.upload_status.emit(name, state.status)

        remaining = {"count": len(mirror_servers)}

        def finalize_if_done() -> None:
            remaining["count"] -= 1
            if remaining["count"] <= 0:
                self._commit_upload(name, media)

        for mirror in mirror_servers:
            origin = server_origin(mirror)
            unsigned = build_blossom_auth_event(
                "upload",
                server=origin,
                pubkey_hex=profile.user_pubkey,
            )

            def make_handlers(origin=origin):
                def on_signed(auth_event: dict) -> None:
                    self._client.mirror(
                        origin,
                        primary_url,
                        auth_event,
                        on_success=lambda result, o=origin: handle_ok(o, result),
                        on_failure=lambda err, o=origin: handle_err(o, err),
                    )

                def handle_ok(o: str, result: UploadResult) -> None:
                    media.urls.append({"server": o, "url": result["url"]})
                    finalize_if_done()

                def handle_err(o: str, err: BlossomError) -> None:
                    # Mirror failures are non-fatal — the primary already
                    # has the file. We just don't add this server to the
                    # urls list.
                    finalize_if_done()

                return on_signed

            self._sign_with_bunker(
                profile,
                unsigned,
                on_signed=make_handlers(),
                on_failure=lambda _reason: finalize_if_done(),
            )

    def _commit_upload(self, name: str, media: MediaFile) -> None:
        # Dedupe with anything already in the library — if the same hash
        # was already there, prefer the new URL list (it's freshest).
        self._files[media.hash] = media
        state = self._uploads.pop(name, None)
        if state is not None:
            state.status = "done"
            state.progress = 100
        self.upload_status.emit(name, "done")
        self.upload_finished.emit(name, media)
        self.library_changed.emit()

    def _fail_upload(self, name: str, reason: str) -> None:
        state = self._uploads.pop(name, None)
        if state is not None:
            state.status = "failed"
            state.error = reason
        self.upload_status.emit(name, "failed")
        self.upload_failed.emit(name, reason)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_file(self, file_hash: str) -> None:
        """Delete a blob from every server it lives on.

        Files are deduped by sha256 across servers, so the same hash can
        be on N hosts; we issue a parallel DELETE to each, then commit
        the local removal once all of them have reported back. A delete
        is "successful" if at least one server confirmed it (or the
        whole list returned 404 — the blob is already gone). We only
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
            # Always drop the local record — the user said "remove this".
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

def _hostname(url: str) -> str:
    try:
        return server_origin(url).split("://", 1)[1]
    except ValueError:
        return url


def _format_err(err) -> str:
    if isinstance(err, BlossomError):
        if err.status:
            return f"{err.reason} (HTTP {err.status})"
        return err.reason
    return str(err)
