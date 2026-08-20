# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kind 0 profile metadata + avatar image loader.

Two pieces:

  ProfileMetadataFetcher subscribes to the configured relays for a
    user's ``kind:0`` event, parses the content JSON, writes the resolved
    name/picture/nip05 back to the profile store, and emits the updated
    Profile.

  AvatarLoader downloads the ``picture`` URL via QtNetwork to a local
    cache directory, then emits the resulting QPixmap so the chip can
    refresh.

These are separate because the metadata fetch is fast (subscribe, then
parse small JSON) while the avatar download is a potentially slow HTTPS
round-trip that should never block UI updates.

An avatar URL comes from a stranger's kind 0 event, so it is treated as
hostile input: the scheme and host are checked before the request and
again after redirects, the transfer is capped mid-flight, and the bytes
are decoded through an explicit format allowlist rather than Qt
sniffing (which hands SVG to a renderer that reads local files).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QStandardPaths, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

import url_safety
from image_safety import decode_image_bytes

from . import DEFAULT_RELAYS
from .profiles import Profile, ProfileStore
from .queries import fetch_latest_event
from .relay import RelayPool


AVATAR_CACHE_DIR = Path.home() / ".cache" / "my_editor" / "nostr_avatars"
# Soft cap on avatar bytes: a malicious or accidental 100 MB image must
# not land in the cache and stall the editor.
_MAX_AVATAR_BYTES: int = 2_000_000
# Curate how long we'll wait for an HTTP response before giving up.
_AVATAR_HTTP_TIMEOUT_MS: int = 10_000
# Avatars live behind CDNs and vanity redirectors, so hops are normal.
# Qt's no-less-safe policy refuses an https to http downgrade; the final
# URL is re-checked in the handler because that policy still permits a
# redirect into a private address.
_MAX_AVATAR_REDIRECTS: int = 4


# --------------------------------------------------------------------------- #
# Metadata (kind 0) fetcher                                                   #
# --------------------------------------------------------------------------- #

