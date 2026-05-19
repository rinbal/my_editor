"""Unit tests for ``nostr.rss.dtag``."""

from __future__ import annotations

import unittest
from hashlib import sha256

from nostr.rss.dtag import NoIdentifierError, derive_identifier


class DeriveIdentifierTests(unittest.TestCase):
    def test_guid_is_preferred_seed(self) -> None:
        ident = derive_identifier(
            guid="urn:item:42",
            link="https://example.com/post",
            title="Hello",
        )
        expected = sha256(b"urn:item:42").hexdigest()[:16]
        self.assertEqual(ident, expected)

    def test_falls_back_to_link_when_guid_missing(self) -> None:
        ident = derive_identifier(link="https://example.com/post", title="Hello")
        expected = sha256(b"https://example.com/post").hexdigest()[:16]
        self.assertEqual(ident, expected)

    def test_falls_back_to_title_when_guid_and_link_missing(self) -> None:
        ident = derive_identifier(title="Hello world")
        expected = sha256(b"Hello world").hexdigest()[:16]
        self.assertEqual(ident, expected)

    def test_empty_strings_are_treated_as_missing(self) -> None:
        ident = derive_identifier(guid="", link="", title="only this")
        expected = sha256(b"only this").hexdigest()[:16]
        self.assertEqual(ident, expected)

    def test_prefix_is_prepended_verbatim(self) -> None:
        ident = derive_identifier(guid="seed", prefix="rss-")
        expected = "rss-" + sha256(b"seed").hexdigest()[:16]
        self.assertEqual(ident, expected)

    def test_identifier_is_stable_across_runs(self) -> None:
        a = derive_identifier(guid="same input")
        b = derive_identifier(guid="same input")
        self.assertEqual(a, b)

    def test_different_seeds_produce_different_ids(self) -> None:
        a = derive_identifier(guid="seed-a")
        b = derive_identifier(guid="seed-b")
        self.assertNotEqual(a, b)

    def test_unicode_seed_is_utf8_hashed(self) -> None:
        ident = derive_identifier(title="café à Paris")
        expected = sha256("café à Paris".encode("utf-8")).hexdigest()[:16]
        self.assertEqual(ident, expected)

    def test_no_inputs_raises(self) -> None:
        with self.assertRaises(NoIdentifierError):
            derive_identifier()

    def test_only_empty_strings_raises(self) -> None:
        with self.assertRaises(NoIdentifierError):
            derive_identifier(guid="", link="", title="")


if __name__ == "__main__":
    unittest.main()
