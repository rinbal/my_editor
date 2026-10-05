# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The account backup file: what it says, and how a key is read back.

A Nostr account has no password reset. The private key is the only way
back in, so Create Account offers a backup file, and Restore Account
reads one. The file is plain text a person can read and print:

    MyEditor Nostr account backup
    ...
    Public key (share it freely):
    npub1...

    Protected private key (opens with your backup password):
    ncryptsec1...

The protected key is NIP-49, which other Nostr apps import too. Saving the
key unprotected (nsec) is possible but never the default.

Reading is forgiving on purpose, because the file may have been printed,
retyped, or pasted into an email: the first ``ncryptsec1...`` or
``nsec1...`` anywhere in the text is the key, and a bare 64-character hex
key is accepted from the paste field. Never from a file: a file full of
hex (an event id, a hash) would be guessed at. A pasted hex key that is the
public key of an account MyEditor knows is refused as what it is.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from nostr import bech32, crypto, nip49

FILE_SUFFIX = ".txt"

_NCRYPTSEC = re.compile(r"ncryptsec1[02-9ac-hj-np-z]{60,}", re.IGNORECASE)
_NSEC = re.compile(r"nsec1[02-9ac-hj-np-z]{58}", re.IGNORECASE)
_HEX = re.compile(r"\A[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True)
class FoundKey:
    """A key found in a file or a paste.

    ``protected`` keys need ``open_with(password)``; an unprotected one
    already carries ``secret``. Neither the key text nor the secret is
    part of its repr, so a log line or a traceback never carries a key.
    """

    text: str = field(repr=False)
    protected: bool
    secret: Optional[bytes] = field(default=None, repr=False)

    def open_with(self, password: str) -> bytes:
        """The secret key, or nip49.Nip49Error."""
        if not self.protected:
            return self.secret
        return nip49.decrypt(self.text, password)


class BackupError(ValueError):
    """Why nothing usable was found. The message is for the person."""


def suggested_file_name(pubkey_hex: str) -> str:
    npub = bech32.encode_npub(pubkey_hex)
    return f"nostr-account-{npub[5:13]}{FILE_SUFFIX}"


def backup_text(secret_key: bytes, *, password: Optional[str],
                today: Optional[datetime.date] = None) -> str:
    """The backup file's contents. ``password`` None means unprotected."""
    pubkey = crypto.get_public_key(secret_key).hex()
    npub = bech32.encode_npub(pubkey)
    day = (today or datetime.date.today()).isoformat()
    if password is not None:
        key_heading = "Protected private key (opens with your backup password):"
        key = nip49.encrypt(secret_key, password)
        warning = ("Keep this file and its password apart. Anyone who has both "
                   "can use your account.")
    else:
        key_heading = "Private key (this opens your account, keep it secret):"
        key = bech32.encode_nsec(secret_key.hex())
        warning = ("This file is not protected by a password. Anyone who reads "
                   "it can use your account. Keep it somewhere only you can reach.")
    return "\n".join([
        "MyEditor Nostr account backup",
        f"Saved {day}",
        "",
        "Public key (share it freely):",
        npub,
        "",
        key_heading,
        key,
        "",
        warning,
        "",
        "To restore: in MyEditor, choose Nostr > Restore Account and open this file.",
        "Other Nostr apps can import this key too.",
        "Nobody can reset this key for you. Without it, the account can't be recovered.",
        "",
    ])


_PUBLIC_KEY = ("That is a public key. It shows who you are but can’t sign in. "
               "Use the private key from your backup.")


def find_key(text: str, *, allow_hex: bool = False,
             public_keys: Iterable[str] = ()) -> FoundKey:
    """The key in ``text``, protected or not. BackupError when there is none.

    ``public_keys`` are the hex public keys of accounts MyEditor knows: a
    pasted hex key equal to one of them is a public key, not a private one.
    """
    text = text or ""
    match = _NCRYPTSEC.search(text)
    if match:
        return FoundKey(text=match.group(0).lower(), protected=True)
    match = _NSEC.search(text)
    if match:
        try:
            secret = bytes.fromhex(bech32.decode_nsec(match.group(0).lower()))
        except Exception as exc:  # noqa: BLE001, any decoding failure is a typo
            raise BackupError("That private key is incomplete or mistyped. Copy the "
                              "whole key from your backup and try again.") from exc
        return FoundKey(text=match.group(0).lower(), protected=False,
                        secret=_checked(secret))
    candidate = text.strip()
    if allow_hex and _HEX.match(candidate):
        if candidate.lower() in {(k or "").lower() for k in public_keys}:
            raise BackupError(_PUBLIC_KEY)
        return FoundKey(text=candidate.lower(), protected=False,
                        secret=_checked(bytes.fromhex(candidate)))
    if candidate.lower().startswith("npub1"):
        raise BackupError(_PUBLIC_KEY)
    raise BackupError("No private key was found. Choose the backup file you saved "
                      "when you created the account, or paste the whole key.")


def _checked(secret: bytes) -> bytes:
    """A 32-byte scalar that is a valid secp256k1 private key."""
    try:
        crypto.get_public_key(secret)
    except Exception as exc:  # noqa: BLE001
        raise BackupError("That key can’t be used as a private key.") from exc
    return secret


def password_problem(password: str, confirmation: str) -> Optional[str]:
    """Why a backup password can't be used yet, in plain words, or None.

    Length is the only rule: a long passphrase is what makes the file
    safe if it leaks, and anything stricter only gets written on a note.
    """
    if len(password) < 8:
        return "Use at least 8 characters."
    if password != confirmation:
        return "The passwords don’t match."
    return None
