# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which media is private, which is public, and the bridge between them.

Private and public are not two values of one flag here, they are two
types. A private blob cannot exist without a decryption key and a public
blob has nowhere to put one, so the shapes themselves stop a private
key or a private hash being handed to something that publishes. A
boolean would not: every call site would have to remember to check it,
and one that forgets leaks a file.

Making a private file public is never a move, a flag or a mirror. The
bytes on the server are ciphertext, so the only way to give a reader
something they can open is to decrypt locally and upload a second,
independent blob. That copy has its own hash and its own life. The
private original is never modified, re-keyed or deleted by the act of
publishing one, which is what makes the operation safe to offer at all.

The ledger below is the authority on what is currently public. Code asks
it, not the pointer on the private record, because the two can disagree:
a revoke that half succeeded leaves a pointer naming a blob that no
longer exists, and trusting the pointer would then both claim the file
is published and refuse to publish it again.

The ledger is written to disk. The per-file decryption keys are not:
they belong to the encrypted metadata that travels with the account, and
a plaintext key sitting in a JSON file beside the ciphertext would undo
the encryption entirely.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import assets as _assets


LEDGER_FILE = _assets.INDEX_DIR / "media_public.json"
CURRENT_LEDGER_VERSION = 1

_HEX64_LEN = 64


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX64_LEN
        and all(c in "0123456789abcdef" for c in value.lower())
    )


@dataclass(frozen=True)
class PrivateBlob:
    """An encrypted blob, plus the key that opens it.

    ``key_hex`` is required and has no default. A record that reached
    this type has a key by construction, so nothing downstream has to
    handle the "private but unopenable" case that a nullable field would
    invent.
    """

    sha256: str
    key_hex: str
    servers: List[str] = field(default_factory=list)
    size: int = 0
    mime: str = ""
    name: str = ""
    uploaded_at: int = 0
    # The public copy minted from this file, if one has been. A hint for
    # the UI and a reuse shortcut; the ledger is what actually decides.
    public_copy_hash: str = ""

    def __post_init__(self) -> None:
        if not _is_sha256(self.sha256):
            raise ValueError("private blob needs a sha256")
        if not self.key_hex:
            raise ValueError("a private blob without its key is unopenable")


@dataclass(frozen=True)
class PublicBlob:
    """A plaintext blob anyone can fetch.

    There is deliberately no key field. This type is what gets embedded
    in published content, so the absence is the guarantee: a key cannot
    be leaked through a shape that has nowhere to hold one.
    """

    sha256: str
    url: str
    servers: List[str] = field(default_factory=list)
    size: int = 0
    mime: str = ""
    uploaded_at: int = 0
    # The private original this was minted from, when it was. Lets the
    # UI trace a public link back to the file it came from, and lets a
    # second publish of the same original find the existing copy.
    source_hash: str = ""
    # Whether camera and location metadata were removed before upload.
    scrubbed: bool = False

    def __post_init__(self) -> None:
        if not _is_sha256(self.sha256):
            raise ValueError("public blob needs a sha256")
        if not self.url:
            raise ValueError("public blob needs a URL")


def public_blob_from_record(entry: object) -> Optional[PublicBlob]:
    """Parse one persisted entry, or None when it is unusable.

    A single corrupt row must not cost the user the rest of their
    ledger, so this returns None rather than raising and the caller
    skips it.
    """
    if not isinstance(entry, dict):
        return None
    try:
        servers = entry.get("servers")
        return PublicBlob(
            sha256=str(entry.get("sha256", "")).lower(),
            url=str(entry.get("url", "")),
            servers=[str(s) for s in servers] if isinstance(servers, list) else [],
            size=int(entry.get("size", 0) or 0),
            mime=str(entry.get("mime", "")),
            uploaded_at=int(entry.get("uploaded_at", 0) or 0),
            source_hash=str(entry.get("source_hash", "")).lower(),
            scrubbed=bool(entry.get("scrubbed", False)),
        )
    except (ValueError, TypeError):
        return None


def public_blob_to_record(blob: PublicBlob) -> dict:
    return {
        "sha256": blob.sha256,
        "url": blob.url,
        "servers": list(blob.servers),
        "size": blob.size,
        "mime": blob.mime,
        "uploaded_at": blob.uploaded_at,
        "source_hash": blob.source_hash,
        "scrubbed": blob.scrubbed,
    }


