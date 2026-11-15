# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where STANDUP requests may go (nostr/standup/servers.py): an origin
(8.1, feat/redaktion 0820f0d) over https, or plain http to exactly
127.0.0.1 with the developer setting, and no address built in (the
association names its instance)."""

from __future__ import annotations

import pytest

from nostr.standup import servers


@pytest.mark.parametrize("value, local, expected", [
    ("https://standup.example", False, "https://standup.example"),
    # RG-M1 10: a path, a trailing slash or capitals are not an origin.
    ("https://standup.example/", False, ""),
    ("https://standup.example/base", False, ""),
    ("https://STANDUP.example", False, ""),
    ("https://standup.example:8443", False, "https://standup.example:8443"),
    ("http://standup.example", True, ""),
    ("http://127.0.0.1:5000", False, ""),
    ("http://127.0.0.1:5000", True, "http://127.0.0.1:5000"),
    ("http://localhost:5000", True, ""),
    ("http://127.0.0.1.evil.example", True, ""),
    ("http://127.0.0.1@evil.example", True, ""),
    ("https://user@standup.example", False, ""),
    ("https://standup.example/?x=1", False, ""),
    ("https://standup.example/#x", False, ""),
    ("ftp://standup.example", False, ""),
    (None, False, ""),
])
def test_only_https_or_this_computer_with_the_developer_setting(value, local, expected):
    assert servers.usable_server_url(value, local_http=local) == expected


def test_the_developer_setting_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv(servers.ENV_LOCAL_HTTP, raising=False)
    assert servers.usable_server_url("http://127.0.0.1:5000") == ""
    monkeypatch.setenv(servers.ENV_LOCAL_HTTP, "1")
    assert servers.usable_server_url("http://127.0.0.1:5000") == "http://127.0.0.1:5000"


def test_no_server_address_is_built_in(monkeypatch):
    monkeypatch.delenv(servers.ENV_SERVER, raising=False)
    assert servers.configured_server() == ""
    monkeypatch.setenv(servers.ENV_SERVER, "https://standup.local.example")
    assert servers.configured_server() == "https://standup.local.example"
    # Typed by a developer: the slash at the end is forgiven, a path is not.
    monkeypatch.setenv(servers.ENV_SERVER, " https://standup.local.example/ ")
    assert servers.configured_server() == "https://standup.local.example"
    monkeypatch.setenv(servers.ENV_SERVER, "https://standup.local.example/api")
    assert servers.configured_server() == ""
