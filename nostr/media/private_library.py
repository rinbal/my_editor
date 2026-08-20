# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The user's encrypted media library, read back off their relays.

Every private file the user owns is one replaceable kind-34578 event.
The event itself says almost nothing: a ``d`` tag holding the content
hash, and a blob of NIP-44 ciphertext the user encrypted to their own
key. Everything that matters, the file name, where the bytes live, and
above all the key that opens them, is inside that ciphertext. So reading
the library is not a query, it is a query followed by one signer
round-trip per file, and the signer is the expensive part: it may put a
prompt in front of the user for each one. That is why decryption here is
a queue of one, the same shape ``draft_sync`` uses, rather than N
requests fired at a signer that will answer them one at a time anyway.

What comes back is not trusted. A relay can return anything, including
an event from another author, a record that is not a file record, or a
file record with a key-shaped field holding junk. So parsing states its
result rather than raising: a record is a file, a tombstone, or a reason
it was skipped. One bad record costs the user that record and nothing
more, which matters most precisely when a library is large.

Because reading is slow and can fail, "this file is not in the library"
is two different answers wearing one face: it is not private, or it has
not been read yet. The second must never be served as the first, or a
file the user keeps encrypted gets published while its record is still
queued behind a signer prompt. So the library also tracks what this load
can actually answer for, and ``vouches_for`` is the only honest form of
the question.

The keys are the whole point of the module and the whole risk. Once a
record is open, its key is the entire authorisation to read that file,
so:

  - Nothing here writes to disk. The library is memory for one session,
    dropped on profile switch the way ``DraftStore`` drops plaintext.
  - No key and no ciphertext ever reaches a status line, a failure
    message or a log. Reasons from a signer are laundered through
    ``_safe_reason`` before they are shown, because a signer that echoes
    its input is a real thing and a reason string ends up in the UI.
  - ``thumbKey`` is deliberately dropped. ``PrivateBlob`` has one key
    field because there is one key this app needs; a second secret with
    nowhere to live is a second secret to leak.

Everything crossing a boundary is injected: the relay query, the signer
session pool, the relay-list lookup and the clock. The tests drive the
whole state machine with none of the four present.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Final,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)

import time as _time

from PySide6.QtCore import QObject, Signal

from ..bunker import BunkerClient, BunkerSessionPool
from ..outbox import RelayListCache, select_draft_publish_relays
from ..profiles import Profile
from .assets import is_sha256
from .visibility import PrivateBlob


# One replaceable event per private file, addressed by content hash.
PRIVATE_FILE_KIND: Final[int] = 34578

# A per-file key is 32 bytes, the only length ``filecrypto`` accepts.
# Checked while the record is still data off a relay, so a truncated or
# mistyped key is refused here rather than deep inside a decrypt.
_KEY_HEX_LEN: Final[int] = 64
_HEX_DIGITS: Final[frozenset] = frozenset("0123456789abcdef")

# Anything long and opaque in a message from a signer is treated as a
# secret and removed. Keys, ciphertext and base64 all match; ordinary
# prose does not.
_OPAQUE_RUN: Final = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_REDACTED: Final[str] = "[hidden]"

# Failure text is for a person to read, not a payload to carry.
_MAX_REASON_CHARS: Final[int] = 200

# How much of a request to compare a reason against when checking
# whether the signer just handed it back, and how long a shared run has
# to be to count as an echo rather than a coincidence.
_MAX_ECHO_SCAN: Final[int] = 4096
_ECHO_WINDOW: Final[int] = 16


class AddressableQuery(Protocol):
    """The one relay call this module makes.

    Matches ``imports.sources.nostr.RelayQueryAdapter``, which is what
    the app passes in. Declared as a protocol so nothing here depends on
    the importer package, and so a test can satisfy it in six lines.
    """

    def addressable(
        self,
        relays: Sequence[str],
        filters: List[dict],
        on_done: Callable[[List[dict]], None],
    ) -> None: ...


@dataclass(frozen=True)
class LibraryFailure:
    """One file that could not be listed, in words the user can act on.

    ``identifier`` is the event's ``d`` tag, which is a content hash and
    therefore safe to show. ``reason`` has been laundered: it never
    carries a key, a ciphertext or a fragment of either.
    """

    identifier: str
    reason: str