class PublicLedger:
    """The record of every blob this app has deliberately made public.

    Persisted as one versioned JSON file with atomic writes, matching
    ``AssetIndex``. Unlike that index this one is NOT disposable: it is
    the only list of what has been published and therefore the only way
    to revoke any of it. Losing it does not expose anything new, but it
    does strand public blobs with no way to find them again, so a write
    that fails is reported rather than shrugged off.

    Entries are committed one at a time on purpose. A crash between two
    copies must never leave a blob public and unlisted.
    """

    def __init__(self, path: Path = LEDGER_FILE) -> None:
        self._path = Path(path)
        self._blobs: Dict[str, PublicBlob] = {}
        self._degraded = False
        self._read_only = False
        self._load()

    # -- state -------------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """The file existed but could not be read as a whole."""
        return self._degraded

    @property
    def read_only(self) -> bool:
        """A newer build owns the file, so this one must not write it."""
        return self._read_only

    def __len__(self) -> int:
        return len(self._blobs)

    def __iter__(self):
        return iter(self._blobs.values())

    # -- queries -----------------------------------------------------------

    def get(self, sha256: str) -> Optional[PublicBlob]:
        return self._blobs.get((sha256 or "").lower())

    def is_public(self, sha256: str) -> bool:
        return (sha256 or "").lower() in self._blobs

    def copy_of(self, source_hash: str) -> Optional[PublicBlob]:
        """The live public copy minted from ``source_hash``, if any.

        Looked up by source rather than by the private record's pointer,
        so a stale or cleared pointer still finds the copy and a revoked
        copy is correctly reported as absent.
        """
        key = (source_hash or "").lower()
        if not key:
            return None
        for blob in self._blobs.values():
            if blob.source_hash == key:
                return blob
        return None

    def has_public_copy(self, private: PrivateBlob) -> bool:
        """Whether this private file currently has a live public copy.

        The pointer is tried first because it is a direct hit, but it is
        only believed when the ledger agrees. Believing it alone would
        badge a revoked link as published and then refuse to re-publish
        it, which is the worst of both.
        """
        pointed = self.get(private.public_copy_hash) if private.public_copy_hash else None
        if pointed is not None and pointed.source_hash == private.sha256.lower():
            return True
        return self.copy_of(private.sha256) is not None

    # -- mutation ----------------------------------------------------------

    def record(self, blob: PublicBlob) -> bool:
        """Add or replace an entry and persist immediately.

        Returns False when it could not be written, and the caller must
        treat that as "do not upload the next one": an unlisted public
        blob cannot be revoked by a user who cannot see it.
        """
        self._blobs[blob.sha256.lower()] = blob
        return self.save()

    def forget(self, sha256: str) -> bool:
        """Drop an entry, for instance after it was deleted everywhere."""
        self._blobs.pop((sha256 or "").lower(), None)
        return self.save()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._degraded = True
            return
        if not isinstance(data, dict):
            self._degraded = True
            return

        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            version = CURRENT_LEDGER_VERSION
        if version > CURRENT_LEDGER_VERSION:
            # Written by a newer build. Read nothing and write nothing,
            # so running an older build cannot truncate someone's record
            # of what they have published.
            self._read_only = True
            return

        records = data.get("public")
        if not isinstance(records, list):
            self._degraded = True
            return
        for entry in records:
            blob = public_blob_from_record(entry)
            if blob is not None:
                self._blobs[blob.sha256] = blob

    def save(self) -> bool:
        """Write the ledger atomically. Returns False when it did not."""
        if self._read_only:
            return False
        if self._degraded:
            # The file exists and could not be read, so this object holds
            # none of what is in it. Writing would replace the record of
            # everything the user has ever published with whatever one
            # entry happened to be added since, and this ledger is the
            # only way to revoke any of it. Refusing costs the caller a
            # publish; writing would cost the user the list.
            return False
        payload = {
            "version": CURRENT_LEDGER_VERSION,
            "public": [public_blob_to_record(b) for b in self._blobs.values()],
        }
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError:
            pass
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=self._path.name + ".", dir=str(directory),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
            return True
        except OSError:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return False


def needs_public_copy(
    blobs: Iterable[PrivateBlob], ledger: PublicLedger,
) -> List[PrivateBlob]:
    """The private blobs that would have to be copied to publish these.

    Checked against the ledger rather than each record's pointer, so a
    copy that was revoked is correctly reported as needing a new one.
    """
    return [b for b in blobs if not ledger.has_public_copy(b)]
