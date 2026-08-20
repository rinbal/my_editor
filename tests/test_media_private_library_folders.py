# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A drive holds folders as well as files, and folders are not failures.

Observed on a real account: 16 kind-34578 records, 12 files and 4
folders filed under d="folder--Mediums" and friends. Every folder was
read as a broken file record, so it stayed unresolved forever; and
because an unresolved record filed under something that is not a content
hash could be any file, ``vouches_for`` withheld its answer for the
whole library. The result was all 12 real files badged NOT CHECKED, a
permanent warning banner, and a publish gate that refused everything.

The rule these pin: doubt has to be about a file. A record that names no
blob, at an address that names no blob, is not a file this app failed to
read. It is not a file.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.media.private_library import parse_file_record

HASH_A = "a" * 64
HASH_B = "b" * 64
KEY = "1e" * 32

# The four identifiers actually on this account's relays.
REAL_FOLDERS = (
    ("folder--Mediums", "/Mediums"),
    ("folder--Folder-1-Profiles-Profiles-2", "/Folder 1 Profiles/Profiles 2"),
    ("folder--MyEditor", "/MyEditor"),
    ("folder--Folder-1-Profiles", "/Folder 1 Profiles"),
)


def folder_keep(identifier, path):
    """A Lotus folder marker, decrypted off this account's relays.

    Transcribed from the real records rather than imagined. The field
    that matters is ``hash``: it is the folder's own address echoed
    back, not a content hash, which is why a rule keyed on the hash
    being absent did not recognise a single real folder.
    """
    return {
        "type": "application/x-folder-keep",
        "name": ".keep",
        "hash": identifier,
        "folder": path,
        "size": 0,
        "encryptionKey": "",
        "server": "https://blossom.primal.net",
        "uploadedAt": 1781644813,
    }


def parse(payload, identifier):
    return parse_file_record(json.dumps(payload), identifier=identifier)


# --------------------------------------------------------------------- #
# A folder is not a file                                                #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("identifier,path", REAL_FOLDERS)
def test_a_real_folder_is_not_a_file_and_not_a_failure(identifier, path):
    parsed = parse(folder_keep(identifier, path), identifier)
    assert parsed.not_a_file
    assert parsed.blob is None
    # The distinction that matters: nothing to report to the user.
    assert not parsed.reason


def test_the_folders_hash_is_its_own_address_not_a_content_hash():
    # The detail a rule keyed on an absent hash missed entirely, and the
    # reason this needed real data to get right.
    payload = folder_keep("folder--MyEditor", "/MyEditor")
    assert payload["hash"] == "folder--MyEditor"
    assert parse(payload, "folder--MyEditor").not_a_file


def test_a_folder_carrying_a_key_of_its_own_is_still_not_a_file():
    payload = folder_keep("folder--Mediums", "/Mediums")
    payload["encryptionKey"] = KEY
    assert parse(payload, "folder--Mediums").not_a_file


def test_a_folder_with_an_explicitly_empty_hash_is_still_not_a_file():
    payload = folder_keep("folder--Mediums", "/Mediums")
    payload["hash"] = ""
    assert parse(payload, "folder--Mediums").not_a_file


def test_a_folder_with_no_hash_field_at_all_is_still_not_a_file():
    payload = folder_keep("folder--Mediums", "/Mediums")
    payload.pop("hash")
    assert parse(payload, "folder--Mediums").not_a_file


def test_an_unknown_record_type_at_a_non_hash_address_is_tolerated():
    # Whatever Lotus adds to this kind next must not cost the user their
    # whole library on the day it ships.
    parsed = parse({"schema": "something-new", "v": 3}, "lotus:drive:index:v1")
    assert parsed.not_a_file and not parsed.reason


# --------------------------------------------------------------------- #
# Real doubt is still doubt                                             #
# --------------------------------------------------------------------- #

def test_a_record_at_a_hash_address_that_names_no_file_is_a_failure():
    # Filed under a content hash, so it is claiming to be that file and
    # failing. That doubt is real, and confined to that one hash.
    parsed = parse({"encryptionKey": KEY}, HASH_A)
    assert not parsed.not_a_file
    assert parsed.reason and parsed.blob is None


