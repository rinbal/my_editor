# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Table tests for the three URL policies.

The defect classes this file guards against:
- a server response naming ``file://`` or ``data:`` being treated as a
  fetchable media URL (demonstrated: it read a local file into the
  cache),
- userinfo tricks that make a hostile host look like a trusted one,
- an IPv6 origin losing its brackets and becoming unparseable,
- the mirror and avatar policies tightening far enough to break the
  plain-http images that exist all over the real web.
"""

from __future__ import annotations

import pytest

from url_safety import (
    UnsafeUrlError,
    host_of,
    is_safe_external_url,
    is_safe_media_url,
    is_safe_mirror_source,
    origin_of,
    require_safe_media_url,
    same_origin,
)


MEDIA_OK = [
    "https://blossom.band/abc",
    "https://blossom.band:8443/abc",
    "http://localhost/abc",
    "http://localhost:3000/abc",
    "http://127.0.0.1:3000/abc",
    "http://[::1]:3000/abc",
]

MEDIA_REFUSED = [
    "http://evil.example/i.png",
    "file:///etc/passwd",
    "data:text/html,x",
    "javascript:alert(1)",
    "smb://fileserver/share/x",
    "https://user:pw@evil.example@good.example/",
    "https:///no-host",
    "",
    "   ",
]


@pytest.mark.parametrize("url", MEDIA_OK)
def test_media_policy_accepts(url):
    assert is_safe_media_url(url) is True
    assert require_safe_media_url(url) == url


@pytest.mark.parametrize("url", MEDIA_REFUSED)
def test_media_policy_refuses(url):
    assert is_safe_media_url(url) is False
    with pytest.raises(UnsafeUrlError):
        require_safe_media_url(url)


def test_media_policy_pins_the_allowed_origin():
    assert is_safe_media_url("https://a.example/x",
                             allowed_origin="https://a.example") is True
    assert is_safe_media_url("https://A.Example/x",
                             allowed_origin="https://a.example/") is True
    assert is_safe_media_url("https://b.example/x",
                             allowed_origin="https://a.example") is False
    assert is_safe_media_url("https://a.example:8443/x",
                             allowed_origin="https://a.example") is False


def test_external_policy_allows_plain_http_but_not_other_schemes():
    # A release page or a PDF link may legitimately be plain http.
    assert is_safe_external_url("http://evil.example") is True
    assert is_safe_external_url("https://example.com/a") is True
    for url in ("file:///etc/passwd", "data:text/html,x",
                "javascript:alert(1)", "smb://host/share",
                "https://user:pw@evil.example@good.example/", "", "nonsense"):
        assert is_safe_external_url(url) is False


MIRROR_OK = [
    "http://blog.example/i.png",
    "https://blog.example/i.png",
    "http://93.184.216.34/i.png",
]

MIRROR_REFUSED = [
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/x",
    "http://127.0.0.1/x",
    "http://[fe80::1]/x",
    "file:///x",
    "data:image/png;base64,AAAA",
    "https://user:pw@evil.example@good.example/",
]


@pytest.mark.parametrize("url", MIRROR_OK)
def test_mirror_policy_accepts(url):
    assert is_safe_mirror_source(url) is True


@pytest.mark.parametrize("url", MIRROR_REFUSED)
def test_mirror_policy_refuses(url):
    assert is_safe_mirror_source(url) is False


def test_origin_of_rebrackets_ipv6():
    # Reassembling a bare ``::1`` yields http://::1:3000, which nothing
    # can parse back.
    assert origin_of("http://[::1]:3000/blob") == "http://[::1]:3000"
    assert origin_of("https://Blossom.Band/x") == "https://blossom.band"
    assert origin_of("https://blossom.band:8443/x") == "https://blossom.band:8443"
    assert origin_of("file:///etc/passwd") is None
    assert origin_of("not a url") is None


def test_host_of():
    assert host_of("https://Blossom.Band:443/x") == "blossom.band"
    assert host_of("http://[::1]:3000/x") == "::1"
    assert host_of("mailto:someone@example.com") is None


def test_same_origin_is_case_insensitive_on_host_only():
    assert same_origin("https://A.example/x", "https://a.EXAMPLE/y") is True
    assert same_origin("https://a.example/x", "http://a.example/x") is False
    assert same_origin("https://a.example:1/x", "https://a.example:2/x") is False
    assert same_origin("nonsense", "nonsense") is False


def test_invalid_port_is_not_a_crash():
    assert is_safe_media_url("https://a.example:notaport/x") is False
    assert origin_of("https://a.example:notaport/x") is None