@dataclass(frozen=True)
class ParsedRecord:
    """What one decrypted record turned out to be.

    Exactly one of the four is meaningful: a blob, a tombstone, a record
    that is not a file at all, or a reason it was skipped. Returning this
    rather than raising is what keeps one bad record from costing the
    user the rest of a library.

    ``not_a_file`` is not a softer ``reason``. A reason means this app
    failed at something it should have managed, and the library stays
    unsure about a blob because of it. ``not_a_file`` means there was
    never a blob here to be unsure about, so it is not reported and it
    holds nothing back.
    """

    blob: Optional[PrivateBlob] = None
    tombstone: bool = False
    not_a_file: bool = False
    reason: str = ""


# --------------------------------------------------------------------------- #
# Parsing one record                                                          #
# --------------------------------------------------------------------------- #

def parse_file_record(plaintext: str, *, identifier: str) -> ParsedRecord:
    """Read one decrypted file record, or say why it cannot be read.

    ``identifier`` is the ``d`` tag the record was filed under. It is
    the content hash by definition, so it is also the cross-check: a
    record that names a different hash than the address it lives at is
    refused, because publishing from it would upload bytes under a name
    that does not describe them.
    """
    try:
        payload = json.loads(plaintext)
    except (ValueError, TypeError):
        # The exception text is not repeated. A truncated payload can
        # end mid-key, and json's message quotes what it choked on.
        return ParsedRecord(
            reason="This file's details could not be read, so it was skipped."
        )
    if not isinstance(payload, dict):
        return ParsedRecord(reason="This record is not a file, so it was skipped.")

    if payload.get("deleted"):
        # The user deleted this. Not a fault, and not a file.
        return ParsedRecord(tombstone=True)

    filed_under = str(identifier or "").strip().lower()
    sha256 = _hash_field(payload.get("hash"))
    if not sha256:
        if not is_sha256(filed_under):
            # Not a file record at all. A drive holds more than files:
            # Lotus keeps one marker per folder in this same kind, filed
            # under d="folder--<name>" and carrying
            #
            #   {"type": "application/x-folder-keep", "name": ".keep",
            #    "hash": "folder--<name>", "size": 0, "encryptionKey": ""}
            #
            # so its "hash" is the folder's own address echoed back, not
            # a content hash. Neither the address nor the hash names
            # anything a Blossom server could ever serve, so this record
            # cannot be telling us that some listed blob is private, and
            # passing over it withholds nothing.
            #
            # Counting these as unreadable file records is what left
            # every real file in a real drive badged NOT CHECKED. One is
            # enough: an unresolved record at a non-hash address
            # withholds the library's answer for every hash, deliberately,
            # because whatever it says could have named any of them.
            return ParsedRecord(not_a_file=True)
        # Filed under a content hash but naming no file this app can
        # identify. That record is claiming to be that file and failing,
        # so the doubt is real, and it stays confined to that one hash.
        return ParsedRecord(
            reason="This record does not name a file, so it was skipped."
        )

    if is_sha256(filed_under) and filed_under != sha256:
        return ParsedRecord(
            reason="This record does not match the file it is filed under, "
                   "so it was skipped."
        )

    key_hex = str(payload.get("encryptionKey") or "").strip()
    if not key_hex:
        return ParsedRecord(
            reason="This file has no key in your library, so it cannot be "
                   "opened here."
        )
    if not _is_file_key(key_hex):
        # The value itself never travels with the complaint.
        return ParsedRecord(
            reason="This file's key is not in a form this app understands, "
                   "so it cannot be opened."
        )

    try:
        blob = PrivateBlob(
            sha256=sha256,
            key_hex=key_hex.lower(),
            servers=_servers(payload),
            size=_non_negative_int(payload.get("size")),
            mime=str(payload.get("type") or ""),
            name=str(payload.get("name") or ""),
            uploaded_at=_plausible_timestamp(payload.get("uploadedAt")),
            public_copy_hash=_public_copy_hash(payload.get("publicCopy")),
        )
    except (ValueError, TypeError):
        # PrivateBlob validates too. If the two disagree the record is
        # the thing at fault, so it is skipped rather than crashed on.
        return ParsedRecord(
            reason="This file's record is incomplete, so it was skipped."
        )
    return ParsedRecord(blob=blob)


