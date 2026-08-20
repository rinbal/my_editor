# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asset identity, upload state, and the persisted asset index.

An asset is one image this app itself put into a document. Its identity
is the sha256 of the bytes, so the same picture pasted twice is one
asset with one upload. The bytes live in the content-addressed blob
cache; this module owns the metadata that cache cannot express (alt
text, caption, where the blob was uploaded, how far the upload got).

The in-document name of an asset is ``myeditor-asset:<sha256>``. That
key is an app-internal handle: it must never reach a file the user can
open outside the app, and it is the single gate between a
document-supplied string and any dictionary or filesystem lookup, so a
hostile document name cannot be turned into a path.

Nothing here touches a network, a signer or a server. Only the standard
library and QtCore are imported on purpose: the asset layer stays
usable, and testable, with no protocol code loaded.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QTimer


# ---------------------------------------------------------------------------
# Asset keys
# ---------------------------------------------------------------------------

ASSET_SCHEME = "myeditor-asset"

_ASSET_KEY_RE = re.compile(r"^myeditor-asset:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def asset_key(sha256: str) -> str:
    """The in-document name for the blob with this hash."""
    return f"{ASSET_SCHEME}:{(sha256 or '').lower()}"


def parse_asset_key(name: str) -> Optional[str]:
    """The sha256 inside ``name``, or None when it is not an asset key.

    Full match only. Every resolve path calls this before it builds a
    path or looks anything up, which is what makes a hostile document
    name such as ``myeditor-asset:../../etc/passwd`` inert.
    """
    if not isinstance(name, str):
        return None
    if not _ASSET_KEY_RE.match(name):
        return None
    return name.split(":", 1)[1]


def is_asset_key(name: str) -> bool:
    return parse_asset_key(name) is not None


def is_sha256(value: str) -> bool:
    """Whether ``value`` is a bare lowercase sha256 hex digest."""
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


# ---------------------------------------------------------------------------
# Upload state
# ---------------------------------------------------------------------------

class AssetState(str, Enum):
    """How far the upload of an asset has got.

    The states exist independently of any one uploader. With the current
    ``MediaStore`` the MIRRORED step is unobservable (it reports one
    finish after mirroring), so the observed path is UPLOADING then
    COMPLETE; MIRRORED stays for an uploader that reports it.
    """

    LOCAL = "local"
    QUEUED = "queued"
    SIGNING = "signing"
    UPLOADING = "uploading"
    MIRRORED = "mirrored"
    COMPLETE = "complete"
    FAILED = "failed"


# The only legal moves. FAILED is a decorated LOCAL, never a loss, so it
# leads back to QUEUED and nowhere else; MIRRORED cannot fail because
# the bytes are already on a server; COMPLETE is terminal.
LEGAL_TRANSITIONS: Dict[AssetState, frozenset] = {
    AssetState.LOCAL: frozenset({AssetState.QUEUED}),
    AssetState.QUEUED: frozenset({AssetState.SIGNING, AssetState.FAILED}),
    AssetState.SIGNING: frozenset({AssetState.UPLOADING, AssetState.FAILED}),
    AssetState.UPLOADING: frozenset(
        {AssetState.MIRRORED, AssetState.COMPLETE, AssetState.FAILED}
    ),
    AssetState.MIRRORED: frozenset({AssetState.COMPLETE}),
    AssetState.COMPLETE: frozenset(),
    AssetState.FAILED: frozenset({AssetState.QUEUED}),
}

# States that only make sense while a job is running. A record persisted
# mid-upload outlives the job, so it comes back as LOCAL and can be
# requested again; otherwise a crash would strand an asset forever.
_TRANSIENT_STATES = frozenset(
    {AssetState.QUEUED, AssetState.SIGNING, AssetState.UPLOADING}
)


# ---------------------------------------------------------------------------
# The asset record
# ---------------------------------------------------------------------------

@dataclass
class DocumentAsset:
    """One image the app owns, identified by the hash of its bytes."""

    sha256: str
    mime: str = "application/octet-stream"
    size: int = 0
    alt: str = ""
    caption: str = ""
    width: int = 0                    # 0 means not known yet
    height: int = 0
    upload_state: AssetState = AssetState.LOCAL
    remote_url: str = ""              # meaningful only once uploaded
    servers: List[str] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0
    attempts: int = 0
    failure_code: str = ""
    failure_reason: str = ""          # diagnostic, never shown to the user
    # Derived from the blob store on demand, never persisted: a machine
    # local path must not travel between installs or into a document.
    local_path: str = ""

    @property
    def key(self) -> str:
        return asset_key(self.sha256)

    @property
    def is_uploaded(self) -> bool:
        return (
            self.upload_state in (AssetState.MIRRORED, AssetState.COMPLETE)
            and bool(self.remote_url)
        )

    @property
    def can_retry(self) -> bool:
        return self.upload_state is AssetState.FAILED

    def to_record(self) -> dict:
        """The persisted shape: every field except ``local_path``."""
        return {
            "sha256": self.sha256,
            "mime": self.mime,
            "size": self.size,
            "alt": self.alt,
            "caption": self.caption,
            "width": self.width,
            "height": self.height,
            "upload_state": self.upload_state.value,
            "remote_url": self.remote_url,
            "servers": list(self.servers),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "failure_code": self.failure_code,
            "failure_reason": self.failure_reason,
        }


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _acceptable_remote_url(url: str, sha256: str) -> bool:
    """Whether a blob URL may be stored against ``sha256``.

    Deliberately a local rule with no imports: the asset layer must stay
    free of protocol code. It mirrors the media policy used everywhere
    else (https, or http only on loopback for dev servers, no userinfo)
    and adds the hash agreement BUD-03 defines: when the URL carries a
    64 character hex run, the last one is the blob's hash and must be
    the hash this asset claims. A URL with no hex run is accepted
    because its origin was validated where it was parsed.

    Query and fragment are excluded from that scan. A signed or
    tokenised blob URL can carry an unrelated 64 character hex run in
    its token, and reading that as the blob's name rejected a
    spec-compliant server's successful upload.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if parts.scheme == "http":
        if host not in _LOOPBACK_HOSTS:
            return False
    elif parts.scheme != "https":
        return False
    path_only = url.lower().split("#", 1)[0].split("?", 1)[0]
    runs = _HEX64_RE.findall(path_only)
    if runs and runs[-1] != (sha256 or "").lower():
        return False
    return True


def _clean_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _clean_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= 0 else default


def asset_from_record(data: object) -> Optional[DocumentAsset]:
    """Rebuild an asset from a persisted record, or None to drop it.

    Degrade, never raise: a record with one bad field keeps the rest,
    and only an unusable identity drops the whole entry.
    """
    if not isinstance(data, dict):
        return None
    sha = _clean_str(data.get("sha256")).strip().lower()
    if not is_sha256(sha):
        return None

    try:
        state = AssetState(_clean_str(data.get("upload_state")))
    except ValueError:
        state = AssetState.LOCAL
    if state in _TRANSIENT_STATES:
        state = AssetState.LOCAL

    remote_url = _clean_str(data.get("remote_url"))
    if remote_url and not _acceptable_remote_url(remote_url, sha):
        remote_url = ""
    if not remote_url and state in (AssetState.MIRRORED, AssetState.COMPLETE):
        # An "uploaded" record with no usable URL cannot be published and
        # could never be retried; LOCAL is the honest, recoverable state.
        state = AssetState.LOCAL

    servers = data.get("servers")
    if isinstance(servers, list):
        servers = [s for s in servers if isinstance(s, str) and s]
    else:
        servers = []

    return DocumentAsset(
        sha256=sha,
        mime=_clean_str(data.get("mime"), "application/octet-stream"),
        size=_clean_int(data.get("size")),
        alt=_clean_str(data.get("alt")),
        caption=_clean_str(data.get("caption")),
        width=_clean_int(data.get("width")),
        height=_clean_int(data.get("height")),
        upload_state=state,
        remote_url=remote_url,
        servers=servers,
        created_at=_clean_int(data.get("created_at")),
        updated_at=_clean_int(data.get("updated_at")),
        attempts=_clean_int(data.get("attempts")),
        failure_code=_clean_str(data.get("failure_code")),
        failure_reason=_clean_str(data.get("failure_reason")),
    )


# ---------------------------------------------------------------------------
# The persisted index
# ---------------------------------------------------------------------------

INDEX_DIR = Path.home() / ".config" / "my_editor"
INDEX_FILE = INDEX_DIR / "media_assets.json"

CURRENT_INDEX_VERSION = 1

_SAVE_DEBOUNCE_MS = 2_000


class AssetIndex(QObject):
    """Asset metadata on disk: one versioned JSON file, atomic writes.

    A JSON file and not a database on purpose: this is a few hundred
    small records, and a database would be a new dependency and a new
    failure mode for no gain.

    The index is DERIVED AND DISPOSABLE. Deleting it loses cached
    ``remote_url``, ``alt`` and ``caption``, never an image: the bytes
    live in the content-addressed cache and the upload URL is
    recoverable from the media library. That is why a write failure
    degrades to an in-memory session instead of blocking an insert.

    Eviction rule for any future cache GC, recorded here because this is
    where the references live: a blob is evictable only when no index
    entry references it, no open document references its key, no live
    crash backup lists it, and never while ``read_only`` or ``degraded``
    is set (both mean this process does not know the whole picture).
    """

    def __init__(self, path: Path = INDEX_FILE, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._assets: Dict[str, DocumentAsset] = {}
        self._degraded = False
        self._read_only = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_SAVE_DEBOUNCE_MS)
        self._timer.timeout.connect(self.save)

        self._load()

    # -- read --------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def degraded(self) -> bool:
        """True when a read or a write failed. The session still works."""
        return self._degraded

    @property
    def read_only(self) -> bool:
        """True when the file was written by a newer build of the app."""
        return self._read_only

    def get(self, sha256: str) -> Optional[DocumentAsset]:
        return self._assets.get((sha256 or "").lower())

    def values(self) -> List[DocumentAsset]:
        return list(self._assets.values())

    def __contains__(self, sha256: object) -> bool:
        return isinstance(sha256, str) and sha256.lower() in self._assets

    def __len__(self) -> int:
        return len(self._assets)

    # -- mutate ------------------------------------------------------------

    def put(self, asset: DocumentAsset) -> None:
        """Record ``asset`` and schedule a debounced write."""
        self._assets[asset.sha256] = asset
        self._timer.start()

    def flush(self) -> None:
        """Write now if anything is pending. Called on app shutdown."""
        pending = self._timer.isActive()
        self._timer.stop()
        if pending:
            self.save()

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
            version = CURRENT_INDEX_VERSION
        if version > CURRENT_INDEX_VERSION:
            # A newer build owns this file. Read nothing and write
            # nothing, so a downgrade can never clobber it.
            self._read_only = True
            return

        records = data.get("assets")
        if not isinstance(records, list):
            self._degraded = True
            return
        for entry in records:
            asset = asset_from_record(entry)
            if asset is not None:
                self._assets[asset.sha256] = asset   # last record wins

    def save(self) -> bool:
        """Write the index atomically. Returns False when it did not.

        Deliberately never raises, unlike ``BlossomSettings._save``: the
        bytes are already durable in the content-addressed cache, so an
        unwritable config directory must degrade to an in-memory session
        rather than break an insert.
        """
        if self._read_only:
            return False

        payload = {
            "version": CURRENT_INDEX_VERSION,
            "assets": [a.to_record() for a in self._assets.values()],
        }
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._degraded = True
            return False
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".media_assets_", suffix=".json.tmp", dir=str(directory)
            )
        except OSError:
            self._degraded = True
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self._degraded = True
            return False

        self._timer.stop()
        return True
