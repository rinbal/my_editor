# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""BUD-03 user server lists (kind 10063): parse, discover, decide.

A kind 10063 event advertises the Blossom servers a user hosts blobs on.
Two things make it worth reading: it tells other clients where to look
when a blob URL dies, and it tells this app where a new user's blobs
already live before they have configured anything.

The ``server`` tag here is NOT the ``server`` tag in a kind 24242
authorization token. BUD-03 line 9: "The event MUST include at least one
``server`` tag containing the full server URL including the ``http://``
or ``https://``". BUD-11 line 25 says the opposite for the token: "a
lowercase domain name only ... not a full URL". Same tag name, opposite
requirements, different events. See ADR AD-11 and the adjacent pair of
tests in ``tests/test_blossom_server_tags.py``: making them agree is a
defect, not a cleanup. That is why a bare domain found in a 10063 is
dropped rather than repaired with an ``https://`` prefix. Repairing it
would be exactly the harmonization AD-11 forbids, and it would guess a
scheme the author never published.

The policy in :class:`UserServerList` is the whole of AD-13, and nothing
else in the app may implement it:

1. The local configuration is the user's explicit choice and stays
   authoritative for uploads whenever it exists.
2. A discovered list is always usable for retrieval and recovery.
3. It is never silently promoted to an upload target, only suggested.
4. It is adopted only when there is no local configuration at all, and
   the adoption is announced.
5. Publishing our own list happens only on deliberate user action, which
   is why :func:`build_server_list_event` is a pure builder here and no
   publish path in this package calls it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, Signal

import url_safety

from .. import DEFAULT_RELAYS
from ..events import build_event
from ..queries import fetch_latest_event
from .settings import BlossomSettings


BLOSSOM_SERVER_LIST_KIND = 10063

# BUD-03 lists are meant to be short, and every entry is a host this app
# may contact during recovery. Ten is the same ceiling the NIP-65 relay
# list uses, and it bounds the amplification a hostile list can cause.
MAX_SERVER_LIST_ENTRIES = 10

# The scheme is not optional (bud-03.md:9). Checked literally rather than
# inferred from a parse, because "no scheme" and "unparseable" must lead
# to the same outcome: drop it, never repair it.
_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)

# Same values as the NIP-65 cache in ``nostr/outbox.py``: a published
# list changes rarely, and an empty answer is re-asked sooner so a user
# who has just published theirs is not stuck with the miss.
_TTL_HIT_S: int = 30 * 60
_TTL_EMPTY_S: int = 3 * 60


# --------------------------------------------------------------------------- #
# Pure parsing and building                                                   #
# --------------------------------------------------------------------------- #

