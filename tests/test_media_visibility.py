# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the private/public split and the ledger that decides it.

The defects this guards against are all quiet ones. A private key
reaching a published record leaks a file with no error anywhere. A
pointer trusted over the ledger badges a dead link as published and
then refuses to republish it. A ledger write that fails without being
noticed leaves a blob public and unlisted, which the user cannot revoke
because they cannot see it.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.media.visibility import (
    CURRENT_LEDGER_VERSION,
    PrivateBlob,
    PublicBlob,
    PublicLedger,
    needs_public_copy,
    public_blob_from_record,
    public_blob_to_record,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
KEY = "1e" * 32


def private(sha=A, **kw):
    return PrivateBlob(sha256=sha, key_hex=kw.pop("key_hex", KEY), **kw)


def public(sha=B, **kw):
    return PublicBlob(sha256=sha, url=kw.pop("url", f"https://cdn.example/{sha}"), **kw)


def ledger(tmp_path, name="media_public.json"):
    return PublicLedger(path=tmp_path / name)


# --------------------------------------------------------------------- #
# The types themselves are the guarantee                                #
# --------------------------------------------------------------------- #

def test_a_public_blob_has_nowhere_to_put_a_key():
    # The whole point: this type is what gets published, so a key cannot
    # be leaked through a field that does not exist.
    assert not hasattr(public(), "key_hex")
    assert "key" not in public_blob_to_record(public())
    assert not any("key" in k for k in public_blob_to_record(public()))


def test_a_private_blob_cannot_exist_without_its_key():
    with pytest.raises(ValueError, match="unopenable"):
        PrivateBlob(sha256=A, key_hex="")


def test_both_types_require_a_real_hash():
    with pytest.raises(ValueError):
        PrivateBlob(sha256="nope", key_hex=KEY)
    with pytest.raises(ValueError):
        PublicBlob(sha256="nope", url="https://x/y")


def test_a_public_blob_requires_a_url():
    with pytest.raises(ValueError):
        PublicBlob(sha256=B, url="")


def test_neither_type_can_be_mutated_after_construction():
    with pytest.raises(Exception):
        private().sha256 = C
    with pytest.raises(Exception):
        public().url = "https://elsewhere.example/x"


# --------------------------------------------------------------------- #
# The ledger is the authority                                           #
# --------------------------------------------------------------------- #

def test_a_recorded_blob_is_public(tmp_path):
    led = ledger(tmp_path)
    assert not led.is_public(B)
    assert led.record(public())
    assert led.is_public(B)


def test_a_copy_is_found_by_its_source(tmp_path):
    led = ledger(tmp_path)
    led.record(public(source_hash=A))
    found = led.copy_of(A)
    assert found is not None and found.sha256 == B


def test_a_stale_pointer_does_not_claim_a_file_is_published(tmp_path):
    # The revoke removed the copy but left the pointer behind. Trusting
    # the pointer would badge a dead link and block republishing.
    led = ledger(tmp_path)
    stale = PrivateBlob(sha256=A, key_hex=KEY, public_copy_hash=B)
    assert not led.has_public_copy(stale)
    assert needs_public_copy([stale], led) == [stale]


def test_a_pointer_naming_someone_elses_copy_is_not_believed(tmp_path):
    # The blob exists and is public, but it was minted from a different
    # original, so it is not this file's copy.
    led = ledger(tmp_path)
    led.record(public(source_hash=C))
    mislabelled = PrivateBlob(sha256=A, key_hex=KEY, public_copy_hash=B)
    assert not led.has_public_copy(mislabelled)


def test_a_live_copy_is_recognised_without_a_pointer(tmp_path):
    # A pointer lost to a crash must not cost the user a second upload.
    led = ledger(tmp_path)
    led.record(public(source_hash=A))
    assert led.has_public_copy(PrivateBlob(sha256=A, key_hex=KEY))


def test_needs_public_copy_reports_only_what_is_missing(tmp_path):
    led = ledger(tmp_path)
    led.record(public(sha=B, source_hash=A))
    already, missing = private(A), private(C)
    assert needs_public_copy([already, missing], led) == [missing]


def test_forgetting_an_entry_makes_it_need_a_copy_again(tmp_path):
    led = ledger(tmp_path)
    led.record(public(source_hash=A))
    led.forget(B)
    assert not led.has_public_copy(private(A))


# --------------------------------------------------------------------- #
# Persistence                                                           #
# --------------------------------------------------------------------- #

def test_entries_survive_a_restart(tmp_path):
    ledger(tmp_path).record(public(source_hash=A, size=17, mime="image/png"))
    reopened = ledger(tmp_path)
    blob = reopened.get(B)
    assert blob is not None
    assert blob.source_hash == A and blob.size == 17 and blob.mime == "image/png"


def test_the_file_is_versioned_and_private_on_disk(tmp_path):
    led = ledger(tmp_path)
    led.record(public())
    data = json.loads((tmp_path / "media_public.json").read_text())
    assert data["version"] == CURRENT_LEDGER_VERSION
    mode = os.stat(tmp_path / "media_public.json").st_mode & 0o777
    assert mode == 0o600


def test_a_missing_file_is_simply_empty(tmp_path):
    led = ledger(tmp_path)
    assert len(led) == 0 and not led.degraded and not led.read_only


def test_a_corrupt_file_degrades_instead_of_raising(tmp_path):
    (tmp_path / "media_public.json").write_text("{not json")
    led = ledger(tmp_path)
    assert led.degraded and len(led) == 0


def test_one_bad_row_does_not_cost_the_rest_of_the_ledger(tmp_path):
    (tmp_path / "media_public.json").write_text(json.dumps({
        "version": 1,
        "public": [
            {"sha256": B, "url": "https://cdn.example/b"},
            {"sha256": "not-a-hash", "url": "https://cdn.example/x"},
            "a bare string",
            None,
            {"sha256": C, "url": "https://cdn.example/c"},
        ],
    }))
    led = ledger(tmp_path)
    assert led.is_public(B) and led.is_public(C) and len(led) == 2


def test_a_newer_file_is_left_alone(tmp_path):
    # Running an older build must not truncate someone's record of what
    # they have published.
    original = json.dumps({"version": CURRENT_LEDGER_VERSION + 1, "public": [
        {"sha256": B, "url": "https://cdn.example/b"},
    ]})
    path = tmp_path / "media_public.json"
    path.write_text(original)
    led = ledger(tmp_path)
    assert led.read_only and len(led) == 0
    assert not led.record(public(sha=C))
    assert path.read_text() == original


def test_a_degraded_ledger_is_not_overwritten_by_the_first_commit(tmp_path):
    # A ledger that failed to load holds none of what is in the file. The
    # first commit used to write a fresh one containing only the new
    # entry, discarding every public copy the user had ever made, and
    # this list is the only way they can revoke any of it. Refusing costs
    # a publish; writing would cost the list.
    original = json.dumps({"version": 1, "public": [
        {"sha256": B, "url": "https://cdn.example/b", "source_hash": A},
    ]})
    path = tmp_path / "media_public.json"
    path.write_text(original[:-9])   # a truncated write
    led = ledger(tmp_path)
    assert led.degraded and len(led) == 0

    assert led.record(public(sha=C)) is False
    assert path.read_text() == original[:-9]


def test_a_degraded_ledger_does_not_forget_its_way_to_an_empty_file(tmp_path):
    (tmp_path / "media_public.json").write_text("{not json")
    led = ledger(tmp_path)

    assert led.forget(B) is False
    assert (tmp_path / "media_public.json").read_text() == "{not json"


def test_a_write_failure_is_reported_not_swallowed(tmp_path):
    # An unlisted public blob cannot be revoked by a user who cannot see
    # it, so the caller has to learn that the write did not happen.
    led = ledger(tmp_path / "nope" / "deeper")
    (tmp_path / "nope").write_text("this is a file, not a directory")
    assert led.record(public()) is False


def test_a_malformed_record_is_skipped_rather_than_guessed(tmp_path):
    assert public_blob_from_record({"sha256": B}) is None          # no url
    assert public_blob_from_record({"url": "https://x/y"}) is None  # no hash
    assert public_blob_from_record("not a dict") is None
    assert public_blob_from_record(None) is None


def test_a_record_round_trips(tmp_path):
    blob = public(source_hash=A, size=99, mime="image/jpeg",
                  uploaded_at=1755000000, scrubbed=True,
                  servers=["https://one.example", "https://two.example"])
    assert public_blob_from_record(public_blob_to_record(blob)) == blob
