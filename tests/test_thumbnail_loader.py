# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the blob loader's untrusted-boundary behaviour and its cache.

The defect classes this file guards against:
- a blob URL from a server response naming ``file://`` (verified: it
  read a local file into the cache),
- a redirect walking off the requested origin into the user's own
  machine,
- an unbounded stream buffered before any cap applies,
- SVG bytes reaching a renderer that resolves local file references,
- a world-readable cache recording which blobs the user viewed,
- the cache being written under the user's real ~/.config during tests.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWidgets import QApplication

from nostr.ui import thumbnail_loader as tl
from nostr.ui.thumbnail_loader import ThumbnailLoader
from tests.blossom_fakes import FakeNam, FakeReply


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


SVG_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<image xlink:href="/etc/passwd"/></svg>'
)
BLOB_URL = "https://blossom.example/blob"


def _png() -> bytes:
    img = QImage(4, 4, QImage.Format_RGB32)
    img.fill(QColor("teal"))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _loader(tmp_path, replies=None):
    nam = FakeNam(replies)
    return ThumbnailLoader(cache_dir=tmp_path, nam=nam), nam


def _signals(loader):
    ready, failed = [], []
    loader.ready.connect(lambda *args: ready.append(args))
    loader.failed.connect(lambda *args: failed.append(args))
    return ready, failed


def _cache_files(tmp_path):
    return sorted(p.name for p in Path(tmp_path).iterdir())


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def test_cache_dir_seam_never_touches_the_real_home(tmp_path, monkeypatch):
    def _explode():
        raise AssertionError("the real home directory was touched")

    monkeypatch.setattr(Path, "home", staticmethod(_explode))
    loader = ThumbnailLoader(cache_dir=tmp_path / "cache", nam=FakeNam())
    assert loader.cache_path("ab").parent == tmp_path / "cache"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_cache_directory_is_owner_only(tmp_path):
    _loader(tmp_path / "cache")
    assert (tmp_path / "cache").stat().st_mode & 0o777 == 0o700


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #

def test_request_allows_a_few_safe_redirects(tmp_path):
    loader, nam = _loader(tmp_path)
    loader.load(_sha(_png()), BLOB_URL)
    request = nam.calls[0][1]
    policy = request.attribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute)
    assert policy == QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy
    assert request.maximumRedirectsAllowed() == 4


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "http://evil.example/blob",
])
def test_an_unsafe_blob_url_is_never_fetched(tmp_path, url):
    loader, nam = _loader(tmp_path)
    ready, failed = _signals(loader)
    loader.load(_sha(_png()), url)
    assert nam.calls == []
    assert ready == []
    assert failed[0][1] == "blob URL was not allowed"
    assert _cache_files(tmp_path) == []


# --------------------------------------------------------------------------- #
# Response boundary
# --------------------------------------------------------------------------- #

def test_a_redirect_into_loopback_is_refused_before_the_body_is_read(tmp_path):
    data = _png()
    sha = _sha(data)
    reply = FakeReply(status=200, body=data, url="http://127.0.0.1:9/x")
    loader, nam = _loader(tmp_path, [reply])
    ready, failed = _signals(loader)
    loader.load(sha, BLOB_URL)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0][1] == "blob URL was not allowed"
    assert _cache_files(tmp_path) == []


def test_a_cdn_redirect_to_another_https_host_still_works(tmp_path):
    data = _png()
    sha = _sha(data)
    reply = FakeReply(status=200, body=data, url="https://cdn.example/blob")
    loader, nam = _loader(tmp_path, [reply])
    ready, failed = _signals(loader)
    loader.load(sha, BLOB_URL)
    nam.issued[0].finish()
    assert failed == []
    assert ready[0][0] == sha


def test_a_loopback_dev_server_still_serves_blobs(tmp_path):
    # http on loopback is the one plain-http case the media policy
    # allows, and a local Blossom server must keep working.
    data = _png()
    sha = _sha(data)
    url = "http://127.0.0.1:3000/" + sha
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=data, url=url)])
    ready, failed = _signals(loader)
    loader.load(sha, url)
    assert len(nam.calls) == 1
    nam.issued[0].finish()
    assert failed == []
    assert ready[0][0] == sha


def test_download_past_the_cap_is_aborted_mid_transfer(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=_png())])
    ready, failed = _signals(loader)
    loader.load(_sha(_png()), BLOB_URL)
    reply = nam.issued[0]
    reply.progress(tl._MAX_DOWNLOAD_BYTES + 1, -1)
    assert reply.aborted is True
    reply.finish()
    assert ready == []
    assert failed[0][1] == "blob exceeds cache limit"
    assert _cache_files(tmp_path) == []


def test_an_announced_oversize_is_refused_before_bytes_arrive(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=_png())])
    ready, failed = _signals(loader)
    loader.load(_sha(_png()), BLOB_URL)
    reply = nam.issued[0]
    reply.progress(0, tl._MAX_DOWNLOAD_BYTES + 1)
    assert reply.aborted is True
    reply.finish()
    assert failed[0][1] == "blob exceeds cache limit"


def test_svg_bytes_are_cached_but_never_decoded(tmp_path):
    sha = _sha(SVG_BYTES)
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=SVG_BYTES)])
    ready, failed = _signals(loader)
    loader.load(sha, BLOB_URL)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0] == (sha, "not an image")
    # The bytes are kept: they are valid content, just not renderable.
    assert loader.cache_path(sha).read_bytes() == SVG_BYTES