def _hash_field(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if is_sha256(text) else ""


def _is_file_key(value: str) -> bool:
    return len(value) == _KEY_HEX_LEN and set(value.lower()) <= _HEX_DIGITS


def _non_negative_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# Seconds, not milliseconds. The reference implementation writes
# ``Math.floor(Date.now() / 1000)`` everywhere, and this app treats the
# field as seconds throughout. A record is not obliged to agree: it
# arrives off a relay, and one written in milliseconds would render as a
# date tens of thousands of years away.
#
# So a timestamp outside a plausible range is dropped rather than shown.
# An unknown date reads as unknown; a confidently wrong one reads as a
# broken app, and the user cannot tell which of the two it is.
_EARLIEST_PLAUSIBLE_SECONDS = 1_200_000_000   # 2008, before Bitcoin's genesis
_LATEST_PLAUSIBLE_SECONDS = 4_000_000_000     # 2096


def _plausible_timestamp(value: object) -> int:
    """A unix timestamp in seconds, or 0 when it cannot be one."""
    number = _non_negative_int(value)
    if number and _EARLIEST_PLAUSIBLE_SECONDS <= number <= _LATEST_PLAUSIBLE_SECONDS:
        return number
    return 0


def _servers(payload: dict) -> List[str]:
    """Every server named by the record, primary first, deduped.

    The primary leads because it is the one the writer last confirmed;
    the mirrors are what a fetch falls back to.
    """
    out: List[str] = []
    seen: set = set()
    candidates: List[Any] = [payload.get("server")]
    listed = payload.get("servers")
    if isinstance(listed, list):
        candidates.extend(listed)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        url = candidate.strip()
        key = url.rstrip("/").lower()
        if not url or key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _public_copy_hash(value: object) -> str:
    """The hash of this file's public copy, if the pointer is usable.

    Only a hint. ``PublicLedger`` is what actually decides whether a
    file is published, so a junk pointer is dropped rather than
    believed.
    """
    if not isinstance(value, dict):
        return ""
    return _hash_field(value.get("hash"))


def _safe_reason(reason: object, *, echo_of: str = "") -> str:
    """A failure message with anything secret-shaped taken out.

    The signer's own words are worth showing: "user rejected" tells the
    user what to do next. What is not worth showing is the request it
    was answering, and some signers hand it straight back. Redaction
    alone does not cover that, because a request is not always opaque,
    so a reason that repeats a stretch of what we sent is dropped whole
    rather than trimmed.
    """
    text = str(reason or "").strip()
    if not text:
        return "the signer gave no reason"
    if len(text) > _MAX_REASON_CHARS:
        text = text[:_MAX_REASON_CHARS].rstrip() + "..."
    if _echoes(text, echo_of):
        return "the signer gave no reason this app can repeat"
    return _OPAQUE_RUN.sub(_REDACTED, text)


def _echoes(text: str, secret: str) -> bool:
    """Whether ``text`` repeats any real stretch of ``secret``."""
    if not secret or len(text) < _ECHO_WINDOW:
        return False
    haystack = secret[:_MAX_ECHO_SCAN]
    return any(
        text[i:i + _ECHO_WINDOW] in haystack
        for i in range(len(text) - _ECHO_WINDOW + 1)
    )


# --------------------------------------------------------------------------- #
# The library                                                                 #
# --------------------------------------------------------------------------- #

class PrivateLibrary(QObject):
    """The active profile's encrypted files, for this session only.

    Signals:
      library_changed()    the listing changed; repaint.
      status_changed(str)  one human-readable line for a footer.

    ``library_changed`` fires once per load rather than once per file.
    Two hundred files is two hundred signer round-trips, and a repaint
    per round-trip is a UI that spends the whole load rebuilding itself.
    """

    library_changed = Signal()
    status_changed = Signal(str)

    def __init__(
        self,
        *,
        session_pool: BunkerSessionPool,
        relay_list_cache: RelayListCache,
        query: AddressableQuery,
        clock: Optional[Callable[[], int]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._session_pool = session_pool
        self._relay_list_cache = relay_list_cache
        self._query = query
        self._clock = clock or (lambda: int(_time.time()))

        self._profile: Optional[Profile] = None
        # Keyed by ``d`` tag, which is the content hash. Memory only:
        # this dict is the session's entire copy of the user's keys.
        self._blobs: Dict[str, PrivateBlob] = {}
        self._failures: List[LibraryFailure] = []
        # Mirrors the last emitted status so a widget built after
        # the fact can read it instead of waiting for a repeat.
        self._status: str = ""
        self._loaded_at: int = 0
        self._loading = False

        # What this load is entitled to say. ``_covered`` means the
        # relays have answered, so the set of private records is known;
        # ``_unresolved`` holds the ones still queued behind the signer
        # or refused by it. Absence from ``_blobs`` only means "public"
        # when both of those say it can, which is what keeps an
        # unfinished or broken load from silently declaring the user's
        # encrypted files fair game.
        self._covered = False
        self._unresolved: Set[str] = set()

        # Cancellation token, as in ``DraftSync``: every callback
        # captures the generation it was issued in and bails if the
        # library has since moved to another profile. In-flight signer
        # requests cannot be recalled, so their answers are dropped.
        self._generation: int = 0

        self._queue: Deque[Tuple[str, str]] = deque()
        self._client: Optional[BunkerClient] = None
        self._inflight = False
        self._pumping = False
        self._batch_active = False

    # -- state -------------------------------------------------------------

    @property
    def active_profile(self) -> Optional[Profile]:
        return self._profile

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def loaded_at(self) -> int:
        """When the listing was last read, by the injected clock."""
        return self._loaded_at

    @property
    def files(self) -> List[PrivateBlob]:
        """Every listed file, newest upload first."""
        return sorted(
            self._blobs.values(), key=lambda b: b.uploaded_at, reverse=True,
        )

    @property
    def failures(self) -> List[LibraryFailure]:
        """The records this load could not list, and why."""
        return list(self._failures)

    @property
    def status(self) -> str:
        """The line a footer would show right now.

        A dialog built after the load finished never saw
        ``status_changed`` go by, so it has to be able to ask instead of
        assuming the silence means nothing has happened. Without this a
        second opening of the media library reported a library that had
        been read minutes ago as never read.
        """
        return self._status

    def _emit_status(self, text: str) -> None:
        self._status = text
        self.status_changed.emit(text)

    @property
    def settled(self) -> bool:
        """Whether this listing is a complete account of what is private.

        False before a profile is bound, false for as long as a load is
        running, and false afterwards for as long as any record the
        relays listed could not be opened. A caller showing files while
        this is False is showing files whose privacy it does not know.
        """
        return self._covered and not self._unresolved

    def vouches_for(self, sha256: str) -> bool:
        """Whether "this file is not private" is an answer, not a guess.

        A hash is vouched for when the relays have answered, so every
        private record is accounted for, and this particular one is not
        among the records still waiting on the signer or refused by it.

        A record filed under something other than a content hash is the
        one case that poisons the whole answer rather than its own: if
        it could not be opened, its hash is inside the ciphertext and
        there is no way to tell which file it was, so nothing can be
        vouched for until it resolves.
        """
        if not self._covered:
            return False
        wanted = (sha256 or "").strip().lower()
        for identifier in self._unresolved:
            filed_under = str(identifier or "").strip().lower()
            if not is_sha256(filed_under):
                return False
            if filed_under == wanted:
                return False
        return True

    def get(self, sha256: str) -> Optional[PrivateBlob]:
        """The file with this content hash, if it is listed.

        A scan rather than a second index. A library is small enough
        that the scan is free, and an index that can disagree with the
        map it mirrors is a way to hand out a stale key.
        """
        wanted = (sha256 or "").strip().lower()
        if not wanted:
            return None
        for blob in self._blobs.values():
            if blob.sha256 == wanted:
                return blob
        return None

    def __len__(self) -> int:
        return len(self._blobs)

    def __iter__(self) -> Iterator[PrivateBlob]:
        return iter(self.files)

    # -- lifecycle ---------------------------------------------------------

    def bind_profile(self, profile: Optional[Profile]) -> None:
        """Switch to a profile and load its library, or clear entirely.

        Re-binding the profile already loaded is a no-op, so callers can
        be lazy; ``refresh`` is how you ask for a re-read.
        """
        current = _pubkey_of(self._profile)
        wanted = _pubkey_of(profile)
        if profile is not None and current and current == wanted:
            return
        self.stop()
        self._profile = profile
        if profile is None:
            self.library_changed.emit()
            return
        self.refresh()

    def stop(self) -> None:
        """Forget everything and invalidate anything in flight.

        This is where the keys go. Bumping the generation first means a
        signer answering after the switch cannot put the previous
        identity's key back into a library that now belongs to another.
        """
        self._generation += 1
        self._profile = None
        self._blobs.clear()
        self._failures = []
        self._queue.clear()
        self._client = None
        self._inflight = False
        self._batch_active = False
        self._loading = False
        self._loaded_at = 0
        # Whatever this library knew was about the previous identity, so
        # it may not be quoted about this one. Back to knowing nothing.
        self._covered = False
        self._unresolved = set()
        self._status = ""

    def refresh(self) -> None:
        """Re-read the library from the user's relays."""
        profile = self._profile
        if profile is None:
            return
        # A refresh replaces the listing, so the previous load's
        # complaints go with it. Keeping them would stack one report per
        # refresh for a record that is broken once. The generation moves
        # too: a signer still chewing on the last load would otherwise
        # file its answer against this one.
        self._generation += 1
        self._failures = []
        self._queue.clear()
        self._client = None
        self._inflight = False
        self._batch_active = False
        self._loading = True
        # The previous load's coverage does not carry over. Until the
        # relays answer again this library cannot account for anything,
        # and saying otherwise would let a refresh open a window in
        # which private files read as public.
        self._covered = False
        self._unresolved = set()
        gen = self._generation
        self._emit_status("Opening your private library...")

        def _on_relay_list(relay_list) -> None:
            if not self._is_current(gen):
                return
            relays = select_draft_publish_relays(
                relay_list, bunker_relays=profile.bunker_relays,
            )
            self._query.addressable(
                relays,
                [{
                    "kinds": [PRIVATE_FILE_KIND],
                    "authors": [profile.user_pubkey],
                }],
                lambda events, g=gen: self._on_events(g, events),
            )

        self._relay_list_cache.fetch(
            profile.user_pubkey,
            relays=list(dict.fromkeys(profile.bunker_relays)),
            on_done=_on_relay_list,
        )

    # -- internal: cancellation -------------------------------------------

    def _is_current(self, gen: int) -> bool:
        return self._profile is not None and gen == self._generation

    # -- internal: events in, decryption out ------------------------------

    def _on_events(self, gen: int, events: Sequence[dict]) -> None:
        if not self._is_current(gen):
            return
        self._blobs.clear()
        self._loaded_at = int(self._clock())
        wanted = _pubkey_of(self._profile)
        # The relays have answered, so the set of private records is now
        # known even though none of them is open yet. Every one of them
        # starts unresolved and leaves that set only by being read, so a
        # record parked behind a signer prompt is never mistaken for a
        # file that was never private.
        self._covered = True
        self._unresolved = set()
        for identifier, ciphertext in _newest_per_identifier(events, wanted):
            self._unresolved.add(identifier)
            if ciphertext:
                self._queue.append((identifier, ciphertext))
            else:
                # No content is no record. Reported rather than dropped
                # so the count the user sees adds up.
                self._failures.append(LibraryFailure(
                    identifier=identifier,
                    reason="This file's details are missing from your "
                           "library, so it was skipped.",
                ))
        self._batch_active = True
        if not self._queue:
            self._finish_batch()
            return
        self._session_pool.get(
            self._profile,
            on_ready=lambda client, g=gen: self._on_signer_ready(g, client),
            on_error=lambda reason, g=gen: self._on_signer_unavailable(g, reason),
        )

    def _on_signer_ready(self, gen: int, client: BunkerClient) -> None:
        if not self._is_current(gen):
            return
        self._client = client
        self._pump()

    def _on_signer_unavailable(self, gen: int, reason: str) -> None:
        if not self._is_current(gen):
            return
        # One message, not one per file: without a signer nothing in
        # this library can be opened, and that is a single fact.
        #
        # The queue is dropped but ``_unresolved`` deliberately is not.
        # Those files are still private and this app still cannot prove
        # which they are, so they must keep reading as unknown for the
        # rest of the session rather than quietly becoming public.
        self._queue.clear()
        self._batch_active = False
        self._loading = False
        self._emit_status(
            f"Couldn't reach your signer, so your private library stayed "
            f"closed: {_safe_reason(reason)}"
        )
        self.library_changed.emit()

    def _pump(self) -> None:
        """Ask the signer for the next file, one at a time.

        Written as a loop with a re-entrancy guard rather than
        callback recursion. A signer that answers synchronously (a
        cached session, or a test) would otherwise recurse once per
        file, and two hundred files is deep enough to matter.
        """
        if self._pumping or self._inflight:
            return
        self._pumping = True
        try:
            while self._queue and not self._inflight and self._client is not None:
                identifier, ciphertext = self._queue.popleft()
                self._inflight = True
                gen = self._generation
                self._client.nip44_decrypt_self(
                    ciphertext,
                    on_success=lambda plaintext, i=identifier, g=gen: (
                        self._on_decrypted(g, i, plaintext)
                    ),
                    on_failure=lambda reason, i=identifier, c=ciphertext, g=gen: (
                        self._on_decrypt_failed(g, i, reason, c)
                    ),
                )
            # A signer that disappeared mid-batch ends the batch with
            # whatever was opened. Leaving the queue parked would leave
            # the panel loading forever with no way back.
            drained = not self._inflight and (
                not self._queue or self._client is None)
        finally:
            self._pumping = False
        if drained:
            self._finish_batch()

    def _settle(self) -> None:
        self._inflight = False
        # A no-op while the loop above is still running, which is what
        # keeps a synchronous signer iterative instead of recursive.
        self._pump()

    def _on_decrypted(self, gen: int, identifier: str, plaintext: str) -> None:
        if not self._is_current(gen):
            return
        parsed = parse_file_record(plaintext, identifier=identifier)
        if parsed.tombstone or parsed.not_a_file:
            # Neither one leaves a private file at this address: the
            # tombstone because the file is gone, the folder because it
            # was never a file. Either way this address is settled and
            # stops counting against what the library can vouch for.
            # Dropping any earlier entry matters because a tombstone can
            # arrive for a record we already listed from a stale event.
            self._blobs.pop(identifier, None)
            self._unresolved.discard(identifier)
        elif parsed.blob is not None:
            self._blobs[identifier] = parsed.blob
            self._unresolved.discard(identifier)
        else:
            self._failures.append(
                LibraryFailure(identifier=identifier, reason=parsed.reason))
        self._settle()

    def _on_decrypt_failed(
        self, gen: int, identifier: str, reason: str, ciphertext: str,
    ) -> None:
        if not self._is_current(gen):
            return
        self._failures.append(LibraryFailure(
            identifier=identifier,
            reason="Your signer could not open this file: "
                   + _safe_reason(reason, echo_of=ciphertext),
        ))
        self._settle()

    def _finish_batch(self) -> None:
        if not self._batch_active:
            return
        self._batch_active = False
        self._client = None
        self._loading = False
        self._emit_status(self._summary())
        self.library_changed.emit()

    def _summary(self) -> str:
        count = len(self._blobs)
        failed = len(self._failures)
        if not count and not failed:
            return "Your private library is empty."
        line = f"{count} file{'' if count == 1 else 's'} in your private library"
        if failed:
            line += f", {failed} could not be opened"
        return line + "."


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _newest_per_identifier(
    events: Sequence[dict], author: str,
) -> List[Tuple[str, str]]:
    """One (d tag, ciphertext) per file, newest event winning.

    These are replaceable events, so a relay may still hand back an old
    copy alongside the current one. Newest ``created_at`` wins, and at
    an equal timestamp the lower event id does, which is NIP-01's rule:
    without it two devices that saved in the same second would show
    different libraries.

    Events from another author are dropped here. A relay can answer with
    anything, and adopting a foreign record would put someone else's key
    into this user's library.
    """
    best: Dict[str, Tuple[int, str, str]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("pubkey", "")).lower() != author:
            continue
        identifier = _d_tag(event.get("tags"))
        if not identifier:
            continue
        try:
            created_at = int(event.get("created_at", 0))
        except (TypeError, ValueError):
            continue
        event_id = str(event.get("id", ""))
        content = str(event.get("content") or "")
        previous = best.get(identifier)
        if previous is not None:
            prior_at, prior_id, _prior = previous
            if created_at < prior_at:
                continue
            if created_at == prior_at and event_id >= prior_id:
                continue
        best[identifier] = (created_at, event_id, content)
    return [(identifier, entry[2]) for identifier, entry in best.items()]


def _pubkey_of(profile: Optional[Profile]) -> str:
    return str(getattr(profile, "user_pubkey", "") or "").lower()


def _d_tag(tags: object) -> str:
    if not isinstance(tags, list):
        return ""
    for tag in tags:
        if (
            isinstance(tag, list)
            and len(tag) >= 2
            and tag[0] == "d"
            and isinstance(tag[1], str)
        ):
            return tag[1].strip()
    return ""
