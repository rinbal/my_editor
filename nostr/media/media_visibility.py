# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the interface is allowed to know about one blob.

Three objects already hold pieces of this answer. ``PrivateLibrary``
knows which files are encrypted and holds their keys, ``PublicLedger``
knows what this app has deliberately made public, and the media grid
knows only a sha256. Wiring all three into every widget would put a
per-file key inside a dialog, and a key inside a dialog is a key one
careless ``print`` away from a log.

So the widgets ask this instead. It answers in words a grid can render,
it answers about copies with :class:`PublicBlob`, which has nowhere to
hold a key, and the one operation that genuinely needs the key,
decrypting a thumbnail, happens *here* rather than in the caller. The
key is read out of the library and passed to the decrypt in the same
expression; no UI module ever names it.

Both halves are optional. An account with no signer connected has no
private library at all, and a fresh install has an empty ledger; in both
cases every blob is simply public, which is the honest answer and the
same one the app gave before any of this existed.

A library that exists but has not been read is a different thing
entirely, and the difference is the point of :data:`UNKNOWN`. Reading
the library costs a signer round-trip per file, so there is a window,
and a signer that never answers makes the window permanent. Through it,
"not in the library" does not mean "public", it means nobody has looked.
Reporting that as public is how an encrypted file ends up addressed in a
signed event, so it gets its own answer and every caller has to handle
it: the grid marks it, and the publish gate refuses it.
"""

from __future__ import annotations

from typing import Final, Iterable, List, Optional, Protocol, Sequence

from .private_preview import PreviewOutcome, preview_from_envelope
from .visibility import PrivateBlob, PublicBlob


# The four states a listed blob can be in, as plain strings so a widget
# can name one without importing this module.
PUBLIC: Final[str] = "public"
# Encrypted on the server, openable here, and not copied anywhere.
PRIVATE: Final[str] = "private"
# Encrypted on the server, and a public copy of it currently exists.
PUBLISHED_COPY: Final[str] = "published-copy"
# Not checked. The private library could not account for this hash, so
# whether it is private is not known. Never treat this as public.
UNKNOWN: Final[str] = "unknown"


class PrivateFiles(Protocol):
    """The part of ``PrivateLibrary`` this needs.

    ``vouches_for`` is required rather than optional on purpose. A
    library that cannot be asked whether its answer is complete is a
    library whose silence gets read as "public", and that is the exact
    failure this protocol exists to make impossible to write.
    """

    def get(self, sha256: str) -> Optional[PrivateBlob]: ...

    def vouches_for(self, sha256: str) -> bool: ...


class PublicRecord(Protocol):
    """The part of ``PublicLedger`` this needs."""

    def copy_of(self, source_hash: str) -> Optional[PublicBlob]: ...

    def get(self, sha256: str) -> Optional[PublicBlob]: ...

    def forget(self, sha256: str) -> bool: ...


class MediaVisibility:
    """Answers "what is this blob, and has it been copied out".

    Deliberately not a QObject and deliberately stateless: it caches
    nothing, so it cannot disagree with the library or the ledger it
    reads. Both are cheap in-memory lookups.
    """

    def __init__(
        self,
        *,
        library: Optional[PrivateFiles] = None,
        ledger: Optional[PublicRecord] = None,
    ) -> None:
        self._library = library
        self._ledger = ledger

    # -- what a blob is ----------------------------------------------------

    def state_of(self, sha256: str) -> str:
        """One of :data:`PUBLIC`, :data:`PRIVATE`, :data:`PUBLISHED_COPY`,
        :data:`UNKNOWN`.

        The last is what makes the other three trustworthy. Absence from
        the library is only :data:`PUBLIC` when the library says its own
        listing is complete for this hash; otherwise nobody has looked,
        and saying so is the difference between warning the user and
        publishing their private picture on their behalf.
        """
        blob = self._private(sha256)
        if blob is not None:
            return (
                PUBLISHED_COPY if self.public_copy_of(sha256) is not None else PRIVATE
            )
        return PUBLIC if self._vouched(sha256) else UNKNOWN

    def is_private(self, sha256: str) -> bool:
        """Whether this app holds a key for this blob.

        Narrower than "not public": a file nobody has checked is not
        private by this test, because there is no key here for it. Use
        :meth:`state_of` for the question a publish gate has to ask.
        """
        return self._private(sha256) is not None

    def is_unknown(self, sha256: str) -> bool:
        """Whether this blob's privacy has not been established."""
        return self.state_of(sha256) == UNKNOWN

    def declared_mime(self, sha256: str) -> str:
        """What a private file actually is, or "" when it is not one.

        A server holding an envelope calls it a byte stream, because that
        is all it can see. Filtering a library by that type would hide
        every encrypted picture from an image picker, which is precisely
        the pick this whole flow exists to handle. The record inside the
        library knows the real type.
        """
        blob = self._private(sha256)
        return (blob.mime or "") if blob is not None else ""

    def public_copy_of(self, sha256: str) -> Optional[PublicBlob]:
        """The live public copy minted from this private file, or None.

        The ledger decides, not the record's pointer, matching
        ``PublicLedger.has_public_copy``: a revoked copy leaves a pointer
        behind, and believing it would badge a dead link as published.
        """
        blob = self._private(sha256)
        if blob is None or self._ledger is None:
            return None
        pointed = (
            self._ledger.get(blob.public_copy_hash)
            if blob.public_copy_hash
            else None
        )
        if pointed is not None and pointed.source_hash == blob.sha256:
            return pointed
        return self._ledger.copy_of(blob.sha256)

    # -- revoking ----------------------------------------------------------

    def forget_public_copy(self, sha256: str) -> bool:
        """Stop listing a public copy the user has deleted from their servers.

        Deleting the blob is what revoking a copy actually is, and the
        ledger has to follow or two things go wrong at once: the original
        keeps its published badge, and the next publish reuses a copy
        that is not there any more, which puts a dead link in someone's
        article.

        Returns whether anything was dropped, so a caller can tell a
        revoke apart from an ordinary delete. Only entries this app
        listed are touched: a hash that is not in the ledger is not ours
        to have an opinion about.
        """
        if self._ledger is None or self._ledger.get(sha256) is None:
            return False
        self._ledger.forget(sha256)
        return True

    # -- what the gate needs -----------------------------------------------

    def private_blobs(self, hashes: Iterable[str]) -> List[PrivateBlob]:
        """The private files among ``hashes``, in the order given.

        These carry keys, which is why the only caller is the copy maker:
        fetching and decrypting the original is the one job that cannot
        be done without them.
        """
        out: List[PrivateBlob] = []
        for sha in hashes:
            blob = self._private(sha)
            if blob is not None:
                out.append(blob)
        return out

    def any_private(self, hashes: Sequence[str]) -> bool:
        return any(self.is_private(sha) for sha in hashes)

    # -- the one operation that needs a key --------------------------------

    def preview(self, sha256: str, envelope: bytes) -> PreviewOutcome:
        """Decrypt this file's bytes for display, in memory only.

        The key is read and spent inside this call. It is not returned,
        not stored, and never reaches the caller, so a widget can show a
        private picture without ever holding the thing that opens it.
        """
        blob = self._private(sha256)
        if blob is None:
            return PreviewOutcome(reason="this file is not in your private library")
        return preview_from_envelope(envelope, blob.key_hex)

    # -- internals ---------------------------------------------------------

    def _private(self, sha256: str) -> Optional[PrivateBlob]:
        if self._library is None or not sha256:
            return None
        return self._library.get(sha256)

    def _vouched(self, sha256: str) -> bool:
        """Whether the library stands behind "this one is not mine".

        With no library wired there is nothing to be private of, so the
        answer is yes and the app behaves as it did before any of this
        existed. With one wired, only the library may answer.
        """
        if self._library is None:
            return True
        if not sha256:
            # Nothing was asked about, so there is nothing to withhold.
            return True
        return bool(self._library.vouches_for(sha256))
