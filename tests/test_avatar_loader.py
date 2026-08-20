# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the avatar loader's untrusted-boundary behaviour.

A picture URL comes from a stranger's kind 0 event, so this path is
reachable by anyone the user's follows can name. The defect classes
guarded against here:
- ``file:`` and ``data:`` URLs, and IP literals pointing back into the
  local network, being fetched,
- a redirect landing somewhere the initial check would have refused,
- an unbounded stream buffered before any cap applies,
- an SVG avatar reaching a renderer that reads local files,
- the avatar cache, which lists everyone the user follows, being
  world-readable or written under the real home directory in tests.
"""

from __future__ import annotations

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

from nostr import metadata
from nostr.metadata import AvatarLoader
from tests.blossom_fakes import FakeNam, FakeReply


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


PUBKEY = "ab" * 32
AVATAR_URL = "https://images.example/me.png"
SVG_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<image xlink:href="/etc/passwd"/></svg>'
)


def _png() -> bytes:
    img = QImage(4, 4, QImage.Format_RGB32)
    img.fill(QColor("teal"))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _loader(tmp_path, replies=None):
    nam = FakeNam(replies)
    return AvatarLoader(cache_dir=tmp_path, nam=nam), nam


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
    loader = AvatarLoader(cache_dir=tmp_path / "avatars", nam=FakeNam())
    assert (tmp_path / "avatars").is_dir()
    assert loader is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_cache_directory_is_owner_only(tmp_path):
    _loader(tmp_path / "avatars")
    assert (tmp_path / "avatars").stat().st_mode & 0o777 == 0o700


# --------------------------------------------------------------------------- #
# URL policy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:9/avatar.png",
    "http://10.0.0.1/avatar.png",
    "https://user:pw@evil.example@good.example/a.png",
])
def test_a_refused_url_is_never_fetched(tmp_path, url):
    loader, nam = _loader(tmp_path)
    ready, failed = _signals(loader)
    loader.load(PUBKEY, url)
    assert nam.calls == []
    assert ready == []
    assert failed[0] == (PUBKEY, "unsupported URL scheme")


def test_plain_http_avatars_still_work(tmp_path):
    # Real avatars live on plain-http hosts; refusing them would be a
    # compatibility loss, not a security win.
    data = _png()
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=data)])
    ready, failed = _signals(loader)
    loader.load(PUBKEY, "http://images.example/me.png")
    assert len(nam.calls) == 1
    nam.issued[0].finish()
    assert failed == []
    assert ready[0][0] == PUBKEY


def test_request_allows_a_few_safe_redirects(tmp_path):
    loader, nam = _loader(tmp_path)
    loader.load(PUBKEY, AVATAR_URL)
    request = nam.calls[0][1]
    policy = request.attribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute)
    assert policy == QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy
    assert request.maximumRedirectsAllowed() == 4


# --------------------------------------------------------------------------- #
# Response boundary
# --------------------------------------------------------------------------- #

def test_a_redirect_into_the_local_network_is_refused(tmp_path):
    reply = FakeReply(status=200, body=_png(),
                      url="http://169.254.169.254/latest/meta-data/")
    loader, nam = _loader(tmp_path, [reply])
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0] == (PUBKEY, "unsupported URL scheme")
    assert _cache_files(tmp_path) == []


def test_download_past_the_cap_is_aborted_mid_transfer(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=_png())])
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    reply = nam.issued[0]
    reply.progress(metadata._MAX_AVATAR_BYTES + 1, -1)
    assert reply.aborted is True
    reply.finish()
    assert ready == []
    assert "exceeds" in failed[0][1]
    assert _cache_files(tmp_path) == []


def test_an_announced_oversize_is_refused_before_bytes_arrive(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=_png())])
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    reply = nam.issued[0]
    reply.progress(0, metadata._MAX_AVATAR_BYTES + 1)
    assert reply.aborted is True
    reply.finish()
    assert "exceeds" in failed[0][1]


def test_an_svg_avatar_is_never_decoded(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=SVG_BYTES)])
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0] == (PUBKEY, "image decode failed")


def test_a_cached_svg_avatar_is_never_decoded(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=SVG_BYTES)])
    cache_path = loader._cache_path(PUBKEY, AVATAR_URL)
    cache_path.write_bytes(SVG_BYTES)
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    # The cached file is unusable, so the fetch is re-issued rather than
    # a renderer being handed the bytes.
    assert len(nam.calls) == 1
    assert ready == []
    nam.issued[0].finish()
    assert failed[0] == (PUBKEY, "image decode failed")


def test_a_cached_png_avatar_is_served_without_a_request(tmp_path):
    loader, nam = _loader(tmp_path)
    cache_path = loader._cache_path(PUBKEY, AVATAR_URL)
    cache_path.write_bytes(_png())
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    assert nam.calls == []
    assert failed == []
    assert ready[0][0] == PUBKEY


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_cached_avatar_files_are_owner_only(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=_png())])
    loader.load(PUBKEY, AVATAR_URL)
    nam.issued[0].finish()
    cache_path = loader._cache_path(PUBKEY, AVATAR_URL)
    assert cache_path.stat().st_mode & 0o777 == 0o600
    assert _cache_files(tmp_path) == [cache_path.name]   # no .tmp left behind


def test_an_empty_response_reports_failure(tmp_path):
    loader, nam = _loader(tmp_path, [FakeReply(status=200, body=b"")])
    ready, failed = _signals(loader)
    loader.load(PUBKEY, AVATAR_URL)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0] == (PUBKEY, "empty response")
