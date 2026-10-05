# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private keys kept on this computer, for accounts that sign in with one.

Most people sign with a signer app, and then no private key ever reaches
MyEditor. Someone who creates an account here, or signs in with their
private key, chooses to keep the key on this computer instead. It lives
in one file:

    ~/.config/my_editor/nostr_keys.json     chmod 600, folder chmod 700

readable only by this account, written atomically, keyed by public key.

Why a file and not the system keychain: MyEditor's builds are not signed
with a paid certificate, so on a Mac every update changes the app's code
signature, and the keychain would ask for the login password after each
update. A permissions-locked file is the same protection the signer
pairing secrets already have, and the person chose local storage.

Every key read back is checked against the public key it is filed under,
so a damaged or edited file can never sign as somebody else.

The file is never overwritten from a read that failed. A file that can't
be read (permissions, a disk error) stops every change with KeyVaultError,
so no key in it is lost to the next save. A file that reads but isn't a
key file (damaged, edited by hand) is moved aside to
``nostr_keys.json.corrupt-<time>`` before a new one is written, so the
keys in it can still be recovered by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

from nostr import crypto

KEYS_DIR = Path.home() / ".config" / "my_editor"
KEYS_FILE = KEYS_DIR / "nostr_keys.json"


class KeyVaultError(OSError):
    """The key file couldn't be read, so it was left as it is."""


class _Damaged(Exception):
    """The key file reads, but isn't a key file."""


class KeyVault:
    """Secret keys by public key, in a file only this account can read."""

    def __init__(self, path: Path = KEYS_FILE) -> None:
        self._path = Path(path)

    def store(self, secret_key: bytes) -> str:
        """Keep ``secret_key``; return its public key (hex)."""
        if len(secret_key) != 32:
            raise ValueError("a secret key is 32 bytes")
        pubkey = crypto.get_public_key(secret_key).hex()
        keys = self._read_for_change()
        keys[pubkey] = secret_key.hex()
        self._write(keys)
        return pubkey

    def load(self, pubkey_hex: str) -> Optional[bytes]:
        """The secret key filed under ``pubkey_hex``, or None.

        None as well when the stored key does not belong to that public
        key: a key that would sign as someone else is not a key.
        """
        try:
            keys = self._read()
        except (KeyVaultError, _Damaged):
            return None
        value = keys.get((pubkey_hex or "").lower())
        if not isinstance(value, str):
            return None
        try:
            secret = bytes.fromhex(value)
        except ValueError:
            return None
        if len(secret) != 32:
            return None
        try:
            if crypto.get_public_key(secret).hex() != pubkey_hex.lower():
                return None
        except Exception:  # noqa: BLE001, an invalid scalar is not a key
            return None
        return secret

    def has(self, pubkey_hex: str) -> bool:
        """A usable key for ``pubkey_hex`` is on this computer."""
        return self.load(pubkey_hex) is not None

    def forget(self, pubkey_hex: str) -> None:
        keys = self._read_for_change()
        if keys.pop((pubkey_hex or "").lower(), None) is not None:
            self._write(keys)

    # -- file ----------------------------------------------------------------------

    def _read(self) -> Dict[str, str]:
        """The keys on file. {} only when there is no file yet."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise KeyVaultError(f"the key file {self._path} can’t be read") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:      # UnicodeDecodeError is a ValueError too
            raise _Damaged() from exc
        keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(keys, dict):
            raise _Damaged()
        return {k: v for k, v in keys.items() if isinstance(k, str)}

    def _read_for_change(self) -> Dict[str, str]:
        """The keys to change and write back. A damaged file is kept aside
        first; an unreadable one stops the change (KeyVaultError)."""
        try:
            return self._read()
        except _Damaged:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            aside = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
            count = 1
            while aside.exists():
                count += 1
                aside = self._path.with_name(f"{self._path.name}.corrupt-{stamp}-{count}")
            try:
                os.replace(self._path, aside)
            except OSError as exc:
                raise KeyVaultError(f"the damaged key file {self._path} can’t be "
                                    "moved aside") from exc
            return {}

    def _write(self, keys: Dict[str, str]) -> None:
        folder = self._path.parent
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".nostr_keys_", suffix=".tmp", dir=str(folder))
        try:
            os.chmod(tmp, 0o600)   # before a single key byte is written
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "keys": keys}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
