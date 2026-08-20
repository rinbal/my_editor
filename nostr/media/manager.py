# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The asset lifecycle: ingest, resolution, and the upload queue.

``AssetManager`` is the one object the editor talks to about images. It
is local-first by construction: bytes are hashed and written to the
content-addressed cache before anything else, the asset is usable in a
document from that moment, and uploading is background enrichment. No
network, signer or server failure can remove or block an image.

Everything that crosses a boundary is injected: the blob store, the
uploader, the profile provider and the image decoder. That keeps this
module free of protocol and Qt-widget code, and lets the tests run with
no network, no relay, no signer and no real home directory.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Protocol, Tuple

from PySide6.QtCore import QObject, Signal

import url_safety

from export_html import sniff_image_mime

# The one BUD-03 hash-from-URL rule, imported rather than repeated: a
# third copy of it is how the copies drift apart. ``blob_url`` builds the
# canonical address BUD-01 guarantees, which is what makes a recovery
# attempt against a sibling server honest rather than a guess.
from ..blossom.hashes import blob_url, hash_from_url
from .assets import (
    LEGAL_TRANSITIONS,
    AssetIndex,
    AssetState,
    DocumentAsset,
    _acceptable_remote_url,
    is_sha256,
    parse_asset_key,
)


# Formats this app will adopt. SVG is excluded on purpose: Qt's SVG
# decoder resolves external references out of the document, and an
# adopted image is decoded in this process. Foreign SVG already inside a
# document is never adopted and passes through untouched.
ADOPTABLE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
)

# Matches the blob cache's own download cap so both halves of the cache
# agree on what can exist in it.
MAX_ASSET_BYTES = 25 * 1024 * 1024

_DATA_URI_RE = re.compile(r"^data:([^,]*),(.*)$", re.DOTALL)

# How many sibling servers one dead blob URL may be worth. The address
# in the index has already been tried by the ordinary path, so this
# counts the extra requests recovery itself issues. Four is a budget,
# not a target: a document full of broken images must not turn into a
# fan-out against servers the user never chose.
_MAX_RECOVERY_CANDIDATES = 4


class AssetErrorCodes:
    """Failure codes this manager emits.

    MUST match ``nostr/blossom/errors.py``; the equality is pinned by a
    test so the two cannot drift apart.
    """

    SIGNER_REJECTED = "SIGNER_REJECTED"
    UPLOAD_FAILED = "UPLOAD_FAILED"


# Prefixes MediaStore produces today when the signer is the reason an
# upload never started. Temporary until the store reports structured
# codes; pinned by a test so a copy change fails loudly instead of
# silently reclassifying every signer failure.
_SIGNER_REASON_PREFIXES = ("signer rejected", "Connect a Nostr signer")


class BlobStore(Protocol):
    """The content-addressed byte cache (``ThumbnailLoader`` in the app).

    Also emits ``ready(sha256, path, pixmap)`` and
    ``failed(sha256, reason)``.
    """

    def cache_path(self, sha256: str) -> Path: ...
    def has(self, sha256: str) -> bool: ...
    def put_bytes(self, data: bytes) -> str: ...
    def load(self, sha256: str, url: str) -> None: ...


class Uploader(Protocol):
    """The upload orchestrator (``MediaStore`` in the app).

    Exposes ``files``: a sha256 to media-record map whose records carry
    ``hash``, ``url``, ``urls``, ``mime_type`` and ``size``. Also emits
    ``upload_status(name, status)``, ``upload_finished(name, media)`` and
    ``upload_failed(name, reason)``.
    """

    def upload_bytes(self, body: bytes, *, name: str, mime_type: str) -> None: ...


@dataclass(frozen=True)
class ExportAsset:
    """What an exporter needs to embed one asset.

    ``data`` is empty when the cache misses; exporters treat that as
    unavailable and fall through their own ladders.
    """

    sha256: str
    data: bytes = b""
    mime: str = "application/octet-stream"
    remote_url: str = ""
    width: int = 0
    height: int = 0
    alt: str = ""


