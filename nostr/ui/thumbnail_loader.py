# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Blossom blob thumbnail loader and content-addressed byte store.

Downloads image blobs to a disk cache and emits a ``QPixmap`` for the
caller (the media grid in the Library dialog, and the preview lightbox).
``put_bytes`` / ``has`` make the same cache the app's local blob store,
so bytes are durable before any upload is attempted.

Cache layout: ~/.config/my_editor/blossom_cache/<sha256>
The filename is the content hash, so the cache is content-addressed and
never needs invalidation.

The bytes are a stranger's until proven otherwise: the URL is checked
against the media policy before the request and again after redirects,
the transfer is capped mid-flight, the hash is verified, and the decode
goes through an explicit format allowlist rather than Qt sniffing.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

import url_safety
from image_safety import decode_image_bytes


CACHE_DIR = Path.home() / ".config" / "my_editor" / "blossom_cache"

# Hard upper bound on thumbnail downloads. The library is restricted to
# files the user uploaded themselves, so they can't accidentally pull a
# multi-gigabyte object, but a malicious server returning an unbounded
# stream still has to be stopped.
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB
_HTTP_TIMEOUT_MS = 30_000

# A content fetch carries no credentials, and CDN hops are normal, so
# redirects are followed. Qt's no-less-safe policy refuses an https to
# http downgrade; the final URL is re-validated in the handler because
# that policy still allows https to https into a loopback address.
_MAX_REDIRECTS = 4

_UNSAFE_URL_REASON = "blob URL was not allowed"