def _normalize_entry(value: object) -> Optional[str]:
    """A BUD-03 ``server`` value reduced to a usable origin, or None.

    Drops anything without an explicit ``http://`` or ``https://``, then
    applies the media policy, which is https anywhere plus http on
    loopback. Refusing a plain-http public server is a restriction on
    what BUD-03 permits, taken deliberately: every entry here becomes a
    retrieval target and may become an upload target, and both of those
    paths already require the stronger policy.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not _SCHEME_RE.match(text):
        return None
    origin = url_safety.origin_of(text)
    if origin is None or not url_safety.is_safe_media_url(origin):
        return None
    return origin


def parse_server_list(event: object) -> List[str]:
    """The server origins a kind 10063 event advertises, in order.

    Document order is the answer: BUD-03 line 11 says the order "should
    be arranged with the users most 'reliable' or 'trusted' servers being
    first", so it is preference data, not an accident of encoding.

    ``.content`` is ignored entirely (bud-03.md:13). A malformed event
    yields an empty list rather than raising: this is a stranger's event
    arriving over a relay, and the caller has nothing useful to do with
    an exception.
    """
    if not isinstance(event, dict):
        return []
    tags = event.get("tags")
    if not isinstance(tags, (list, tuple)):
        return []

    servers: List[str] = []
    seen: set = set()
    for tag in tags:
        if not isinstance(tag, (list, tuple)) or len(tag) < 2:
            continue
        if tag[0] != "server":
            continue
        origin = _normalize_entry(tag[1])
        if origin is None or origin in seen:
            continue
        seen.add(origin)
        servers.append(origin)
        if len(servers) >= MAX_SERVER_LIST_ENTRIES:
            break
    return servers


def build_server_list_event(
    servers: Sequence[str], pubkey_hex: str, *, created_at: Optional[int] = None
) -> dict:
    """An unsigned kind 10063 event advertising ``servers``.

    Each tag carries the FULL server URL including the scheme, which is
    what BUD-03 requires and the opposite of what the kind 24242
    ``server`` tag requires. Order is preserved because the order is the
    user's preference.

    Pure: this returns an event for a caller to sign and publish on a
    deliberate user action (AD-13.5). Nothing in this package publishes
    it, and no upload path may.
    """
    tags: List[List[str]] = []
    seen: set = set()
    for entry in servers:
        origin = _normalize_entry(entry)
        if origin is None or origin in seen:
            continue
        seen.add(origin)
        tags.append(["server", origin])
        if len(tags) >= MAX_SERVER_LIST_ENTRIES:
            break
    return build_event(
        kind=BLOSSOM_SERVER_LIST_KIND,
        content="",
        tags=tags,
        pubkey_hex=pubkey_hex,
        created_at=created_at,
    )


# --------------------------------------------------------------------------- #
# Cached fetcher                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class _CacheEntry:
    servers: List[str]
    fetched_at: float


class ServerListCache(QObject):
    """Fetches kind 10063 events on demand and caches them per pubkey.

    Shaped like ``nostr.outbox.RelayListCache``, for the same reasons:
    concurrent asks coalesce into one REQ, and a hit resolves without
    touching a relay.

    In memory only, on purpose. A disk cache of other people's server
    lists is a record of whose media the user looked at, it goes stale
    invisibly, and the only caller that needs it (recovery of a dead
    blob URL) can afford one relay round trip.
    """

    def __init__(self, pool, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._cache: Dict[str, _CacheEntry] = {}
        self._inflight: Dict[str, List[Callable[[List[str]], None]]] = {}

    def clear(self) -> None:
        self._cache.clear()

    def invalidate(self, pubkey_hex: str) -> None:
        self._cache.pop(pubkey_hex, None)

    def get_cached(self, pubkey_hex: str) -> Optional[List[str]]:
        """The cached list if still fresh, else None. ``[]`` is a cached
        answer meaning "this user publishes no list", not a miss."""
        entry = self._cache.get(pubkey_hex)
        if entry is None:
            return None
        ttl = _TTL_EMPTY_S if not entry.servers else _TTL_HIT_S
        if time.time() - entry.fetched_at > ttl:
            return None
        return list(entry.servers)

    def fetch(
        self,
        pubkey_hex: str,
        relays: List[str],
        on_done: Callable[[List[str]], None],
        *,
        timeout_ms: int = 6_000,
    ) -> None:
        """Resolve one user's server list (cached or fresh), then call back."""
        cached = self.get_cached(pubkey_hex)
        if cached is not None:
            on_done(cached)
            return
        if self._pool is None:
            # Constructed before there is anything to ask. Answering
            # "nothing published" is the same outcome as a relay that
            # never replies, and it keeps every caller on one code path.
            on_done([])
            return

        waiters = self._inflight.get(pubkey_hex)
        if waiters is not None:
            waiters.append(on_done)
            return
        self._inflight[pubkey_hex] = [on_done]

        def _on_event(event: Optional[dict]) -> None:
            servers = parse_server_list(event) if event else []
            self._cache[pubkey_hex] = _CacheEntry(
                servers=list(servers), fetched_at=time.time()
            )
            callbacks = self._inflight.pop(pubkey_hex, [])
            for callback in callbacks:
                try:
                    callback(list(servers))
                except Exception:  # noqa: BLE001 - one bad waiter must not drop the rest
                    pass

        fetch_latest_event(
            self._pool,
            relays,
            filters=[{
                "kinds": [BLOSSOM_SERVER_LIST_KIND],
                "authors": [pubkey_hex],
                "limit": 1,
            }],
            on_done=_on_event,
            timeout_ms=timeout_ms,
            parent=self,
        )


