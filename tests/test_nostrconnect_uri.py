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


def test_the_link_names_myeditors_own_page_and_leaves_the_icon_out():
    # P1: Amber, Amethyst, Aegis and nsec.app show the name and page where
    # the person approves the connection; nsec.app refuses a link with
    # none of name, url and image (research/signer-pairing.md, section 2).
    # The icon is optional and left out (RG-M2r2b 7): without it the code
    # fits three pixels a module.
    from nostr import CLIENT_URL
    _, _, qs = _split(build_nostrconnect_uri(PK, ["wss://r"], "s"))
    assert qs["url"] == [CLIENT_URL] == ["https://rinbal.github.io/my_editor/"]
    assert "image" not in qs
    assert qs["name"] == ["MyEditor"]
    _, _, qs = _split(build_nostrconnect_uri(PK, ["wss://r"], "s", image="https://i/x.png"))
    assert qs["image"] == ["https://i/x.png"]                  # still there when asked for


def test_the_pairing_code_is_small_enough_to_scan_from_a_screen():
    # RG-M2r2b 7: the link MyEditor shows, at error correction L, is at most
    # QR version 17 (85 modules), so 3 pixels a module and the quiet zone
    # fit in 280.
    import segno
    from nostr.bunker import NIP46_RELAYS
    link = build_nostrconnect_uri("f" * 64, list(NIP46_RELAYS), "f" * 16)
    code = segno.make(link, error="l")
    assert code.version <= 17
    assert (code.symbol_size(border=4)[0]) * 3 <= 280


# The pairing relays' NIP-11 limitations as their documents stated them on
# 2026-11-10 (none asked for payment, none stated a content cap).
# theforest.nostr1.com's said payment_required and listed fees.
# nrs.primal.net is "Primal Nostr Remote Signer Relay", one of Amber's
# defaults (AmberSettings.kt at 621c445a, lines 8 to 13: auth.nostr1.com,
# bucket.coracle.social, nrs.primal.net, relay.nip46.com).
STATED_LIMITATIONS = {
    "wss://relay.primal.net": {"max_message_length": 1_000_000},
    "wss://relay.damus.io": {"max_message_length": 1_000_000},
    "wss://nos.lol": {"max_message_length": 131_072},
    "wss://nrs.primal.net": {"max_message_length": 131_072},
}


def test_the_pairing_relays_are_free_and_uncapped_and_end_with_nsec_apps():
    # Contract 12.5: at least two relays with no small content cap ahead
    # of the capped ones, and relay.nsec.app, where nsec.app answers,
    # whatever the link names. A relay that asks for payment cannot be
    # relied on to carry a channel key's requests.
    from nostr import signer_limits
    from nostr.bunker import NIP46_RELAYS
    assert NIP46_RELAYS == ("wss://relay.primal.net", "wss://relay.damus.io", "wss://nos.lol",
                            "wss://nrs.primal.net", "wss://relay.nsec.app")
    ahead = NIP46_RELAYS[:-1]
    assert len(ahead) >= 2
    for url in ahead:
        cap = signer_limits.content_cap_of({"limitation": STATED_LIMITATIONS[url]})
        assert signer_limits.message_bytes_for_content_cap(cap) == \
            signer_limits.NIP44_MESSAGE_MAX
        assert url not in signer_limits.NAMED_CAPS
    assert "wss://relay.nip46.com" not in NIP46_RELAYS
    assert not any("theforest.nostr1.com" in url for url in NIP46_RELAYS)
