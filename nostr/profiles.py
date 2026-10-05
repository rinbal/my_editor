# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent store for Nostr profiles.

A profile is the editor's record of one account and how it signs.

Most profiles are a signer connection (``signer == "remote"``): the user's
real nsec lives only inside the remote signer (Amber, nsec.app, …), and
the ``local_secret_hex`` we store here is the *editor-side* keypair for
the NIP-46 channel, not their real key.

A profile with ``signer == "local"`` is an account whose private key the
person chose to keep on this computer (Create Account, or signing in with
a private key). Its key is in nostr/key_vault.py, never in this file; its
``bunker_relays`` hold the relays the account starts out with, so every
place that asks "where does this user's traffic go" has an answer.

``setup_pending`` marks an account created in MyEditor whose relay list
and name haven't reached the network yet. The account controller
(nostr/account_controller.py) finishes that the next time the account is
used, and clears the flag.

Storage:
  ~/.config/my_editor/nostr_profiles.json - chmod 600

The file is rewritten atomically (temp file + rename) so a crash mid-write
cannot corrupt the store.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional


PROFILES_DIR = Path.home() / ".config" / "my_editor"
PROFILES_FILE = PROFILES_DIR / "nostr_profiles.json"


@dataclass
class Profile:
    """One account: how it signs, plus cached display metadata."""

    user_pubkey: str                       # hex, 64 chars - the user's real Nostr identity
    bunker_pubkey: str                     # hex, 64 chars - remote-signer relay identity ("" when local)
    bunker_relays: List[str]               # remote: relays the signer listens on;
                                           # local: relays the account starts out with
    local_secret_hex: str                  # remote: editor-side key for the signer channel ("" when local)
    display_name: str = ""                 # from kind 0; may be empty
    picture: str = ""                      # avatar URL from kind 0; may be empty
    nip05: str = ""                        # NIP-05 identifier if set
    metadata_cached_at: int = 0            # unix seconds - 0 means never fetched
    avatar_path: str = ""                  # local cache path for the avatar pixmap
    signer: str = "remote"                 # "remote" (NIP-46 signer app) or "local" (key on this computer)
    setup_pending: bool = False            # created here, and its relay list and name aren't
                                           # published yet; finished when it is next used

    @property
    def is_local(self) -> bool:
        return self.signer == "local"

    def npub_short(self) -> str:
        """First and last 4 hex chars - for UI fallback display."""
        return f"{self.user_pubkey[:8]}…{self.user_pubkey[-4:]}"


# --------------------------------------------------------------------------- #
# Store                                                                        #
# --------------------------------------------------------------------------- #

class ProfileStore:
    """JSON-backed dictionary of profiles, keyed by user_pubkey.

    All mutation methods persist immediately. Reads are served from
    an in-memory copy populated on construction.
    """

    def __init__(self, path: Path = PROFILES_FILE) -> None:
        self._path = path
        self._profiles: dict[str, Profile] = {}
        self._default_pubkey: Optional[str] = None
        self._load()

    # -- read --------------------------------------------------------------

    def __iter__(self) -> Iterator[Profile]:
        return iter(self._profiles.values())

    def __len__(self) -> int:
        return len(self._profiles)

    def __contains__(self, user_pubkey: str) -> bool:
        return user_pubkey in self._profiles

    def get(self, user_pubkey: str) -> Optional[Profile]:
        return self._profiles.get(user_pubkey)

    def list(self) -> List[Profile]:
        return list(self._profiles.values())

    def default(self) -> Optional[Profile]:
        if self._default_pubkey and self._default_pubkey in self._profiles:
            return self._profiles[self._default_pubkey]
        # If the stored default is stale, fall back to any profile we have.
        if self._profiles:
            return next(iter(self._profiles.values()))
        return None

    # -- mutate ------------------------------------------------------------

    def upsert(self, profile: Profile) -> None:
        """Add or replace by ``user_pubkey``. If this is the first profile,
        it becomes the default."""
        self._profiles[profile.user_pubkey] = profile
        if self._default_pubkey is None:
            self._default_pubkey = profile.user_pubkey
        self._save()

    def remove(self, user_pubkey: str) -> bool:
        if user_pubkey not in self._profiles:
            return False
        del self._profiles[user_pubkey]
        if self._default_pubkey == user_pubkey:
            self._default_pubkey = next(iter(self._profiles), None)
        self._save()
        return True

    def set_default(self, user_pubkey: str) -> None:
        if user_pubkey not in self._profiles:
            raise KeyError(f"unknown profile {user_pubkey}")
        self._default_pubkey = user_pubkey
        self._save()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for entry in data.get("profiles", []):
            try:
                profile = Profile(**entry)
            except TypeError:
                continue  # unknown field shape - skip silently
            self._profiles[profile.user_pubkey] = profile
        default = data.get("default")
        if isinstance(default, str) and default in self._profiles:
            self._default_pubkey = default

    def _save(self) -> None:
        folder = self._path.parent
        folder.mkdir(parents=True, exist_ok=True)
        # Try to lock down the directory too. mkdir's mode= is masked by umask
        # on some setups; chmod after the fact gets us there reliably.
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass

        payload = {
            "default": self._default_pubkey,
            "profiles": [asdict(p) for p in self._profiles.values()],
        }

        # Atomic write: tmp file in the same directory, rename into place.
        # Same directory is important - rename across filesystems is not atomic.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".nostr_profiles_", suffix=".json.tmp", dir=str(folder)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except OSError:
            # Best-effort cleanup; re-raise so the caller knows the write failed.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