class ProfileMetadataFetcher(QObject):
    """Background loader for a profile's kind 0 event.

    Signals:
      updated(Profile)  emitted after the profile store has been
                        mutated with the freshly resolved fields.
      failed(str)       emitted with a short reason if no kind 0
                        event could be retrieved.
    """

    updated = Signal(object)   # Profile
    failed = Signal(str)

    def __init__(
        self,
        pool: RelayPool,
        store: ProfileStore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._pool = pool
        self._store = store

    def fetch(self, profile: Profile, *, timeout_ms: int = 8_000) -> None:
        # Try the curated set first; the bunker's own relays usually mirror
        # the user's preferred set, so include them too.
        relays = list(
            dict.fromkeys(list(DEFAULT_RELAYS) + list(profile.bunker_relays))
        )

        def _on_done(event: Optional[dict]) -> None:
            if event is None:
                self.failed.emit("no metadata event found")
                return
            try:
                fields = json.loads(event.get("content", "") or "{}")
            except json.JSONDecodeError:
                self.failed.emit("metadata content was not JSON")
                return
            if not isinstance(fields, dict):
                self.failed.emit("metadata content was not a JSON object")
                return

            display_name = (
                fields.get("display_name") or fields.get("name") or ""
            ).strip()
            picture = (fields.get("picture") or "").strip()
            nip05 = (fields.get("nip05") or "").strip()

            current = self._store.get(profile.user_pubkey)
            if current is None:
                # Profile was removed while we were fetching; bail quietly.
                return
            current.display_name = display_name
            current.picture = picture
            current.nip05 = nip05
            current.metadata_cached_at = int(event.get("created_at", 0))
            self._store.upsert(current)
            self.updated.emit(current)

        fetch_latest_event(
            self._pool,
            relays,
            filters=[{"kinds": [0], "authors": [profile.user_pubkey], "limit": 1}],
            on_done=_on_done,
            timeout_ms=timeout_ms,
            parent=self,
        )


# --------------------------------------------------------------------------- #
# Avatar loader (QtNetwork)                                                   #
# --------------------------------------------------------------------------- #

class AvatarLoader(QObject):
    """Downloads avatar images and caches them to disk.

    Signals:
      ready(pubkey_hex, QPixmap)  fired when a valid pixmap is available,
                                  either from the on-disk cache or after
                                  a successful HTTP download.
      failed(pubkey_hex, str)     fired when the request can't be served
                                  (skipped, network error, bad payload,
                                  oversized). Always fires exactly once
                                  per ``load()`` call alongside ``ready``,
                                  or alone on failure, which a throttled
                                  batcher needs so it can free its slot
                                  regardless of outcome.
    """

    ready = Signal(str, object)   # pubkey_hex, QPixmap
    failed = Signal(str, str)     # pubkey_hex, reason

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        cache_dir=None,
        nam=None,
    ) -> None:
        super().__init__(parent)
        # Seams for tests: no test writes the real cache or opens a socket.
        self._cache_dir = Path(cache_dir) if cache_dir else AVATAR_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # The cache lists everyone the user follows, which is a
        # behavioural fingerprint; keep it owner-only.
        _chmod(self._cache_dir, 0o700)
        self._nam = nam or QNetworkAccessManager(self)
        # pubkey -> reply, so a re-request for the same pubkey can be coalesced
        self._inflight: dict[str, QNetworkReply] = {}

    def load(self, pubkey_hex: str, picture_url: str) -> None:
        """Resolve an avatar for ``pubkey_hex`` from ``picture_url``.

        Cache-first: if we've successfully downloaded this exact URL before,
        the local file is loaded and the network is not touched.
        """
        if not picture_url:
            self.failed.emit(pubkey_hex, "no picture URL")
            return
        if not url_safety.is_safe_mirror_source(picture_url):
            # file:// paths, data: URLs, userinfo tricks and IP literals
            # pointing back into the local network are refused. Plain
            # http passes: real avatars still live on plain-http hosts.
            self.failed.emit(pubkey_hex, "unsupported URL scheme")
            return
        if pubkey_hex in self._inflight:
            return  # already downloading for this pubkey, and its
                    # eventual signal covers both callers

        cache_path = self._cache_path(pubkey_hex, picture_url)
        if cache_path.is_file():
            image = _decode_cached(cache_path)
            if image is not None:
                self.ready.emit(pubkey_hex, QPixmap.fromImage(image))
                return

        request = QNetworkRequest(QUrl(picture_url))
        request.setTransferTimeout(_AVATAR_HTTP_TIMEOUT_MS)
        # Identify ourselves so a vain image host doesn't reject the request.
        request.setRawHeader(b"User-Agent", b"my-editor-nostr-chip/1")
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setMaximumRedirectsAllowed(_MAX_AVATAR_REDIRECTS)
        reply = self._nam.get(request)
        self._inflight[pubkey_hex] = reply
        oversize = {"hit": False}

        def _size_guard(received: int, total: int, r=reply) -> None:
            if oversize["hit"]:
                return
            if received > _MAX_AVATAR_BYTES or (
                total > 0 and total > _MAX_AVATAR_BYTES
            ):
                oversize["hit"] = True
                r.abort()

        reply.downloadProgress.connect(_size_guard)
        reply.finished.connect(
            lambda pk=pubkey_hex, r=reply, p=cache_path: self._on_reply(
                pk, r, p, oversize)
        )

    # -- internals ---------------------------------------------------------

    def _on_reply(
        self,
        pubkey_hex: str,
        reply: QNetworkReply,
        cache_path: Path,
        oversize: dict,
    ) -> None:
        self._inflight.pop(pubkey_hex, None)
        try:
            if oversize["hit"]:
                self.failed.emit(
                    pubkey_hex, f"avatar exceeds {_MAX_AVATAR_BYTES} bytes")
                return
            if reply.error() != QNetworkReply.NoError:
                self.failed.emit(pubkey_hex, reply.errorString() or "network error")
                return
            # Redirects were followed, so check where the bytes actually
            # came from before reading them.
            if not url_safety.is_safe_mirror_source(reply.url().toString()):
                self.failed.emit(pubkey_hex, "unsupported URL scheme")
                return
            data = bytes(reply.readAll())
            if not data:
                self.failed.emit(pubkey_hex, "empty response")
                return
            if len(data) > _MAX_AVATAR_BYTES:
                self.failed.emit(pubkey_hex, f"avatar exceeds {_MAX_AVATAR_BYTES} bytes")
                return
            image = decode_image_bytes(data)
            if image is None:
                self.failed.emit(pubkey_hex, "image decode failed")
                return
            _write_private_file(cache_path, data)
            self.ready.emit(pubkey_hex, QPixmap.fromImage(image))
        finally:
            reply.deleteLater()

    def _cache_path(self, pubkey_hex: str, picture_url: str) -> Path:
        """Cache key includes a hash of the URL so a URL change invalidates."""
        url_hash = hashlib.sha256(picture_url.encode("utf-8")).hexdigest()[:12]
        return self._cache_dir / f"{pubkey_hex}-{url_hash}"


# --------------------------------------------------------------------------- #
# Utilities                                                                    #
# --------------------------------------------------------------------------- #

def _decode_cached(path: Path) -> Optional[object]:
    """Decode a cached avatar file through the format allowlist."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return decode_image_bytes(data)


def _chmod(target, mode: int) -> None:
    """Best-effort permissions; on Windows chmod only toggles read-only
    and a failure here must never stop startup."""
    try:
        os.chmod(target, mode)
    except OSError:
        pass


def _write_private_file(path: Path, data: bytes) -> None:
    """Atomically cache ``data`` at ``path``, owner-readable only.

    A cache miss next time is fine, so every failure here is swallowed:
    the caller already has the pixmap.
    """
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".avatar_", suffix=".tmp", dir=str(path.parent)
        )
    except OSError:
        return
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