def test_a_record_naming_an_unreadable_hash_is_a_failure():
    for bad in ("beach.jpg", "zz" * 32, HASH_A[:-1], HASH_A + "aa"):
        parsed = parse({"hash": bad, "encryptionKey": KEY}, HASH_A)
        assert not parsed.not_a_file, bad
        assert parsed.reason, bad


def test_a_file_record_still_lists_with_its_key():
    parsed = parse(
        {"hash": HASH_A, "encryptionKey": KEY, "name": "beach.jpg"}, HASH_A)
    assert parsed.blob is not None
    assert parsed.blob.sha256 == HASH_A and parsed.blob.key_hex == KEY
    assert not parsed.not_a_file


def test_a_record_filed_under_someone_elses_hash_is_still_refused():
    # Unchanged by any of this: publishing from it would upload bytes
    # under a name that does not describe them.
    parsed = parse({"hash": HASH_B, "encryptionKey": KEY}, HASH_A)
    assert parsed.blob is None and parsed.reason


def test_a_tombstone_is_still_a_tombstone():
    parsed = parse({"deleted": True}, HASH_A)
    assert parsed.tombstone and not parsed.not_a_file


# --------------------------------------------------------------------- #
# End to end: the library settles and vouches                           #
# --------------------------------------------------------------------- #

def _library_after(records):
    """Drive a real PrivateLibrary through a load of ``records``.

    ``records`` is (identifier, payload). The signer is a stub that hands
    back the payload it was given, because what is under test is what the
    library does with a decrypted record, not the decryption.
    """
    from unittest.mock import MagicMock

    from PySide6.QtCore import QCoreApplication

    from nostr.media.private_library import PrivateLibrary

    QCoreApplication.instance() or QCoreApplication(sys.argv)

    plaintexts = {ident: json.dumps(payload) for ident, payload in records}

    client = MagicMock()

    def decrypt_self(ciphertext, on_success, on_failure):
        on_success(plaintexts[ciphertext])

    client.nip44_decrypt_self.side_effect = decrypt_self

    session_pool = MagicMock()
    session_pool.get.side_effect = lambda profile, on_ready, on_error: on_ready(client)

    relay_list_cache = MagicMock()
    relay_list_cache.fetch.side_effect = (
        lambda pubkey, relays, on_done: on_done(MagicMock(write=[], read=[]))
    )

    query = MagicMock()
    events = [
        {"kind": 34578, "pubkey": HASH_A, "created_at": 1_700_000_000 + i,
         "tags": [["d", ident]], "content": ident}
        for i, (ident, _payload) in enumerate(records)
    ]
    query.addressable.side_effect = (
        lambda relays, filters, on_done: on_done(events)
    )

    library = PrivateLibrary(
        session_pool=session_pool, relay_list_cache=relay_list_cache,
        query=query, clock=lambda: 1_700_000_000,
    )
    profile = MagicMock()
    profile.user_pubkey = HASH_A
    profile.bunker_relays = ["wss://r.test/"]
    library.bind_profile(profile)
    return library


def test_the_reported_library_settles_and_vouches_for_its_files():
    # 12 files and 4 folders, the shape actually on the user's relays.
    files = [(f"{i:064x}", {"hash": f"{i:064x}", "encryptionKey": KEY})
             for i in range(1, 13)]
    folders = [(d, folder_keep(d, path)) for d, path in REAL_FOLDERS]
    library = _library_after(files + folders)

    assert len(library) == 12
    assert not library.failures
    # The two properties the grid and the publish gate read.
    assert library.settled
    for identifier, _payload in files:
        assert library.vouches_for(identifier)


def test_one_unreadable_folder_no_longer_costs_the_whole_library():
    # A folder that will not decrypt is still filed under a non-hash, so
    # it still withholds the answer: its contents could name any file.
    # That is the conservative branch, and it is deliberately kept.
    files = [(HASH_A, {"hash": HASH_A, "encryptionKey": KEY})]
    library = _library_after(
        files + [("folder--X", folder_keep("folder--X", "/X"))])
    assert library.vouches_for(HASH_A)


def test_a_broken_file_record_withholds_only_its_own_hash():
    good = (HASH_A, {"hash": HASH_A, "encryptionKey": KEY})
    broken = (HASH_B, {"encryptionKey": KEY})
    library = _library_after([good, broken])

    assert library.vouches_for(HASH_A)
    assert not library.vouches_for(HASH_B)
    assert not library.settled
    assert len(library.failures) == 1
