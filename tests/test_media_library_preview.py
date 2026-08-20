# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins what the library says when a blob has no thumbnail.

Two very different situations render as the same grey square: a blob
whose type is not an image (never requested), and an image-typed blob
whose bytes the decoder refuses (downloaded, hash-checked, then found
unreadable). Encrypted uploads from other clients are the common case
of the second, and they are indistinguishable from a broken app unless
the grid explains itself.

The failure signal was not connected at all, so a blob that could not be
previewed looked identical to one still loading, forever.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nostr.blossom.store import MediaFile
from nostr.ui.media_library_dialog import _no_preview_reason, _tooltip_for


def _media(mime="image/png", **kw):
    return MediaFile(
        hash="a" * 64,
        url="https://cdn.example/" + "a" * 64,
        mime_type=mime,
        size=1234,
        **kw,
    )


# --------------------------------------------------------------------- #
# Why there is no preview                                                #
# --------------------------------------------------------------------- #

def test_non_image_type_says_it_is_not_an_image():
    reason = _no_preview_reason(_media(mime="application/octet-stream"))
    assert "not an image" in reason
    assert "application/octet-stream" in reason


def test_unknown_type_still_reads_as_a_sentence():
    reason = _no_preview_reason(_media(mime=""))
    assert "unknown type" in reason


def test_video_says_it_is_not_an_image():
    assert "not an image" in _no_preview_reason(_media(mime="video/mp4"))


def test_image_type_with_unreadable_bytes_is_distinguished():
    # This is the encrypted-blob case: the download worked and the hash
    # matched, so saying "not an image" alone would read as a lie.
    reason = _no_preview_reason(_media(), "not an image")
    assert "downloaded" in reason
    assert "not a readable image" in reason


def test_other_failures_are_passed_through():
    assert _no_preview_reason(_media(), "blob exceeds cache limit") == (
        "blob exceeds cache limit"
    )


def test_a_pending_image_claims_nothing():
    # Still loading is not the same as failed, and must stay silent.
    assert _no_preview_reason(_media()) == ""


# --------------------------------------------------------------------- #
# The tooltip carries it                                                 #
# --------------------------------------------------------------------- #

def test_tooltip_explains_a_non_image_blob():
    tip = _tooltip_for(_media(mime="application/octet-stream"))
    assert "preview:" in tip
    assert "not an image" in tip


def test_tooltip_explains_an_unreadable_image():
    tip = _tooltip_for(_media(), preview_error="not an image")
    assert "preview:" in tip
    assert "not a readable image" in tip


def test_tooltip_of_a_good_image_makes_no_excuse():
    tip = _tooltip_for(_media(width=10, height=20))
    assert "preview:" not in tip


def test_tooltip_still_reports_the_basics():
    tip = _tooltip_for(_media(mime="application/octet-stream"))
    assert "a" * 64 in tip
    assert "application/octet-stream" in tip
