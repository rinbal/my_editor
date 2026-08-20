# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two ``server`` tags, pinned side by side. See ADR AD-11.

Two different specs define a tag called ``server``, and they require
OPPOSITE values. Both live in this one file, adjacent, so nobody can
read one as a bug in the other and "harmonize" them.

BUD-11, kind 24242 authorization token (``specs/bud-11.md:25``):

    The value MUST be a lowercase domain name only (e.g.,
    ``cdn.example.com``), not a full URL.

BUD-03, kind 10063 user server list (``specs/bud-03.md:9``):

    The event MUST include at least one ``server`` tag containing the
    full server URL including the ``http://`` or ``https://``.

Same tag name, opposite requirements, different contexts. Making them
agree is a defect, not a cleanup. One helper decides each value:
``auth.auth_server_domain`` for the token, and the kind 10063 builder in
the server-list module for the list.
"""

from __future__ import annotations

from nostr.blossom import auth
from nostr.blossom.server_list import build_server_list_event, parse_server_list


PUBKEY = "ab" * 32

SERVER_URL = "https://cdn.example.com"
SERVER_DOMAIN = "cdn.example.com"


def _tag_values(event: dict, name: str):
    return [t[1] for t in event["tags"] if t[0] == name]


# --------------------------------------------------------------------------- #
# BUD-11: bare domain, never a URL
# --------------------------------------------------------------------------- #

def test_bud11_auth_server_tag_is_a_bare_lowercase_domain():
    event = auth.build_blossom_auth_event(
        "upload", file_hash="a" * 64, server=SERVER_URL
    )
    values = _tag_values(event, "server")
    assert values == [SERVER_DOMAIN]
    assert "://" not in values[0]
    assert values[0] == values[0].lower()


def test_bud11_auth_server_tag_drops_scheme_port_and_path():
    event = auth.build_blossom_auth_event(
        "upload", file_hash="a" * 64,
        server="HTTPS://CDN.Example.com:8443/upload",
    )
    assert _tag_values(event, "server") == [SERVER_DOMAIN]


def test_bud11_auth_server_tag_accepts_a_bare_domain_unchanged():
    event = auth.build_blossom_auth_event(
        "upload", file_hash="a" * 64, server=SERVER_DOMAIN
    )
    assert _tag_values(event, "server") == [SERVER_DOMAIN]


# --------------------------------------------------------------------------- #
# BUD-03: full URL, scheme intact
# --------------------------------------------------------------------------- #

def test_bud03_server_list_entries_keep_the_full_url():
    # The exact opposite of the BUD-11 tests above, and deliberately so.
    event = build_server_list_event([SERVER_URL], PUBKEY)
    values = _tag_values(event, "server")
    assert values == [SERVER_URL]
    assert values[0].startswith("https://")


def test_bud03_server_list_refuses_the_bud11_form():
    # A bare domain is the correct value for a kind 24242 token and an
    # invalid one for a kind 10063 list, so it is dropped rather than
    # repaired. Coercing it would be the harmonization AD-11 forbids.
    event = build_server_list_event([SERVER_DOMAIN], PUBKEY)
    assert _tag_values(event, "server") == []
    assert parse_server_list({"tags": [["server", SERVER_DOMAIN]]}) == []


def test_the_same_server_yields_a_different_tag_value_in_each_event():
    # One server, two events, two spellings. If a future cleanup ever
    # makes these equal, this is the test that should stop it.
    token = auth.build_blossom_auth_event(
        "upload", file_hash="a" * 64, server=SERVER_URL
    )
    listing = build_server_list_event([SERVER_URL], PUBKEY)
    bud11_value = _tag_values(token, "server")[0]
    bud03_value = _tag_values(listing, "server")[0]

    assert bud11_value == SERVER_DOMAIN
    assert bud03_value == SERVER_URL
    assert bud11_value != bud03_value


def test_the_two_forms_are_deliberately_different():
    """A guard for the reader, not for the code: the BUD-11 value and
    the BUD-03 value describe the same server and are not equal."""
    bud11 = auth.auth_server_domain(SERVER_URL)
    bud03 = SERVER_URL
    assert bud11 != bud03
    assert bud03.endswith(bud11)
    assert "://" not in bud11
    assert "://" in bud03
