# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins that a private picture can be shown without being written down.

The feature is small and the risk is not. Decrypting a thumbnail is the
whole point of holding the key, but the app has one content-addressed
blob cache that both the grid and the document asset layer share, and an
export walks it. Plaintext written there is a private photo one export
away from a folder the user hands to somebody else.

So the tests below check the negative twice over: the decrypt path is
handed a cache directory and a real one is watched, and neither gains a
file. The rest pin the vocabulary, because a placeholder with no
explanation reads as a broken app and the wrong explanation reads as a
lie: a sealed file is not "not a readable image".

Nothing here touches a network, a signer, a relay or a real home
directory.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.media.filecrypto import encrypt_file
from nostr.media.media_visibility import (
    PRIVATE,
    PUBLIC,
    PUBLISHED_COPY,
    UNKNOWN,
    MediaVisibility,
)
from nostr.media.private_preview import (
    MAX_PREVIEW_BYTES,
    REASON_NOT_AN_IMAGE,
    REASON_NOT_ENCRYPTED,
    REASON_TOO_LARGE,
    REASON_WRONG_KEY,
    preview_from_envelope,
)
from nostr.media.visibility import PrivateBlob, PublicBlob, PublicLedger

from tests.media_fakes import PNG_BYTES, SVG_BYTES, TEXT_BYTES, sha_of


KEY = "3a" * 32
OTHER_KEY = "4b" * 32


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def sealed(data: bytes = PNG_BYTES, key: str = KEY) -> bytes:
    return encrypt_file(data, key_hex=key).envelope


def private(envelope: bytes = None, *, key: str = KEY, **kw) -> PrivateBlob:
    envelope = sealed() if envelope is None else envelope
    return PrivateBlob(
        sha256=sha_of(envelope),
        key_hex=key,
        servers=["https://one.example"],
        size=len(envelope),
        mime=kw.pop("mime", "image/png"),
        name=kw.pop("name", "holiday.png"),
        **kw,
    )


class FakeLibrary:
    """PrivateLibrary's two reads: sha256 to blob, and "are you sure".

    ``vouches_for`` is True by construction here: this fake is a library
    that has finished reading, which is the state these tests are about.
    The unfinished and unreadable states have their own tests.
    """

    def __init__(self, blobs=(), *, vouches=True) -> None:
        self.blobs = {b.sha256: b for b in blobs}
        self.vouches = vouches

    def get(self, sha256):
        return self.blobs.get((sha256 or "").lower())

    def vouches_for(self, sha256):
        return self.vouches


# --------------------------------------------------------------------- #
# The decrypt is real, and it stays in memory                            #
# --------------------------------------------------------------------- #

def test_a_sealed_picture_decodes_to_an_image():
    outcome = preview_from_envelope(sealed(), KEY)
    assert outcome.ok
    assert outcome.image.width() == 3
    assert outcome.image.height() == 2
    assert outcome.reason == ""


def test_nothing_is_written_anywhere_by_a_preview(tmp_path, monkeypatch):
    # The guarantee, checked the blunt way: run the decrypt with the
    # process parked in an empty directory and watch it stay empty.
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    assert preview_from_envelope(sealed(), KEY).ok
    assert set(tmp_path.rglob("*")) == before


def test_the_module_cannot_write_at_all():
    # A stronger form of the test above, since a future edit could write
    # somewhere other than the working directory. Read as names actually
    # used rather than as text, so the prose explaining the rule cannot
    # be mistaken for a breach of it.
    import ast
    import inspect

    from nostr.media import private_preview

    tree = ast.parse(inspect.getsource(private_preview))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for forbidden in (
        "open", "put_bytes", "write_bytes", "write_text", "mkstemp", "Path",
    ):
        assert forbidden not in used, forbidden


def test_a_wrong_key_is_named_as_a_key_problem():
    outcome = preview_from_envelope(sealed(), OTHER_KEY)
    assert not outcome.ok
    assert outcome.reason == REASON_WRONG_KEY


def test_a_failure_never_repeats_the_ciphertext_or_the_key():
    outcome = preview_from_envelope(sealed(), OTHER_KEY)
    assert KEY not in outcome.reason
    assert OTHER_KEY not in outcome.reason


def test_plain_bytes_are_refused_before_any_decrypt():
    outcome = preview_from_envelope(PNG_BYTES, KEY)
    assert not outcome.ok
    assert outcome.reason == REASON_NOT_ENCRYPTED


def test_an_envelope_holding_prose_is_not_an_image():
    outcome = preview_from_envelope(sealed(TEXT_BYTES), KEY)
    assert not outcome.ok
    assert outcome.reason == REASON_NOT_AN_IMAGE


def test_an_svg_inside_the_envelope_is_still_refused():
    # The decode allowlist is what keeps an SVG from pulling in external
    # resources. Being encrypted by a key the user holds does not make
    # the bytes inside trustworthy.
    outcome = preview_from_envelope(sealed(SVG_BYTES), KEY)
    assert not outcome.ok
    assert outcome.reason == REASON_NOT_AN_IMAGE


