# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The BUD-03 hash-from-URL rule, pinned against the spec's own examples.

specs/bud-03.md line 40: "When extracting the SHA256 hash from the URL
clients MUST use the last occurrence of a 64 char hex string." Lines
45 to 51 list six URL shapes that must all yield the same hash. They are
reproduced here verbatim, because "last occurrence" is the whole rule
and any local improvement on it desynchronises this client from every
other one.

Pure and offline: no network, no relay, no signer, no Qt.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.blossom.hashes import (
    blob_url,
    hash_from_url,
    hashes_in_url,
    url_agrees_with_hash,
)


# The hash BUD-03 says every example below must select.
WANTED = "b1674191a88ec5cdd733e4240a81803105dc412d6c6708d53ab94fc248f4f553"

# The 64 character PUBKEY that shares a path with the hash in one of the
# spec's own examples. It is what makes "last occurrence" load-bearing.
PUBKEY = "ec4425ff5e9446080d2f70440188e3ca5d6da8713db7bdeef73d0ed54d9093f0"

OTHER = "a" * 64


# --------------------------------------------------------------------------- #
# The six shapes from specs/bud-03.md                                         #
# --------------------------------------------------------------------------- #

BUD03_URLS = [
    f"https://blossom.example.com/{WANTED}.pdf",
    f"https://cdn.example.com/{WANTED}",
    f"https://cdn.example.com/user/{PUBKEY}/media/{WANTED}.pdf",
    f"https://cdn.example.com/media/user-name/documents/{WANTED}.pdf",
    f"http://download.example.com/downloads/{WANTED}",
    f"http://media.example.com/documents/b1/67/{WANTED}.pdf",
]


@pytest.mark.parametrize("url", BUD03_URLS)
def test_every_bud03_example_selects_the_documented_hash(url):
    assert hash_from_url(url) == WANTED


def test_a_pubkey_earlier_in_the_path_never_wins():
    # The whole point of "last occurrence": this URL carries two 64 char
    # hex runs and the first one is the author's pubkey, not a blob.
    url = f"https://cdn.example.com/user/{PUBKEY}/media/{WANTED}.pdf"
    assert hashes_in_url(url) == [PUBKEY, WANTED]
    assert hash_from_url(url) != PUBKEY
    assert hash_from_url(url) == WANTED


@pytest.mark.parametrize("suffix", ["", ".pdf", ".png"])
def test_a_file_extension_does_not_change_the_hash(suffix):
    assert hash_from_url(f"https://cdn.example.com/{WANTED}{suffix}") == WANTED


# --------------------------------------------------------------------------- #
# Absence, case, and run boundaries                                           #
# --------------------------------------------------------------------------- #

def test_a_url_with_no_hex_run_has_no_hash():
    # None means "this URL names no hash". No caller may read it as an
    # instruction to compute one.
    assert hash_from_url("https://example.com/logo.png") is None
    assert hashes_in_url("https://example.com/logo.png") == []


def test_uppercase_hex_normalizes_to_lowercase():
    assert hash_from_url(f"https://cdn.example.com/{WANTED.upper()}") == WANTED


def test_non_strings_and_blanks_are_not_hashes():
    assert hash_from_url("") is None
    assert hash_from_url(None) is None
    assert hashes_in_url(None) == []


def test_a_63_character_run_is_not_a_hash():
    assert hash_from_url("https://cdn.example.com/" + "b" * 63) is None


def test_a_65_character_run_yields_its_first_64_characters():
    # Pins the non-overlapping scan: the rule counts runs of 64, it does
    # not look for a run bounded by path separators.
    assert hash_from_url("https://cdn.example.com/" + "b" * 65) == "b" * 64


# --------------------------------------------------------------------------- #
# blob_url                                                                    #
# --------------------------------------------------------------------------- #

def test_blob_url_tolerates_a_trailing_slash_on_the_origin():
    assert blob_url("https://cdn.example.com/", WANTED) == (
        f"https://cdn.example.com/{WANTED}")
    assert blob_url("https://cdn.example.com", WANTED) == (
        f"https://cdn.example.com/{WANTED}")


def test_blob_url_lowercases_the_hash():
    assert blob_url("https://cdn.example.com", WANTED.upper()).endswith(WANTED)


# --------------------------------------------------------------------------- #
# url_agrees_with_hash: the verification predicate                            #
# --------------------------------------------------------------------------- #

def test_a_tokenised_url_still_agrees_with_the_blob_it_serves():
    # Both directions pinned. Under the literal extraction rule the token
    # wins, which is correct and is exactly why verification strips the
    # query first instead of reusing the extraction rule.
    url = f"https://cdn.example/{WANTED}.png?token={OTHER}"
    assert hash_from_url(url) == OTHER
    assert url_agrees_with_hash(url, WANTED) is True


def test_a_fragment_is_stripped_before_verification():
    assert url_agrees_with_hash(f"https://cdn.example/{WANTED}.png#{OTHER}",
                                WANTED) is True


def test_a_path_whose_last_run_is_a_different_blob_does_not_agree():
    # The rejected relaxation ("our hash appears anywhere") would accept
    # this, and every other client would resolve the stored URL to the
    # other blob.
    assert url_agrees_with_hash(f"https://cdn.ours/{WANTED}/x/{OTHER}",
                                WANTED) is False


def test_a_url_with_no_hex_run_agrees_with_anything():
    assert url_agrees_with_hash("https://cdn.example/blob/42", WANTED) is True


def test_verification_needs_a_hash_to_verify_against():
    assert url_agrees_with_hash(f"https://cdn.example/{WANTED}", "") is False
    assert url_agrees_with_hash("", WANTED) is False


def test_verification_is_case_insensitive_on_both_sides():
    assert url_agrees_with_hash(f"https://cdn.example/{WANTED.upper()}",
                                WANTED) is True
    assert url_agrees_with_hash(f"https://cdn.example/{WANTED}",
                                WANTED.upper()) is True
