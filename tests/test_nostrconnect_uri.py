# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""build_nostrconnect_uri: pure URI construction tests."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from nostr.bunker import DEFAULT_PERMS, build_nostrconnect_uri


PK = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def _split(uri: str) -> tuple[str, str, dict[str, list[str]]]:
    parts = urlsplit(uri)
    return parts.scheme, (parts.netloc or parts.path.lstrip("/")), parse_qs(parts.query, keep_blank_values=False)


def test_basic_uri_shape():
    uri = build_nostrconnect_uri(PK, ["wss://relay.example"], "mysecret")
    scheme, host, qs = _split(uri)
    assert scheme == "nostrconnect"
    assert host == PK
    assert qs["relay"] == ["wss://relay.example"]
    assert qs["secret"] == ["mysecret"]
    assert qs["perms"] == [DEFAULT_PERMS]
    assert qs["name"] == ["MyEditor"]


def test_the_qr_asks_for_sealing_messages():
    # A seal (NIP-59, kind 13) is signed by the sender's own key; a signer
    # that enforces the pairing's list would refuse it otherwise.
    _, _, qs = _split(build_nostrconnect_uri(PK, ["wss://relay.example"], "s"))
    assert "sign_event:13" in qs["perms"][0].split(",")


def test_multiple_relays_preserve_order():
    uri = build_nostrconnect_uri(
        PK, ["wss://r1", "wss://r2", "wss://r3"], "s",
    )
    _, _, qs = _split(uri)
    assert qs["relay"] == ["wss://r1", "wss://r2", "wss://r3"]


def test_relay_urls_are_url_encoded():
    """A relay URL with a query string would otherwise corrupt the parent
    URI's query parsing; make sure we encode the embedded '?', '&', '='."""
    weird = "wss://relay.example/?foo=bar&baz=qux"
    uri = build_nostrconnect_uri(PK, [weird], "s")
    _, _, qs = _split(uri)
    assert qs["relay"] == [weird]


def test_perms_default_preserved_commas():
    uri = build_nostrconnect_uri(PK, ["wss://r"], "s")
    _, _, qs = _split(uri)
    # Commas (and the colon in sign_event:1) must round-trip: they're
    # part of the perms grammar.
    assert "sign_event:1" in qs["perms"][0]
    assert "sign_event:30023" in qs["perms"][0]


def test_optional_url_and_image_appended():
    uri = build_nostrconnect_uri(
        PK,
        ["wss://r"],
        "s",
        url="https://example.com",
        image="https://example.com/icon.png",
    )
    _, _, qs = _split(uri)
    assert qs["url"] == ["https://example.com"]
    assert qs["image"] == ["https://example.com/icon.png"]


def test_pubkey_lowercased():
    upper = "A" * 64
    uri = build_nostrconnect_uri(upper, ["wss://r"], "s")
    _, host, _ = _split(uri)
    assert host == "a" * 64


@pytest.mark.parametrize("bad_pk", [
    "short",
    "g" * 64,            # non-hex
    "",
])
def test_rejects_bad_pubkey(bad_pk):
    with pytest.raises(ValueError):
        build_nostrconnect_uri(bad_pk, ["wss://r"], "s")


def test_rejects_no_relays():
    with pytest.raises(ValueError):
        build_nostrconnect_uri(PK, [], "s")


def test_rejects_empty_secret():
    """The spec requires the secret to prevent connection spoofing;
    silently issuing a URI without one would be dangerous."""
    with pytest.raises(ValueError):
        build_nostrconnect_uri(PK, ["wss://r"], "")


def test_custom_name_overrides_default():
    uri = build_nostrconnect_uri(PK, ["wss://r"], "s", name="Custom App")
    _, _, qs = _split(uri)
    assert qs["name"] == ["Custom App"]


def test_the_link_names_myeditors_own_page_and_icon_by_default():
    # P1: Amber, Amethyst, Aegis and nsec.app show them where the person
    # approves the connection; nsec.app refuses a link with none of name,
    # url and image (research/signer-pairing.md, section 2).
    from nostr import CLIENT_IMAGE, CLIENT_URL
    _, _, qs = _split(build_nostrconnect_uri(PK, ["wss://r"], "s"))
    assert qs["url"] == [CLIENT_URL] == ["https://rinbal.github.io/my_editor/"]
    assert qs["image"] == [CLIENT_IMAGE] == [
        "https://rinbal.github.io/my_editor/install/icon.png"]
    assert qs["name"] == ["MyEditor"]


def test_the_pairing_relays_end_with_the_one_nsec_app_answers_on():
    # Contract 12.5 (feat/redaktion 4076b9b): relays with no small content
    # cap first, and relay.nsec.app, where nsec.app answers, whatever the
    # link names.
    from nostr.bunker import NIP46_RELAYS
    assert NIP46_RELAYS[-1] == "wss://relay.nsec.app"
    assert NIP46_RELAYS[:2] == ("wss://relay.primal.net", "wss://relay.damus.io")
    assert "wss://relay.nip46.com" not in NIP46_RELAYS