class ThumbnailLoader(QObject):
    """Resolve a Blossom blob URL to a local file path + QPixmap.

    Always keyed by sha256; the URL is only used when the cache misses.
    Concurrent requests for the same hash coalesce.
    """

    ready = Signal(str, str, object)   # sha256, local_path, QPixmap
    failed = Signal(str, str)          # sha256, reason

    # A NIP-23 ``image`` tag is a bare URL with no hash, so the API
    # above cannot resolve it: it is keyed by content hash and verifies
    # the bytes against the caller's sha256. These two carry the URL
    # instead. Separate signals rather than reusing the pair above, so
    # no existing listener, all of which match on a known sha, ever sees
    # a payload it cannot interpret.
    url_ready = Signal(str, str, object)   # url, sha256, QPixmap
    url_failed = Signal(str, str)          # url, reason

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        cache_dir=None,
        nam=None,
    ) -> None:
        super().__init__(parent)
        # Both seams exist for tests: no test may write to the real
        # ~/.config, and none may touch a network.
        self._cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        _chmod(self._cache_dir, 0o700)
        self._nam = nam or QNetworkAccessManager(self)
        self._inflight: Dict[str, QNetworkReply] = {}
        self._inflight_urls: Dict[str, QNetworkReply] = {}
        # url → sha256, learned from a completed download, so a repeat
        # request is served from the content-addressed cache with no
        # network at all.
        self._url_sha: Dict[str, str] = {}

    def cache_path(self, sha256: str) -> Path:
        return self._cache_dir / sha256.lower()

    # -- byte store --------------------------------------------------------

    def has(self, sha256: str) -> bool:
        return self.cache_path(sha256).is_file()

    def put_bytes(self, data: bytes) -> str:
        """Store ``data`` under its own sha256 and return that hash.

        Raises OSError when the cache cannot be written, so callers can
        degrade instead of silently losing the bytes.
        """
        sha = hashlib.sha256(data).hexdigest()
        path = self.cache_path(sha)
        if path.is_file():
            return sha
        _write_cache_file(path, data)
        return sha

    # -- resolution --------------------------------------------------------

    def load(self, sha256: str, url: str) -> None:
        """Asynchronously resolve the blob. Emits ``ready`` on success or
        ``failed`` on any error. Idempotent: a second call for the same
        hash while a request is in flight is a no-op (the in-flight reply
        will fire ``ready`` for both callers via signal broadcast)."""
        sha = sha256.lower()
        path = self.cache_path(sha)
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            if data and hashlib.sha256(data).hexdigest() == sha:
                image = decode_image_bytes(data)
                if image is None:
                    # Valid bytes, just not a format this process will
                    # render. Keep them: re-downloading forever is the
                    # bug the old code had here.
                    self.failed.emit(sha, "not an image")
                    return
                self.ready.emit(sha, str(path), QPixmap.fromImage(image))
                return
            # Truly corrupt entry: the bytes do not hash to their name.
            try:
                path.unlink()
            except OSError:
                pass
        if sha in self._inflight:
            return
        if not url_safety.is_safe_media_url(url):
            self.failed.emit(sha, _UNSAFE_URL_REASON)
            return
        reply, oversize = self._start(url)
        self._inflight[sha] = reply
        reply.finished.connect(
            lambda s=sha, r=reply, p=path, u=url: self._on_reply(
                s, r, p, oversize, u)
        )

    def load_url(self, url: str) -> None:
        """Resolve an image whose bytes are not known in advance.

        Everything ``load`` guards against is guarded against here, and
        by the same code: the pre-request policy check, the no-less-safe
        redirect policy and its hop limit, the re-validation of the
        final URL after redirects, the mid-flight size abort, and the
        decode allowlist. The one difference is that the sha256 is
        *computed from the bytes* rather than compared to one the caller
        already knew, because a NIP-23 ``image`` tag carries no hash.

        Callers are expected to apply their own, tighter gate first. The
        media policy accepts plain http on loopback so a local Blossom
        dev server works, and a URL that arrived inside someone else's
        content should never reach the user's own machine.
        """
        sha = self._url_sha.get(url, "")
        if sha:
            path = self.cache_path(sha)
            if path.is_file():
                try:
                    data = path.read_bytes()
                except OSError:
                    data = b""
                if data and hashlib.sha256(data).hexdigest() == sha:
                    image = decode_image_bytes(data)
                    if image is None:
                        self.url_failed.emit(url, "not an image")
                    else:
                        self.url_ready.emit(url, sha, QPixmap.fromImage(image))
                    return
        if url in self._inflight_urls:
            return
        if not url_safety.is_safe_media_url(url):
            self.url_failed.emit(url, _UNSAFE_URL_REASON)
            return
        reply, oversize = self._start(url)
        self._inflight_urls[url] = reply
        reply.finished.connect(
            lambda r=reply, u=url: self._on_url_reply(u, r, oversize)
        )

    def fetch(
        self,
        url: str,
        *,
        on_success,
        on_failure,
    ) -> None:
        """Hand back one URL's bytes, satisfying ``CiphertextFetcher``.

        The copy maker needs the raw bytes at one address and nothing
        else: it hashes them itself, because trusting a fetcher to do
        that is how a substituted blob gets decrypted. So this caches
        nothing and decodes nothing, and exists here rather than as a
        second downloader so that the policy check, the redirect rules,
        the hop limit, the final-URL revalidation and the size cap are
        the same code that guards every other download in the app.
        """
        if not url_safety.is_safe_media_url(url):
            on_failure(_UNSAFE_URL_REASON)
            return
        reply, oversize = self._start(url)

        def _done(r=reply, u=url) -> None:
            try:
                data, reason = self._settled_bytes(r, oversize, u)
                if reason:
                    on_failure(reason)
                else:
                    on_success(data)
            finally:
                r.deleteLater()

        reply.finished.connect(_done)

    # -- shared request plumbing ------------------------------------------

    def _start(self, url: str):
        """Issue the GET both entry points use, with the same guards."""
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", b"my-editor-blossom-thumb/1")
        request.setTransferTimeout(_HTTP_TIMEOUT_MS)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setMaximumRedirectsAllowed(_MAX_REDIRECTS)
        reply = self._nam.get(request)
        oversize = {"hit": False}

        def _size_guard(received: int, total: int, r=reply) -> None:
            if oversize["hit"]:
                return
            if received > _MAX_DOWNLOAD_BYTES or (
                total > 0 and total > _MAX_DOWNLOAD_BYTES
            ):
                oversize["hit"] = True
                r.abort()

        reply.downloadProgress.connect(_size_guard)
        return reply, oversize

    def _settled_bytes(self, reply, oversize: dict, requested_url: str):
        """``(data, reason)``: the body, or why it may not be read.

        One copy of the response boundary, shared by both entry points,
        so a fix to any of these checks cannot land on one and miss the
        other.
        """
        if oversize["hit"]:
            return b"", "blob exceeds cache limit"
        if reply.error() != QNetworkReply.NoError:
            return b"", reply.errorString() or "network error"
        # Redirects were followed, so the bytes may come from an origin
        # the caller never named; validate where they came from before
        # reading them.
        if not _final_url_allowed(requested_url, reply.url().toString()):
            return b"", _UNSAFE_URL_REASON
        data = bytes(reply.readAll())
        if not data:
            return b"", "empty response"
        if len(data) > _MAX_DOWNLOAD_BYTES:
            return b"", "blob exceeds cache limit"
        return data, ""

    def _on_reply(
        self,
        sha: str,
        reply: QNetworkReply,
        path: Path,
        oversize: dict,
        requested_url: str,
    ) -> None:
        self._inflight.pop(sha, None)
        try:
            data, reason = self._settled_bytes(reply, oversize, requested_url)
            if reason:
                self.failed.emit(sha, reason)
                return
            # Validate the bytes match the hash before trusting them.
            actual = hashlib.sha256(data).hexdigest()
            if actual != sha:
                self.failed.emit(sha, "downloaded bytes do not match sha256")
                return
            image = decode_image_bytes(data)
            if image is None:
                # Non-image blob, or a format outside the decode
                # allowlist. Still cache it but tell the caller there is
                # no pixmap to show.
                try:
                    _write_cache_file(path, data)
                except OSError:
                    pass
                self.failed.emit(sha, "not an image")
                return
            try:
                _write_cache_file(path, data)
            except OSError:
                pass
            self.ready.emit(sha, str(path), QPixmap.fromImage(image))
        finally:
            reply.deleteLater()

    def _on_url_reply(self, url: str, reply: QNetworkReply, oversize: dict) -> None:
        self._inflight_urls.pop(url, None)
        try:
            data, reason = self._settled_bytes(reply, oversize, url)
            if reason:
                self.url_failed.emit(url, reason)
                return
            sha = hashlib.sha256(data).hexdigest()
            image = decode_image_bytes(data)
            # Cached either way, and the url → sha mapping recorded
            # either way, so a refusal is remembered as cheaply as a
            # success and neither is re-downloaded.
            try:
                _write_cache_file(self.cache_path(sha), data)
            except OSError:
                pass
            self._url_sha[url] = sha
            if image is None:
                self.url_failed.emit(url, "not an image")
                return
            self.url_ready.emit(url, sha, QPixmap.fromImage(image))
        finally:
            reply.deleteLater()


def _final_url_allowed(requested: str, final: str) -> bool:
    """Whether bytes served from ``final`` may be read.

    The media policy alone is not enough here: it accepts loopback so a
    local dev server works, which would let a public server redirect
    into 127.0.0.1 and turn the app into a probe of the user's own
    machine. Staying on the requested origin is always fine; moving
    origin is fine only to a host the mirror policy accepts, which
    refuses loopback, link-local and private IP literals.
    """
    if not url_safety.is_safe_media_url(final):
        return False
    if url_safety.same_origin(requested, final):
        return True
    return url_safety.is_safe_mirror_source(final)


def _chmod(target, mode: int) -> None:
    """Best-effort permissions. On Windows chmod only toggles the
    read-only bit, and a failure here must never stop startup."""
    try:
        os.chmod(target, mode)
    except OSError:
        pass


def _write_cache_file(path: Path, data: bytes) -> None:
    """Atomically place ``data`` at ``path``, owner-readable only.

    The cache records which blobs the user looked at, so it is kept as
    private as the config directory it lives in. An aborted write leaves
    no temp file behind.
    """
    fd, tmp_path = tempfile.mkstemp(
        prefix=".blob_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