def test_a_cached_svg_is_refused_without_being_deleted(tmp_path):
    sha = _sha(SVG_BYTES)
    loader, nam = _loader(tmp_path)
    loader.cache_path(sha).write_bytes(SVG_BYTES)
    ready, failed = _signals(loader)
    loader.load(sha, BLOB_URL)
    assert nam.calls == []          # no endless re-download loop
    assert ready == []
    assert failed[0] == (sha, "not an image")
    assert loader.cache_path(sha).is_file()


def test_bytes_that_do_not_match_the_hash_are_refused(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=_png())])
    ready, failed = _signals(loader)
    loader.load("a" * 64, BLOB_URL)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0][1] == "downloaded bytes do not match sha256"


def test_a_corrupt_cache_entry_is_replaced(tmp_path):
    data = _png()
    sha = _sha(data)
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=data)])
    loader.cache_path(sha).write_bytes(b"tampered content")
    ready, failed = _signals(loader)
    loader.load(sha, BLOB_URL)
    assert len(nam.calls) == 1      # fell through to a re-download
    nam.issued[0].finish()
    assert failed == []
    assert ready[0][0] == sha
    assert loader.cache_path(sha).read_bytes() == data


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_cached_blob_files_are_owner_only(tmp_path):
    data = _png()
    sha = _sha(data)
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=data)])
    loader.load(sha, BLOB_URL)
    nam.issued[0].finish()
    assert loader.cache_path(sha).stat().st_mode & 0o777 == 0o600
    assert _cache_files(tmp_path) == [sha]   # no .tmp left behind


# --------------------------------------------------------------------------- #
# Byte store
# --------------------------------------------------------------------------- #

def test_put_bytes_and_has_round_trip(tmp_path):
    loader, _nam = _loader(tmp_path)
    data = _png()
    sha = loader.put_bytes(data)
    assert sha == _sha(data)
    assert loader.has(sha) is True
    assert loader.cache_path(sha).read_bytes() == data


def test_put_bytes_of_existing_content_is_a_no_op(tmp_path):
    loader, _nam = _loader(tmp_path)
    data = _png()
    sha = loader.put_bytes(data)
    written = loader.cache_path(sha).stat().st_mtime_ns
    assert loader.put_bytes(data) == sha
    assert loader.cache_path(sha).stat().st_mtime_ns == written
    assert _cache_files(tmp_path) == [sha]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_put_bytes_writes_an_owner_only_file(tmp_path):
    loader, _nam = _loader(tmp_path)
    sha = loader.put_bytes(_png())
    assert loader.cache_path(sha).stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_put_bytes_raises_when_the_cache_cannot_be_written(tmp_path):
    if os.getuid() == 0:
        pytest.skip("root ignores directory permissions")
    loader, _nam = _loader(tmp_path)
    os.chmod(tmp_path, 0o500)
    try:
        with pytest.raises(OSError):
            loader.put_bytes(_png())
    finally:
        os.chmod(tmp_path, 0o700)


def test_has_is_false_for_an_unknown_hash(tmp_path):
    loader, _nam = _loader(tmp_path)
    assert loader.has("b" * 64) is False


# --------------------------------------------------------------------------- #
# The ciphertext fetch, which the public-copy path uses
# --------------------------------------------------------------------------- #

def _fetch(loader, nam, url):
    """Drive ``fetch`` and collect whichever side answered."""
    got, refused = [], []
    loader.fetch(url, on_success=got.append, on_failure=refused.append)
    if nam.issued:
        nam.issued[0].finish()
    return got, refused


def test_the_fetch_hands_back_the_bytes_at_one_url(tmp_path):
    body = b"\x02" + b"\x11" * 48
    loader, _nam = _loader(tmp_path, [FakeReply(status=200, body=body)])
    got, refused = _fetch(loader, _nam, BLOB_URL)
    assert got == [body] and refused == []


def test_the_fetch_caches_nothing(tmp_path):
    # The copy maker hashes and verifies the bytes itself, and an
    # envelope has no business being filed under an address the rest of
    # the app resolves as a picture.
    body = b"\x02" + b"\x22" * 48
    loader, _nam = _loader(tmp_path, [FakeReply(status=200, body=body)])
    _fetch(loader, _nam, BLOB_URL)
    assert _cache_files(tmp_path) == []


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "data:application/octet-stream;base64,AAAA",
    "http://evil.example/blob",
])
def test_the_fetch_applies_the_same_url_policy(tmp_path, url):
    loader, nam = _loader(tmp_path)
    got, refused = _fetch(loader, nam, url)
    assert nam.calls == []
    assert got == []
    assert refused == ["blob URL was not allowed"]


def test_the_fetch_refuses_bytes_served_from_loopback_after_a_redirect(tmp_path):
    reply = FakeReply(status=200, body=b"\x02" * 64, url="http://127.0.0.1:9/x")
    loader, _nam = _loader(tmp_path, [reply])
    got, refused = _fetch(loader, _nam, BLOB_URL)
    assert got == []
    assert refused == ["blob URL was not allowed"]


def test_the_fetch_reports_a_transport_failure_rather_than_empty_bytes(tmp_path):
    from PySide6.QtNetwork import QNetworkReply

    reply = FakeReply(error=QNetworkReply.HostNotFoundError,
                      error_string="host not found")
    loader, _nam = _loader(tmp_path, [reply])
    got, refused = _fetch(loader, _nam, BLOB_URL)
    assert got == []
    assert refused == ["host not found"]