# --------------------------------------------------------------------------- #
# Policy                                                                      #
# --------------------------------------------------------------------------- #

class UserServerList(QObject):
    """The one place kind 10063 turns into a decision (ADR AD-13).

    ``discovered`` reports what the profile publishes, every time.
    ``adopted`` fires only on the first run described in AD-13.4, when
    there is no local configuration at all, and the caller is expected to
    say so where the user can see it. ``suggestions_changed`` carries the
    published servers the user has not configured; they are an offer, and
    only the user turns one into an upload target.
    """

    discovered = Signal(list)
    adopted = Signal(list)
    suggestions_changed = Signal(list)

    def __init__(
        self,
        pool=None,
        *,
        settings: Optional[BlossomSettings] = None,
        cache: Optional[ServerListCache] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings if settings is not None else BlossomSettings()
        self._cache = cache if cache is not None else ServerListCache(pool, parent=self)
        self._servers: List[str] = list(self._settings.discovered_servers)
        self._pubkey: str = self._settings.discovered_pubkey

    # -- read --------------------------------------------------------------

    @property
    def settings(self) -> BlossomSettings:
        return self._settings

    @property
    def discovered_servers(self) -> List[str]:
        """The last published list seen, which survives a restart through
        the settings file so recovery works before the first fetch."""
        return list(self._servers)

    @property
    def discovered_pubkey(self) -> str:
        """Whose list :attr:`discovered_servers` belongs to."""
        return self._pubkey

    @property
    def suggestions(self) -> List[str]:
        """Published servers the user has not configured. An offer only."""
        configured = self._settings.configured_servers
        return [s for s in self._servers if s not in configured]

    def recovery_servers(self, sha256: str = "") -> List[str]:
        """Origins worth trying for a blob whose stored URL went dead.

        AD-13.2, and the only consumer of a discovered list that is not
        the user's own choice. The configured servers come first because
        they are the list the user picked, then the published list in
        its own order, which BUD-03 calls the author's preference order.

        ``sha256`` is accepted and not consulted: every asset this app
        can recover is one it uploaded, so the author is the active
        profile and the answer is the same for every hash. Keeping the
        parameter means a per-author lookup can land later without
        touching the callers or the seam in ``AssetManager``.
        """
        out: List[str] = []
        for origin in list(self._settings.configured_servers) + list(self._servers):
            if origin and origin not in out:
                out.append(origin)
        return out

    # -- discovery ---------------------------------------------------------

    def refresh(
        self, profile: object, *, force: bool = False, timeout_ms: int = 6_000
    ) -> None:
        """Fetch the profile's published list and apply AD-13 once.

        ``force`` drops the cached answer first, which is what a "check
        again" action in Settings needs; ordinary profile activation
        should leave it alone so switching profiles twice does not mean
        two REQs.
        """
        pubkey = str(getattr(profile, "user_pubkey", "") or "")
        if not pubkey:
            return
        if force:
            self._cache.invalidate(pubkey)
        extra = list(getattr(profile, "bunker_relays", None) or [])
        relays = list(dict.fromkeys(list(DEFAULT_RELAYS) + extra))
        self._cache.fetch(
            pubkey,
            relays,
            lambda servers, pk=pubkey: self._apply(pk, servers),
            timeout_ms=timeout_ms,
        )

    def _apply(self, pubkey: str, servers: List[str]) -> None:
        self.discovered.emit(list(servers))
        if not servers:
            # An empty answer is far more often "no relay replied in
            # time" than "the user deleted their list", and the two are
            # indistinguishable from here. The last list seen is kept, so
            # a timeout cannot cost the user their recovery targets.
            return
        self._pubkey = pubkey
        self._servers = list(servers)

        if not self._settings.has_explicit_config:
            adopted = self._settings.adopt_discovered(servers, pubkey)
            if adopted:
                self.adopted.emit(list(adopted))
                return
            # Adoption refused, which means the user reverted to the
            # bundled defaults on purpose. That is an explicit choice
            # too, so fall through and offer rather than override.

        self._settings.record_discovered(servers, pubkey)
        self.suggestions_changed.emit(self.suggestions)