class AssetManager(QObject):
    """Owns every asset in the session: ingest, resolve, upload, retry."""

    asset_added = Signal(str)                # sha256
    asset_changed = Signal(str)              # sha256, bytes arrived or state moved
    asset_upload_failed = Signal(str, str)   # sha256, stable code

    def __init__(
        self,
        *,
        blob_store: BlobStore,
        uploader: Uploader,
        profile_provider: Callable[[], Optional[object]],
        decoder: Optional[Callable[[bytes], Optional[object]]] = None,
        index: Optional[AssetIndex] = None,
        recovery_provider: Optional[Callable[[str], List[str]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._blob_store = blob_store
        self._uploader = uploader
        self._profile_provider = profile_provider
        self._decoder = decoder
        self._index = index if index is not None else AssetIndex()
        # Origins to try when a stored blob URL goes dead, supplied by
        # the BUD-03 policy object. None means no recovery at all, which
        # is the behaviour every caller had before this seam existed.
        self._recovery_provider = recovery_provider

        self._queue: Deque[str] = deque()
        self._inflight: Optional[str] = None
        self._jobs: Dict[str, Tuple[str, int]] = {}   # job name -> (sha, attempts)
        self._fetching: set = set()
        self._recovery: Dict[str, List[str]] = {}    # sha -> candidates left
        self._recovered: set = set()                 # shas whose ladder ran

        uploader.upload_status.connect(self._on_upload_status)
        uploader.upload_finished.connect(self._on_upload_finished)
        uploader.upload_failed.connect(self._on_upload_failed)
        blob_store.ready.connect(self._on_blob_ready)
        blob_store.failed.connect(self._on_blob_failed)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    @property
    def index(self) -> AssetIndex:
        return self._index

    def get(self, sha256: str) -> Optional[DocumentAsset]:
        return self._index.get(sha256)

    def __contains__(self, sha256: object) -> bool:
        return isinstance(sha256, str) and self._index.get(sha256) is not None

    def find_by_url(self, url: str) -> Optional[DocumentAsset]:
        """The asset a published URL refers to, or None. Read only.

        After a ``.md`` save and reopen, an image this app uploaded comes
        back as a plain ``https`` name, so the hash in the URL is the
        only route back to the record holding its mime, size and pixel
        size. Deliberately strict about which URLs qualify: content
        addressing means a stranger hosting the same picture matches by
        hash too, and describing their copy as ours is exactly the
        silent rewrite that had to be removed from the save path. The
        URL must also be one this app was told about, either the exact
        address the upload returned or a server that confirmed the blob.

        Nothing is written, and nothing is rewritten in either
        direction. The only effect of a match is that the publisher may
        describe the image it was already going to publish.
        """
        if not isinstance(url, str) or not url:
            return None
        sha = hash_from_url(url)
        if sha is None:
            return None
        asset = self._index.get(sha)
        if asset is None:
            return None
        if asset.remote_url and url == asset.remote_url:
            return asset
        for origin in asset.servers:
            if url_safety.same_origin(url, origin):
                return asset
        return None

    def can_upload(self) -> bool:
        """Whether an upload could start at all, i.e. a profile is active.

        Callers use it to choose their copy; the queue itself never asks,
        because a refusal from the uploader is an ordinary failure that
        leaves the asset in the document.
        """
        return self._profile_provider() is not None

    def display_label(self, sha256: str) -> str:
        asset = self._index.get(sha256)
        if asset is not None and asset.alt:
            return asset.alt
        return (sha256 or "")[:12]

    def unuploaded_of(self, names: Iterable[str]) -> List[str]:
        """The asset keys among ``names`` that are not uploaded yet.

        Deduplicated in first-seen order, so one image used twice counts
        once. Callers turn a key back into a hash with ``parse_asset_key``
        when they need to request the upload.
        """
        blocked: List[str] = []
        seen: set = set()
        for name in names:
            sha = parse_asset_key(name)
            if sha is None or name in seen:
                continue
            seen.add(name)
            asset = self._index.get(sha)
            if asset is None or not asset.is_uploaded:
                blocked.append(name)
        return blocked

    def failed_assets(self) -> List[DocumentAsset]:
        return [a for a in self._index.values() if a.upload_state is AssetState.FAILED]

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def adopt_bytes(
        self,
        data: bytes,
        *,
        mime: str = "",
        alt: str = "",
        caption: str = "",
    ) -> Optional[DocumentAsset]:
        """Take ownership of image bytes. Returns None when refused.

        The bytes decide the type, never the caller and never a server,
        so ``mime`` is only a hint and the sniffed value wins.
        """
        if not data:
            return None
        sha = hashlib.sha256(data).hexdigest()

        existing = self._index.get(sha)
        if existing is not None:
            if alt and not existing.alt:
                existing.alt = alt
            if caption and not existing.caption:
                existing.caption = caption
            existing.updated_at = int(time.time())
            if not self._blob_store.has(sha):
                # The cache was cleared under a known asset and the bytes
                # are in hand; putting them back costs nothing and keeps
                # the image resolvable.
                try:
                    self._blob_store.put_bytes(data)
                except OSError:
                    pass
            self._index.put(existing)
            return existing

        sniffed = sniff_image_mime(data)
        if sniffed not in ADOPTABLE_MIMES:
            return None
        if len(data) > MAX_ASSET_BYTES:
            return None
        try:
            self._blob_store.put_bytes(data)
        except OSError:
            return None

        width, height = self._dimensions_of(data)
        now = int(time.time())
        asset = DocumentAsset(
            sha256=sha,
            mime=sniffed,
            size=len(data),
            alt=alt,
            caption=caption,
            width=width,
            height=height,
            upload_state=AssetState.LOCAL,
            created_at=now,
            updated_at=now,
        )
        self._index.put(asset)
        self.asset_added.emit(sha)
        return asset

    def adopt_file(
        self, path: str, *, alt: str = "", caption: str = ""
    ) -> Optional[DocumentAsset]:
        """Adopt a file from disk, verbatim.

        The original is never read again: the cache copy is the app's
        copy, byte for byte, so nothing re-encodes the user's file.
        """
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        return self.adopt_bytes(data, alt=alt, caption=caption)

    def adopt_data_uri(self, uri: str) -> Optional[str]:
        """Adopt ``data:<mime>;base64,<payload>``, returning its key."""
        if not isinstance(uri, str):
            return None
        match = _DATA_URI_RE.match(uri.strip())
        if match is None:
            return None
        meta, payload = match.group(1), match.group(2)
        if "base64" not in [part.strip().lower() for part in meta.split(";")]:
            return None
        try:
            data = base64.b64decode(payload)
        except (binascii.Error, ValueError):
            return None
        asset = self.adopt_bytes(data)
        return asset.key if asset is not None else None

    def adopt_library_file(
        self,
        *,
        sha256: str,
        remote_url: str,
        mime: str = "application/octet-stream",
        size: int = 0,
        alt: str = "",
    ) -> Optional[DocumentAsset]:
        """Adopt a blob that is already hosted, e.g. a library pick."""
        sha = (sha256 or "").lower()
        if not is_sha256(sha):
            return None
        if not _acceptable_remote_url(remote_url, sha):
            return None

        existing = self._index.get(sha)
        if existing is not None:
            if not existing.remote_url:
                existing.remote_url = remote_url
                idle = sha != self._inflight and sha not in self._queue
                if idle and existing.upload_state in (AssetState.LOCAL, AssetState.FAILED):
                    # Learning the blob is already hosted is an ingest
                    # fact, not a queue transition: it re-enters at
                    # COMPLETE exactly as a fresh library adopt would.
                    self._enter_complete(existing, remote_url, existing.servers)
            if alt and not existing.alt:
                existing.alt = alt
            existing.updated_at = int(time.time())
            self._index.put(existing)
            return existing

        now = int(time.time())
        asset = DocumentAsset(
            sha256=sha,
            mime=str(mime or "application/octet-stream"),
            size=int(size or 0),
            alt=alt,
            upload_state=AssetState.COMPLETE,
            remote_url=remote_url,
            created_at=now,
            updated_at=now,
        )
        self._index.put(asset)
        self.asset_added.emit(sha)
        return asset

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_bytes(self, key_or_name: str) -> Optional[bytes]:
        """Cached bytes for an asset key, or None."""
        sha = parse_asset_key(key_or_name)
        if sha is None:
            return None
        if not self._blob_store.has(sha):
            return None
        try:
            return self._blob_store.cache_path(sha).read_bytes()
        except OSError:
            return None

    def resolve_image(self, name: str):
        """Decode an asset key to an image for the editor, or None.

        A miss for an asset whose blob lives on a server starts one
        fetch, once: the editor caches the placeholder it shows, so a
        second document holding the same key must not start a second
        download.
        """
        sha = parse_asset_key(name)
        if sha is None:
            return None
        data = self.resolve_bytes(name)
        if data is not None:
            return self._decoder(data) if self._decoder is not None else None

        asset = self._index.get(sha)
        if asset is not None and asset.remote_url and sha not in self._fetching:
            self._fetching.add(sha)
            self._blob_store.load(sha, asset.remote_url)
        return None

    def export_view(self, name: str) -> Optional[ExportAsset]:
        """What an exporter needs for one asset key, or None."""
        sha = parse_asset_key(name)
        if sha is None:
            return None
        asset = self._index.get(sha)
        if asset is None:
            return None
        return ExportAsset(
            sha256=sha,
            data=self.resolve_bytes(name) or b"",
            mime=asset.mime,
            remote_url=asset.remote_url,
            width=asset.width,
            height=asset.height,
            alt=asset.alt,
        )

    # ------------------------------------------------------------------
    # Upload queue: one flight at a time, stops on failure
    # ------------------------------------------------------------------

    def request_upload(self, sha256: str) -> None:
        """Queue an asset for upload. Only LOCAL and FAILED are eligible."""
        sha = (sha256 or "").lower()
        asset = self._index.get(sha)
        if asset is None:
            return
        if asset.upload_state not in (AssetState.LOCAL, AssetState.FAILED):
            return

        media = self._library_record(sha)
        if media is not None:
            url = str(getattr(media, "url", "") or "")
            if _acceptable_remote_url(url, sha):
                # Already on a server: no signer prompt, no redundant PUT.
                self._enter_complete(asset, url, _servers_of(media))
                return

        if asset.upload_state is AssetState.FAILED:
            asset.attempts += 1
        asset.failure_code = ""
        asset.failure_reason = ""
        if not self._set_state(asset, AssetState.QUEUED):
            return
        if sha not in self._queue:
            self._queue.append(sha)
        self._pump()

    def retry(self, sha256: str) -> None:
        asset = self._index.get(sha256)
        if asset is None or not asset.can_retry:
            return
        self.request_upload(asset.sha256)

    def retry_all_failed(self) -> None:
        for asset in self.failed_assets():
            self.request_upload(asset.sha256)

    def flush(self) -> None:
        """Persist pending metadata. Called when the window closes."""
        self._index.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        if self._inflight is not None:
            return
        while self._queue:
            sha = self._queue.popleft()
            asset = self._index.get(sha)
            if asset is None or asset.upload_state is not AssetState.QUEUED:
                continue
            body = self.resolve_bytes(asset.key)
            if not body:
                self._fail(asset, AssetErrorCodes.UPLOAD_FAILED, "cached bytes missing")
                return
            self._inflight = sha
            self._jobs[asset.key] = (sha, asset.attempts)
            self._set_state(asset, AssetState.SIGNING)
            # The uploader may fail synchronously and re-enter the slots
            # below, so nothing may be touched after this call returns.
            self._uploader.upload_bytes(body, name=asset.key, mime_type=asset.mime)
            return

    def _job_asset(self, name: str) -> Optional[DocumentAsset]:
        """The asset behind a job name, or None when it is not ours.

        Also rejects a late callback from a previous attempt: the retry
        that superseded it bumped ``attempts``.
        """
        job = self._jobs.get(name)
        if job is None:
            return None
        sha, attempts = job
        asset = self._index.get(sha)
        if asset is None or asset.attempts != attempts:
            return None
        return asset

    def _on_upload_status(self, name: str, status: str) -> None:
        asset = self._job_asset(name)
        if asset is None:
            return
        if status == "uploading":
            self._set_state(asset, AssetState.UPLOADING)

    def _on_upload_finished(self, name: str, media: object) -> None:
        asset = self._job_asset(name)
        if asset is None:
            return
        url = str(getattr(media, "url", "") or "")
        if not _acceptable_remote_url(url, asset.sha256):
            # A hostile or mismatched URL must never enter the index.
            # The client validates first; this is the second net.
            self._fail(
                asset,
                AssetErrorCodes.UPLOAD_FAILED,
                "server returned an unusable blob URL",
            )
            return
        size = getattr(media, "size", 0)
        if not asset.size and isinstance(size, int) and size > 0:
            asset.size = size
        self._jobs.pop(name, None)
        if self._inflight == asset.sha256:
            self._inflight = None
        self._enter_complete(asset, url, _servers_of(media))
        self._pump()

    def _on_upload_failed(self, name: str, reason: str) -> None:
        asset = self._job_asset(name)
        if asset is None:
            return
        text = str(reason or "")
        code = (
            AssetErrorCodes.SIGNER_REJECTED
            if text.startswith(_SIGNER_REASON_PREFIXES)
            else AssetErrorCodes.UPLOAD_FAILED
        )
        self._fail(asset, code, text)

    def _on_blob_ready(self, sha256: str, path: str, pixmap: object) -> None:
        sha = (sha256 or "").lower()
        if sha not in self._fetching:
            return
        self._fetching.discard(sha)
        self._recovery.pop(sha, None)
        asset = self._index.get(sha)
        if asset is None:
            return
        if not asset.width or not asset.height:
            width, height = _size_of(pixmap)
            if width and height:
                asset.width, asset.height = width, height
        asset.updated_at = int(time.time())
        self._index.put(asset)
        self.asset_changed.emit(sha)

    def _on_blob_failed(self, sha256: str, reason: str) -> None:
        # Nothing destructive: the asset keeps every field it had and the
        # next resolve may try again.
        sha = (sha256 or "").lower()
        self._fetching.discard(sha)
        self._try_recovery(sha)

    # ------------------------------------------------------------------
    # BUD-03 recovery: a dead URL is not a dead blob
    # ------------------------------------------------------------------

    def _try_recovery(self, sha: str) -> None:
        """Ask the next sibling server for a blob whose URL went dead.

        BUD-03 describes exactly this flow: read the hash out of the
        URL, consult the author's servers, and try each one in order.
        The bytes are what is trusted, not the host that served them,
        because the blob store verifies the sha256 before adopting
        anything. That verification is what lets this contact a server
        the user never chose without sending a credential to it.

        Strictly read only. Nothing here rewrites ``remote_url``, the
        document, or the modified flag: ``remote_url`` is the string the
        save path and the publish path serialize, so repointing it at
        whichever server happened to answer would silently rewrite the
        user's file and their next published event. That is the rewrite
        AD-4 exists to prevent, wearing a different hat.
        """
        if self._blob_store.has(sha):
            # The bytes are already here, so the failure was about
            # rendering them, not about finding them. Content addressing
            # guarantees a sibling server holds the same bytes, so asking
            # one would be four requests to learn nothing.
            return
        queue = self._recovery.get(sha)
        if queue is None:
            if sha in self._recovered:
                # The ladder already ran for this hash. A repaint asks
                # again for every unresolved image, so remembering the
                # negative outcome is what stops one broken picture from
                # re-issuing its whole ladder on every keystroke.
                return
            queue = self._recovery_candidates(sha)
            if not queue:
                return
            self._recovered.add(sha)
            self._recovery[sha] = queue

        if not queue:
            self._recovery.pop(sha, None)
            return
        url = queue.pop(0)
        if not queue:
            self._recovery.pop(sha, None)
        # One request at a time: the next candidate is only reached when
        # this one fails. A parallel fan-out would be a load problem for
        # the servers and a privacy problem for the user.
        self._fetching.add(sha)
        self._blob_store.load(sha, url)

    def _recovery_candidates(self, sha: str) -> List[str]:
        """Sibling addresses for ``sha``, most trusted first.

        Order: the servers that confirmed this blob, then whatever the
        provider offers, which is the user's own configuration followed
        by the published kind 10063 in its own order. The URL already in
        the index is not included; it is the address that just failed.

        BUD-03's optional step 4, falling back to a well-known popular
        Blossom server, is deliberately declined. It broadcasts a hash
        the user is interested in to a server neither party named, and
        the spec makes it a MAY.
        """
        if self._recovery_provider is None:
            return []
        asset = self._index.get(sha)
        if asset is None or not asset.remote_url:
            return []
        try:
            offered = list(self._recovery_provider(sha) or [])
        except Exception:  # noqa: BLE001 - a provider fault must not break resolve
            offered = []

        candidates: List[str] = []
        for origin in list(asset.servers) + offered:
            if not isinstance(origin, str) or not origin:
                continue
            url = blob_url(origin, sha)
            if url in candidates:
                continue
            if url_safety.same_origin(url, asset.remote_url):
                continue
            if not url_safety.is_safe_media_url(url):
                continue
            candidates.append(url)
            if len(candidates) >= _MAX_RECOVERY_CANDIDATES:
                break
        return candidates

    def _fail(self, asset: DocumentAsset, code: str, reason: str) -> None:
        asset.failure_code = code
        asset.failure_reason = reason
        # Stop the whole queue: the next job would pop another signer
        # prompt at the worst possible moment.
        self._drain_queue(code)
        self._jobs.pop(asset.key, None)
        if self._inflight == asset.sha256:
            self._inflight = None
        self._set_state(asset, AssetState.FAILED)
        self.asset_upload_failed.emit(asset.sha256, code)

    def _drain_queue(self, code: str) -> None:
        """Empty the queue without stranding what was waiting in it.

        Only LOCAL and FAILED assets can be requested again, so an asset
        left QUEUED after the queue stops could never be uploaded. They
        enter FAILED instead, which one retry brings back. No per-asset
        failure signal: the queue stopped, these uploads never ran.
        """
        while self._queue:
            sha = self._queue.popleft()
            asset = self._index.get(sha)
            if asset is None:
                continue
            asset.failure_code = code
            asset.failure_reason = "the upload queue stopped after an earlier failure"
            self._set_state(asset, AssetState.FAILED)

    def _enter_complete(
        self, asset: DocumentAsset, url: str, servers: List[str]
    ) -> None:
        asset.remote_url = url
        if servers:
            asset.servers = servers
        asset.failure_code = ""
        asset.failure_reason = ""
        if asset.upload_state is AssetState.SIGNING:
            # A server can answer before the "uploading" status arrives.
            self._set_state(asset, AssetState.UPLOADING)
        if asset.upload_state in (AssetState.UPLOADING, AssetState.MIRRORED):
            self._set_state(asset, AssetState.COMPLETE)
        else:
            # Ingest of an already hosted blob, not a queue transition.
            asset.upload_state = AssetState.COMPLETE
            asset.updated_at = int(time.time())
            self._index.put(asset)
            self.asset_changed.emit(asset.sha256)

    def _set_state(self, asset: DocumentAsset, new_state: AssetState) -> bool:
        """Move ``asset`` if the move is legal. Late callbacks are normal,
        so an illegal or repeated move is a silent no-op."""
        current = asset.upload_state
        if new_state is current:
            return False
        if new_state not in LEGAL_TRANSITIONS.get(current, frozenset()):
            return False
        asset.upload_state = new_state
        asset.updated_at = int(time.time())
        self._index.put(asset)
        self.asset_changed.emit(asset.sha256)
        return True

    def _library_record(self, sha: str) -> Optional[object]:
        try:
            files = self._uploader.files
        except AttributeError:
            return None
        if not isinstance(files, dict):
            return None
        return files.get(sha)

    def _dimensions_of(self, data: bytes) -> Tuple[int, int]:
        if self._decoder is None:
            return 0, 0
        return _size_of(self._decoder(data))


def _size_of(image: object) -> Tuple[int, int]:
    """Pixel size of a QImage or QPixmap, or (0, 0) when unavailable."""
    if image is None:
        return 0, 0
    try:
        width = int(image.width())
        height = int(image.height())
    except (AttributeError, TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    return width, height


def _servers_of(media: object) -> List[str]:
    """The server origins a media record was confirmed on."""
    entries = getattr(media, "urls", None)
    if not isinstance(entries, list):
        return []
    servers: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        server = entry.get("server")
        if isinstance(server, str) and server and server not in servers:
            servers.append(server)
    return servers