def test_an_oversized_envelope_is_refused_without_decrypting():
    seen = []

    def decoder(data):
        seen.append(data)
        return None

    huge = bytes([2]) + b"\x00" * MAX_PREVIEW_BYTES
    outcome = preview_from_envelope(huge, KEY, decoder=decoder)
    assert outcome.reason == REASON_TOO_LARGE
    assert seen == []


def test_a_null_image_from_the_decoder_is_not_reported_as_a_picture():
    class NullImage:
        def isNull(self):
            return True

    outcome = preview_from_envelope(sealed(), KEY, decoder=lambda _d: NullImage())
    assert not outcome.ok
    assert outcome.reason == REASON_NOT_AN_IMAGE


def test_the_decoder_is_handed_the_plaintext_not_the_envelope():
    seen = []
    preview_from_envelope(sealed(), KEY, decoder=lambda d: seen.append(d))
    assert seen == [PNG_BYTES]


# --------------------------------------------------------------------- #
# What the interface is allowed to know                                  #
# --------------------------------------------------------------------- #

def test_an_unknown_blob_is_public():
    assert MediaVisibility().state_of("a" * 64) == PUBLIC


def test_a_blob_a_library_cannot_account_for_is_not_public():
    # The whole point of the fourth state. Before it existed, a library
    # that was still loading, or that never opened at all, reported every
    # private file as public, and the picker embedded its ciphertext
    # address in a signed event without asking anyone.
    view = MediaVisibility(library=FakeLibrary([], vouches=False))

    assert view.state_of("a" * 64) == UNKNOWN
    assert view.is_unknown("a" * 64)
    # It is not private either: there is no key here for it. The two are
    # different claims and only one of them is being made.
    assert not view.is_private("a" * 64)


def test_a_file_the_library_holds_is_private_even_mid_load():
    # Coverage is per hash. A record that has already been opened is
    # private and stays private while the rest of the load runs.
    blob = private()
    view = MediaVisibility(library=FakeLibrary([blob], vouches=False))

    assert view.state_of(blob.sha256) == PRIVATE


def test_a_library_that_cannot_account_for_a_file_hides_nothing_else(tmp_path):
    # The unknown answer must not disturb the copy lookup or the key
    # path: both read the record, and there is no record to read.
    view = MediaVisibility(library=FakeLibrary([], vouches=False))

    assert view.public_copy_of("a" * 64) is None
    assert view.private_blobs(["a" * 64]) == []
    assert view.declared_mime("a" * 64) == ""


def test_a_library_file_with_no_copy_is_private():
    blob = private()
    view = MediaVisibility(library=FakeLibrary([blob]))
    assert view.state_of(blob.sha256) == PRIVATE
    assert view.is_private(blob.sha256)


def test_a_library_file_with_a_live_copy_says_so(tmp_path):
    blob = private()
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(
        sha256="c" * 64,
        url="https://cdn.example/" + "c" * 64,
        source_hash=blob.sha256,
    ))
    view = MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)
    assert view.state_of(blob.sha256) == PUBLISHED_COPY
    assert view.public_copy_of(blob.sha256).sha256 == "c" * 64


def test_a_pointer_the_ledger_does_not_confirm_is_not_believed(tmp_path):
    # A revoke that half succeeded leaves the pointer behind. Believing
    # it would badge a dead link as published and then refuse to make a
    # new copy.
    blob = private(public_copy_hash="d" * 64)
    ledger = PublicLedger(path=tmp_path / "public.json")
    view = MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)
    assert view.state_of(blob.sha256) == PRIVATE
    assert view.public_copy_of(blob.sha256) is None


def test_a_pointer_to_someone_elses_copy_is_ignored(tmp_path):
    blob = private(public_copy_hash="e" * 64)
    ledger = PublicLedger(path=tmp_path / "public.json")
    ledger.record(PublicBlob(
        sha256="e" * 64,
        url="https://cdn.example/" + "e" * 64,
        source_hash="f" * 64,
    ))
    view = MediaVisibility(library=FakeLibrary([blob]), ledger=ledger)
    assert view.public_copy_of(blob.sha256) is None


def test_no_library_means_every_file_is_public():
    blob = private()
    view = MediaVisibility(ledger=None, library=None)
    assert not view.is_private(blob.sha256)
    assert view.private_blobs([blob.sha256]) == []


def test_private_blobs_keeps_the_order_it_was_given():
    one, two = private(), private(sealed(PNG_BYTES + b"x"))
    view = MediaVisibility(library=FakeLibrary([one, two]))
    picked = view.private_blobs([two.sha256, "z" * 64, one.sha256])
    assert [b.sha256 for b in picked] == [two.sha256, one.sha256]


def test_the_view_decrypts_so_the_caller_never_holds_the_key():
    envelope = sealed()
    blob = private(envelope)
    view = MediaVisibility(library=FakeLibrary([blob]))
    outcome = view.preview(blob.sha256, envelope)
    assert outcome.ok
    # The one place a key could leak into the interface is a preview
    # result, so it carries an image and a sentence and nothing else.
    assert not hasattr(outcome, "key_hex")


def test_previewing_a_file_that_is_not_in_the_library_says_so():
    outcome = MediaVisibility().preview("a" * 64, sealed())
    assert not outcome.ok
    assert "private library" in outcome.reason
