# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nostr.rss.nostr_resolver`` — the pure parser layer.

The async ``LongFormFetcher`` is exercised indirectly via the importer
integration; testing it in isolation needs a Qt event loop and a relay
pool double, which is excessive for what is a thin wrapper around
``fetch_latest_event``.
"""

from __future__ import annotations

import unittest

from nostr.bech32 import encode_naddr
from nostr.rss.nostr_resolver import (
    LongFormCoord,
    extract_nostr_coord,
    is_nostr_uri_scheme,
)


# A reusable long-form naddr with explicit relay hints baked in.
_LONGFORM_NADDR = encode_naddr(
    "my-post", "a" * 64, 30023,
    ["wss://relay.example.com", "wss://nos.lol"],
)

# Same coordinate shape, but a non-long-form kind. Should be rejected by
# extract_nostr_coord because we only mirror NIP-23 events.
_WRONG_KIND_NADDR = encode_naddr("note-x", "b" * 64, 31000, [])

# Long-form with no relay hints in the TLV. Useful for asserting we
# don't crash on the empty case.
_NO_RELAYS_NADDR = encode_naddr("solo", "c" * 64, 30023, [])


class ExtractNostrCoordTests(unittest.TestCase):
    def test_nostr_uri_scheme(self) -> None:
        coord = extract_nostr_coord(f"nostr:{_LONGFORM_NADDR}")
        self.assertIsInstance(coord, LongFormCoord)
        self.assertEqual(coord.kind, 30023)
        self.assertEqual(coord.d_tag, "my-post")
        self.assertEqual(coord.pubkey_hex, "a" * 64)

    def test_relay_hints_preserved(self) -> None:
        coord = extract_nostr_coord(f"nostr:{_LONGFORM_NADDR}")
        self.assertEqual(
            coord.relay_hints,
            ("wss://relay.example.com", "wss://nos.lol"),
        )

    def test_https_njump_url(self) -> None:
        coord = extract_nostr_coord(f"https://njump.me/{_LONGFORM_NADDR}")
        self.assertIsNotNone(coord)
        self.assertEqual(coord.d_tag, "my-post")

    def test_https_habla_url(self) -> None:
        coord = extract_nostr_coord(f"https://habla.news/a/{_LONGFORM_NADDR}")
        self.assertIsNotNone(coord)
        self.assertEqual(coord.kind, 30023)

    def test_https_yakihonne_url(self) -> None:
        coord = extract_nostr_coord(
            f"https://yakihonne.com/article/{_LONGFORM_NADDR}"
        )
        self.assertIsNotNone(coord)

    def test_naddr_in_path_with_trailing_query(self) -> None:
        coord = extract_nostr_coord(
            f"https://example.com/post/{_LONGFORM_NADDR}?ref=rss"
        )
        self.assertIsNotNone(coord)

    def test_bare_bech32_string(self) -> None:
        coord = extract_nostr_coord(_LONGFORM_NADDR)
        self.assertIsNotNone(coord)
        self.assertEqual(coord.kind, 30023)

    def test_long_form_with_no_relay_hints(self) -> None:
        coord = extract_nostr_coord(f"nostr:{_NO_RELAYS_NADDR}")
        self.assertIsNotNone(coord)
        self.assertEqual(coord.relay_hints, ())

    def test_pubkey_normalised_to_lowercase(self) -> None:
        # Synthesise an uppercase variant via re-encode of the same hex.
        upper_naddr = encode_naddr("z", "AB" + "00" * 31, 30023, [])
        coord = extract_nostr_coord(f"nostr:{upper_naddr}")
        self.assertEqual(coord.pubkey_hex, ("AB" + "00" * 31).lower())

    def test_rejects_non_longform_kind(self) -> None:
        self.assertIsNone(extract_nostr_coord(f"nostr:{_WRONG_KIND_NADDR}"))

    def test_rejects_url_without_naddr(self) -> None:
        self.assertIsNone(
            extract_nostr_coord("https://example.com/post/hello-world")
        )

    def test_rejects_empty_string(self) -> None:
        self.assertIsNone(extract_nostr_coord(""))

    def test_rejects_none(self) -> None:
        self.assertIsNone(extract_nostr_coord(None))

    def test_rejects_malformed_bech32(self) -> None:
        self.assertIsNone(extract_nostr_coord("nostr:naddr1notavalidbech32"))

    def test_rejects_garbage(self) -> None:
        self.assertIsNone(extract_nostr_coord("https://example.com/login"))

    def test_picks_full_naddr_when_url_contains_truncated_slug(self) -> None:
        # Pareto / self-hosted publishers often embed a humanised slug
        # that truncates the naddr (e.g. ``/post-naddr1qqr.../``) and
        # follow it with the full canonical address. The regex matches
        # the short prefix first; the resolver must keep trying matches
        # until one decodes.
        url = (
            f"https://example.com/s/alice/post-{_LONGFORM_NADDR[:12]}/"
            f"{_LONGFORM_NADDR}"
        )
        coord = extract_nostr_coord(url)
        self.assertIsNotNone(coord)
        self.assertEqual(coord.d_tag, "my-post")

    def test_returns_first_decodable_when_multiple_naddrs_present(self) -> None:
        # If a URL crams two full naddrs, return the first that decodes
        # — document-order is the canonical pick.
        other = encode_naddr("other-post", "d" * 64, 30023, [])
        url = f"https://example.com/{_LONGFORM_NADDR}?then={other}"
        coord = extract_nostr_coord(url)
        self.assertIsNotNone(coord)
        self.assertEqual(coord.d_tag, "my-post")


class IsNostrUriSchemeTests(unittest.TestCase):
    def test_recognises_nostr_uri(self) -> None:
        self.assertTrue(is_nostr_uri_scheme("nostr:naddr1abc"))

    def test_case_insensitive_scheme(self) -> None:
        self.assertTrue(is_nostr_uri_scheme("NOSTR:naddr1abc"))

    def test_ignores_leading_whitespace(self) -> None:
        self.assertTrue(is_nostr_uri_scheme("   nostr:naddr1abc"))

    def test_http_url_with_naddr_is_not_nostr_uri(self) -> None:
        self.assertFalse(
            is_nostr_uri_scheme("https://njump.me/naddr1abc")
        )

    def test_empty_input(self) -> None:
        self.assertFalse(is_nostr_uri_scheme(""))
        self.assertFalse(is_nostr_uri_scheme(None))


if __name__ == "__main__":
    unittest.main()
