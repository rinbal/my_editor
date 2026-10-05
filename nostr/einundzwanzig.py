# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einundzwanzig association membership, and the benefits it unlocks.

Members of the Einundzwanzig Bitcoin association get a members-only relay
and a members-only Blossom media server. This module answers one
question, "is this account on the roster", and names what that entitles
them to. It decides nothing about how those benefits are used; the relay
and media layers own that.

Membership comes from a roster the association publishes per year. The
whole roster is fetched and matched locally rather than asking the server
about one pubkey, which means the association learns that somebody opened
the app but not who. That is a happy accident of the API shape and worth
keeping if it ever changes.

A failure never grants what was not known. Before any roster has arrived,
every failure path resolves to "not a member": a non-member being handed a
members-only relay produces writes that are rejected, which is a confusing
failure a user cannot act on. Once a roster is held, a refresh that fails
keeps it: the host being down for a minute says nothing about who is a
member, and a paying member losing their relay and media server every time
it hiccups is a failure they cannot act on either.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .einundzwanzig_api import nip05_handle_problem


# --------------------------------------------------------------------------- #
# The benefits                                                                 #
# --------------------------------------------------------------------------- #

# The members-only relay. Its NIP-11 advertises restricted_writes, so a
# non-member's events are rejected rather than silently dropped.
#
# This is deliberately NOT wss://group.einundzwanzig.space. That host is the
# association's NIP-29 groups relay: it serves group traffic, not an account's
# ordinary notes and articles. Naming it in a relay list would tell the network
# to look for this author somewhere that will not answer for them.
MEMBER_RELAY: str = "wss://nostr.einundzwanzig.space"

# The members-only Blossom media server.
MEMBER_BLOSSOM: str = "https://blossom.einundzwanzig.space"

# Server limits, published by the association. The server is authoritative.
# The per-file limit the upload planner enforces lives in the Blossom server
# registry (nostr/blossom/servers.py), which a test pins to this number; the
# per-member allowance is what the Media Library measures usage against.
MAX_FILE_BYTES: int = 1024 ** 3          # 1 GiB per file
MAX_FILE_LABEL: str = "1 GB"
PER_USER_BYTES: int = 5 * 1024 ** 3      # 5 GiB per member
PER_USER_LABEL: str = "5 GB"

# Where somebody who is not a member can read about joining.
JOIN_URL: str = "https://verein.einundzwanzig.space/association/profile"

_ROSTER_URL = "https://verein.einundzwanzig.space/api/members/{year}"

# The association refreshes the roster roughly every quarter hour, so a
# newly joined member is recognized promptly without polling the host.
_CACHE_TTL_SECONDS: float = 15 * 60

# A third-party host must never be able to hang the app.
_TIMEOUT_MS: int = 8000

# The roster is small (low hundreds). Anything larger is not the roster.
_MAX_ROSTER_BYTES: int = 4 * 1024 * 1024

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z", re.IGNORECASE)

# The association serves verified names under this domain.
NIP05_DOMAIN: str = "einundzwanzig.space"


def _is_handle(handle: str) -> bool:
    """A name the association could serve. One rule for the whole app: the
    one the membership API checks a requested name against."""
    return bool(handle) and nip05_handle_problem(handle) is None


@dataclass(frozen=True)
class Benefits:
    """What membership entitles an account to.

    Held as data rather than read from constants at each call site so a
    caller can be handed the empty set for a non-member and need no
    membership branch of its own.
    """

    relay: str = ""
    blossom_server: str = ""
    per_user_bytes: int = 0

    @property
    def is_member(self) -> bool:
        return bool(self.relay or self.blossom_server)


NO_BENEFITS = Benefits()
MEMBER_BENEFITS = Benefits(
    relay=MEMBER_RELAY,
    blossom_server=MEMBER_BLOSSOM,
    per_user_bytes=PER_USER_BYTES,
)


def benefits_for(is_member: bool) -> Benefits:
    return MEMBER_BENEFITS if is_member else NO_BENEFITS


# --------------------------------------------------------------------------- #
# Roster parsing                                                               #
# --------------------------------------------------------------------------- #

def parse_roster(payload: bytes) -> Set[str]:
    """Hex pubkeys from a roster response, lowercased.

    The roster is a third-party document, so nothing about its shape is
    assumed beyond "a list of objects that may carry a pubkey". A record
    that is malformed is skipped rather than failing the whole roster,
    since one bad row must not cost every member their benefits.
    """
    return set(parse_roster_records(payload))


def parse_roster_records(payload: bytes) -> Dict[str, Optional[str]]:
    """Hex pubkey -> verified name handle (or None) from a roster response.

    The handle is the part before ``@einundzwanzig.space``. One that does
    not look like a handle is dropped, never the member it came with.
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    found: Dict[str, Optional[str]] = {}
    for record in data:
        if not isinstance(record, dict):
            continue
        pubkey = record.get("pubkey")
        if not (isinstance(pubkey, str) and _HEX64.match(pubkey.strip())):
            continue
        handle = record.get("nip05_handle")
        handle = handle.strip().lower() if isinstance(handle, str) else ""
        found[pubkey.strip().lower()] = handle if _is_handle(handle) else None
    return found


def nip05_address(handle: str) -> str:
    """``handle@einundzwanzig.space``."""
    return f"{handle}@{NIP05_DOMAIN}"


# --------------------------------------------------------------------------- #
# Membership lookup                                                            #
# --------------------------------------------------------------------------- #

class MembershipDirectory(QObject):
    """Resolves whether a pubkey is on the current member roster.

    One in-flight request at a time, one cached answer with a TTL. The
    cache holds the roster rather than a yes/no per pubkey so switching
    between profiles costs nothing extra. A refresh that fails keeps the
    roster it was refreshing, so a member keeps their benefits while the
    association's host is unreachable.
    """

    # pubkey_hex, is_member. Emitted once per resolve, success or failure.
    resolved = Signal(str, bool)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        nam: Optional[QNetworkAccessManager] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(parent)
        # Both seams exist for tests: no test may touch the network, and
        # none may depend on the wall clock or the calendar year.
        self._nam = nam or QNetworkAccessManager(self)
        self._clock = clock or time.monotonic
        self._roster: Optional[Set[str]] = None
        self._handles: Dict[str, Optional[str]] = {}
        # Pubkeys the association itself just confirmed (a signed status
        # answer), so a fresh member is recognized before the published
        # roster catches up with them.
        self._confirmed: Set[str] = set()
        # Names saved through the membership window this session. They win
        # over the roster, which can take a while to list a new name.
        self._saved_handles: Dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._inflight: Optional[QNetworkReply] = None
        self._waiting: list = []

    # -- cached answers ----------------------------------------------------

    def cached_membership(self, pubkey_hex: str) -> Optional[bool]:
        """A fresh answer if one is held, else None.

        None means "not known yet", which callers must treat differently
        from False. Showing a member the non-member state while the roster
        loads would flicker a "join the association" prompt at somebody who
        already did.
        """
        if (pubkey_hex or "").strip().lower() in self._confirmed:
            return True
        if not self._is_fresh():
            return None
        return self._match(pubkey_hex)

    def cached_benefits(self, pubkey_hex: str) -> Optional[Benefits]:
        known = self.cached_membership(pubkey_hex)
        return None if known is None else benefits_for(known)

    def last_known_membership(self, pubkey_hex: str) -> Optional[bool]:
        """The latest answer held, fresh or not; None when there never was one.

        For callers that must not flicker while a stale roster is being
        refreshed: a member's server silently dropping out of uploads and
        the library every fifteen minutes is worse than an answer that is
        a few minutes old.
        """
        key = (pubkey_hex or "").strip().lower()
        if key in self._confirmed:
            return True
        if self._roster is None:
            return None
        return self._match(key)

    def _is_fresh(self) -> bool:
        return (
            self._roster is not None
            and (self._clock() - self._fetched_at) < _CACHE_TTL_SECONDS
        )

    def _match(self, pubkey_hex: str) -> bool:
        key = (pubkey_hex or "").strip().lower()
        if key and key in self._confirmed:
            return True
        if self._roster is None:
            return False
        return bool(key) and key in self._roster

    def cached_handle(self, pubkey_hex: str) -> Optional[str]:
        """The verified name handle for this pubkey, if any: one saved this
        session, else the one the roster lists."""
        key = (pubkey_hex or "").strip().lower()
        return self._saved_handles.get(key) or self._handles.get(key)

    def record_handle(self, pubkey_hex: str, handle: str) -> None:
        """Remember a name the association just accepted for this pubkey,
        so it is shown before the published roster lists it."""
        key = (pubkey_hex or "").strip().lower()
        value = (handle or "").strip().lower()
        if _HEX64.match(key) and _is_handle(value):
            self._saved_handles[key] = value

    def confirm_member(self, pubkey_hex: str) -> None:
        """Record that the association confirmed this pubkey as a member.

        Only call this with an answer from the association's signed status
        endpoint. It holds for the rest of the session; the roster takes
        over from the next launch.
        """
        key = (pubkey_hex or "").strip().lower()
        if _HEX64.match(key):
            self._confirmed.add(key)

    def forget(self, pubkey_hex: str) -> None:
        """Drop what this session learned about a pubkey from the association
        itself (a confirmation, a saved name), after its data was deleted.
        The roster decides again from its next answer."""
        key = (pubkey_hex or "").strip().lower()
        self._confirmed.discard(key)
        self._saved_handles.pop(key, None)

    def invalidate(self) -> None:
        """Drop the cached roster so the next resolve refetches."""
        self._roster = None
        self._fetched_at = 0.0

    # -- resolving ---------------------------------------------------------

    def resolve(self, pubkey_hex: str, year: Optional[int] = None, *,
                force: bool = False) -> None:
        """Answer for ``pubkey_hex``, from cache when it is fresh.

        ``force`` fetches the roster even when the held one is fresh, for a
        periodic refresh that must not wait for the cache to go stale. The
        held roster keeps answering meanwhile, and keeps answering if the
        fetch fails.

        Always emits ``resolved`` exactly once, so a caller can rely on the
        signal rather than having to branch on whether a cache hit occurred.
        """
        key = (pubkey_hex or "").strip().lower()
        if not key or not _HEX64.match(key):
            self.resolved.emit(key, False)
            return
        if self._is_fresh() and not force:
            self.resolved.emit(key, self._match(key))
            return
        self._waiting.append(key)
        if self._inflight is None:
            self._start(year if year is not None else self._current_year())

    def _current_year(self) -> int:
        return time.gmtime().tm_year

    def _start(self, year: int) -> None:
        request = QNetworkRequest(QUrl(_ROSTER_URL.format(year=year)))
        request.setRawHeader(b"Accept", b"application/json")
        request.setTransferTimeout(_TIMEOUT_MS)
        # A roster is a plain document. Following a redirect to another host
        # would let the association's DNS decide where this request lands.
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy,
        )
        reply = self._nam.get(request)
        self._inflight = reply
        oversize = {"hit": False}

        def _guard(received: int, total: int, r=reply) -> None:
            if oversize["hit"]:
                return
            if received > _MAX_ROSTER_BYTES or total > _MAX_ROSTER_BYTES:
                oversize["hit"] = True
                r.abort()

        reply.downloadProgress.connect(_guard)
        reply.finished.connect(lambda r=reply, y=year: self._on_reply(r, y, oversize))

    def _on_reply(self, reply: QNetworkReply, year: int, oversize: dict) -> None:
        self._inflight = None
        try:
            ok = reply.error() == QNetworkReply.NoError and not oversize["hit"]
            records = parse_roster_records(bytes(reply.readAll())) if ok else {}
            roster = set(records)
        finally:
            reply.deleteLater()

        # An empty roster right after New Year usually means the association
        # has not rolled the new year's list over yet, so last year's list is
        # still the truthful answer. Tried once, never in a loop.
        #
        # Only a successful fetch earns the fallback. A refused, hung or
        # oversized response says nothing about which year is current, and
        # retrying a different year would just be a second way to fail.
        if ok and not roster and self._is_rollover_retry_worthwhile(year):
            self._start(year - 1)
            return

        # A failed fetch keeps the roster it was refreshing, and its age, so
        # the next resolve tries again while every answer meanwhile is the
        # last one known. Without a roster there is nothing to keep, and
        # every waiter hears "not a member".
        if ok:
            self._roster = roster
            self._handles = records
            self._fetched_at = self._clock()
        waiting, self._waiting = self._waiting, []
        for key in waiting:
            self.resolved.emit(key, self._match(key))

    def _is_rollover_retry_worthwhile(self, year: int) -> bool:
        return year >= self._current_year()
